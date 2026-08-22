from fastapi.testclient import TestClient


def test_desktop_agent_workspace_and_model_planned_tool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "desktop-agent.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now
    from app.services import desktop_agent

    init_db()
    now = utc_now()
    with connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO job_postings (
                platform, title, company, city, salary_text, jd_text, extracted_json,
                match_score, match_level, risk_level, recommendation, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Boss 直聘", "AI Agent 开发实习生", "桌面测试科技", "杭州", "200-300元/天", "Python RAG FastAPI",
                '{"extracted":{"required_skills":["Python"]},"scoring":{"fit_notes":["Python 匹配"]}}',
                88, "高匹配", "低", "必投", "待确认", now, now,
            ),
        ).lastrowid

    class FakeClient:
        configured = True
        profile = {"id": 1, "name": "Terra"}
        model = "gpt-5.6-terra"

        def __init__(self):
            self.calls = 0

        def complete_json(self, _messages):
            self.calls += 1
            if self.calls == 1:
                return {
                    "plan": "先查看本地岗位，再根据已有证据给出建议。",
                    "tool_calls": [{"name": "list_jobs", "arguments": {"limit": 3}}],
                    "response": "",
                    "task_summary": "已查看本地岗位机会。",
                }
            return {
                "plan": "岗位列表已返回，无需继续调用工具。",
                "tool_calls": [],
                "response": "我已读取本地岗位列表，优先关注匹配分高且风险低的机会。",
                "task_summary": "已查看本地岗位机会。",
            }

    fake_client = FakeClient()
    monkeypatch.setattr(desktop_agent, "load_client", lambda _profile_id: (fake_client, {"id": 1, "name": "Terra", "model": "gpt-5.6-terra"}))
    monkeypatch.setattr(main, "controlled_edge_status", lambda: {"status": "未连接"})
    monkeypatch.setattr(main, "communication_policy", lambda: {"mode": "draft"})
    monkeypatch.setattr(main, "automation_control", lambda: {"status_label": "运行中"})
    client = TestClient(main.app)

    workspace = client.get("/agent")
    assert workspace.status_code == 200
    assert "求职agent" in workspace.text
    assert "岗位画布" in workspace.text

    boot = client.get("/api/agent/bootstrap")
    assert boot.status_code == 200
    session_id = boot.json()["conversation"]["session"]["id"]

    response = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"message": "看看有哪些岗位", "model_profile_id": 1, "auto_communication": False},
    )
    assert response.status_code == 200
    conversation = response.json()["conversation"]
    assistant = conversation["messages"][-1]
    assert "优先关注" in assistant["content"]
    assert any(item["kind"] == "工具调用" and item.get("tool") == "list_jobs" for item in assistant["events"])
    assert any(item["kind"] == "安全结论" for item in assistant["events"])
    assert conversation["session"]["summary"] == "已查看本地岗位机会。"

    select = client.post(f"/api/agent/sessions/{session_id}/active-job", json={"job_id": job_id})
    assert select.status_code == 200
    active_job = select.json()["conversation"]["state"]["active_job"]
    assert active_job["id"] == job_id
    assert active_job["required_skills"] == ["Python"]

    with connect() as conn:
        tool = conn.execute("SELECT tool_name, permission_level, status FROM agent_tool_runs ORDER BY id DESC LIMIT 1").fetchone()
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        preparation_count = conn.execute("SELECT COUNT(*) AS count FROM application_preparations").fetchone()["count"]
    assert tool["tool_name"] == "list_jobs"
    assert tool["permission_level"] == "本地只读"
    assert draft_count == 0
    assert preparation_count == 0


def test_desktop_agent_discards_unlisted_tool_and_never_submits(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "desktop-agent-deny.sqlite3"))
    from app import main
    from app.db import connect, init_db
    from app.services import desktop_agent

    init_db()

    class FakeClient:
        configured = True

        def complete_json(self, _messages):
            return {
                "plan": "尝试执行不允许的动作。",
                "tool_calls": [{"name": "submit_application", "arguments": {"url": "https://example.com"}}],
                "response": "该动作不在受限工具范围内，未执行。",
                "task_summary": "未执行提交动作。",
            }

    monkeypatch.setattr(desktop_agent, "load_client", lambda _profile_id: (FakeClient(), {"id": 1, "name": "Terra", "model": "gpt-5.6-terra"}))
    monkeypatch.setattr(main, "controlled_edge_status", lambda: {"status": "未连接"})
    monkeypatch.setattr(main, "communication_policy", lambda: {"mode": "draft"})
    monkeypatch.setattr(main, "automation_control", lambda: {"status_label": "运行中"})
    client = TestClient(main.app)
    session_id = client.get("/api/agent/bootstrap").json()["conversation"]["session"]["id"]

    response = client.post(f"/api/agent/sessions/{session_id}/messages", json={"message": "直接投递", "auto_communication": True})
    assert response.status_code == 200
    assistant = response.json()["conversation"]["messages"][-1]
    assert "未执行" in assistant["content"]
    assert not any(item["kind"] == "工具调用" for item in assistant["events"])
    with connect() as conn:
        tool_count = conn.execute("SELECT COUNT(*) AS count FROM agent_tool_runs").fetchone()["count"]
        preparation_count = conn.execute("SELECT COUNT(*) AS count FROM application_preparations").fetchone()["count"]
    assert tool_count == 0
    assert preparation_count == 0


def test_desktop_auto_toggle_authorizes_only_current_session_without_enabling_patrol(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "desktop-agent-auto-toggle.sqlite3"))
    from app import main
    from app.db import get_setting, init_db, set_setting, utc_now
    from app.services import desktop_agent

    init_db()
    set_setting("automation_control", {"paused": True, "pause_reason": "旧暂停", "updated_at": utc_now()})
    set_setting("message_patrol_policy", {"enabled": False, "interval_seconds": 300, "cooldown_seconds": 120})

    class FakeClient:
        configured = True

        def complete_json(self, _messages):
            return {"plan": "不调用工具。", "tool_calls": [], "response": "已记录自动沟通授权。", "task_summary": "已开启本会话自动沟通。"}

    monkeypatch.setattr(desktop_agent, "load_client", lambda _profile_id: (FakeClient(), {"id": 1, "name": "Terra", "model": "gpt-5.6-terra"}))
    client = TestClient(main.app)
    session_id = client.get("/api/agent/bootstrap").json()["conversation"]["session"]["id"]
    response = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"message": "开启自动沟通", "auto_communication": True},
    )

    assert response.status_code == 200
    assert response.json()["conversation"]["session"]["auto_communication"] is True
    assert get_setting("automation_control", {})["paused"] is False
    assert get_setting("message_patrol_policy", {})["enabled"] is False


def test_desktop_auto_communication_uses_first_contact_gate_and_never_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "desktop-agent-auto-contact.sqlite3"))
    from app import main
    from app.db import connect, init_db, set_setting, utc_now

    init_db()
    now = utc_now()
    set_setting("communication_policy", {"mode": "draft", "max_auto_followups": 2})
    set_setting("automation_control", {"paused": False, "pause_reason": "", "updated_at": now})
    with connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO job_postings (
                platform, source_url, title, company, city, jd_text, match_score,
                match_level, risk_level, recommendation, status, analysis_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Boss 直聘", "https://www.zhipin.com/job_detail/desktop-auto-contact.html", "AI Agent 开发实习生",
                "首聊验证科技", "杭州", "Python FastAPI RAG", 86,
                "高匹配", "低", "可冲", "待确认", "local_rules", now, now,
            ),
        ).lastrowid

    calls = []
    monkeypatch.setattr(main, "open_message_patrol_browser", lambda url: calls.append(("open", url)) or url)
    monkeypatch.setattr(
        main,
        "click_first_contact_in_controlled_edge",
        lambda url, platform: calls.append(("contact", url, platform)) or {"browser_clicked": True, "contact_selector": "button:has-text('立即沟通')"},
    )
    monkeypatch.setattr(
        main,
        "run_autonomous_draft_send",
        lambda draft_id, trigger, agent_authorized=False: calls.append(("send", draft_id, trigger, agent_authorized)) or {"status": "已发送", "note": "已发送岗位相关首聊。"},
    )
    monkeypatch.setattr(main.time, "sleep", lambda _seconds: None)

    result = main.run_desktop_autonomous_first_contact(int(job_id), session_id=8)

    assert result["status"] == "已发送"
    assert result["contact_clicked"] is True
    assert result["message_sent"] is True
    assert calls[0][0] == "open"
    assert calls[1][0] == "contact"
    assert calls[2][0] == "send" and calls[2][-1] is True
    with connect() as conn:
        draft = conn.execute(
            "SELECT draft_type, communication_mode, followup_index, followup_limit FROM message_drafts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        preparation_count = conn.execute("SELECT COUNT(*) AS count FROM application_preparations").fetchone()["count"]
    assert draft["draft_type"] == "自主询问候选"
    assert draft["communication_mode"] == "autonomous"
    assert draft["followup_index"] == 1
    assert draft["followup_limit"] == 2
    assert preparation_count == 0


def test_desktop_auto_communication_blocks_low_score_before_opening_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "desktop-agent-auto-contact-blocked.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    init_db()
    now = utc_now()
    with connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO job_postings (
                platform, source_url, title, company, jd_text, match_score,
                match_level, risk_level, recommendation, status, analysis_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Boss 直聘", "https://www.zhipin.com/job_detail/blocked.html", "AI 应用开发实习生", "低分验证科技", "Python", 63,
                "中匹配", "低", "可冲", "待确认", "local_rules", now, now,
            ),
        ).lastrowid

    opened = []
    monkeypatch.setattr(main, "open_message_patrol_browser", lambda *_args: opened.append(True))
    result = main.run_desktop_autonomous_first_contact(int(job_id), session_id=9)

    assert result["status"] == "已阻止"
    assert "匹配分低于主动沟通阈值" in result["note"]
    assert opened == []
