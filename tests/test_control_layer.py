from fastapi.testclient import TestClient


def create_memory_test_job(conn, now):
    resume_id = conn.execute(
        "INSERT INTO resume_versions (name, target_role, is_default, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("Agent 开发版", "Agent 开发实习", 1, now, now),
    ).lastrowid
    job_id = conn.execute(
        """
        INSERT INTO job_postings (
            platform, title, company, city, jd_text, selected_resume_id,
            match_score, match_level, risk_level, recommendation, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Boss 直聘", "AI Agent 开发实习生", "记忆测试科技", "杭州",
            "参与 Python、FastAPI 和 Agent 应用开发。", resume_id,
            86, "高匹配", "低", "必投", "待确认", now, now,
        ),
    ).lastrowid
    return int(job_id)


def test_control_layer_runs_non_destructive_search_from_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads

    init_db()
    called = []
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "run_controlled_job_discovery", lambda filters: called.append(filters) or {"status": "完成", "note": "测试"})
    client = TestClient(main.app)

    response = client.post("/control", data={"message": "找杭州 Agent 实习，日薪至少 200"}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/control")
    assert "Agent 对话" in page.text
    assert "已执行岗位发现" in page.text
    with connect() as conn:
        plan = conn.execute("SELECT * FROM control_plans").fetchone()
        conversation = conn.execute("SELECT * FROM control_conversations").fetchone()
        decision = loads(conn.execute("SELECT decision_json FROM agent_action_logs WHERE action_type = 'control_request'").fetchone()["decision_json"], {})
    assert plan["status"] == "已完成"
    assert loads(plan["payload_json"], {}) == {"role": "Agent 开发实习", "city": "杭州", "min_salary_per_day": 200}
    assert conversation["intent_type"] == "search_draft"
    assert called == [{"role": "Agent 开发实习", "city": "杭州", "min_salary_per_day": 200}]
    assert decision["execution_mode"] == "chat_direct_non_submitting"
    assert decision["auto_apply"] is False
    assert decision["auto_message"] is False


def test_control_message_api_persists_stats_turn_with_auditable_events(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-api.sqlite3"))
    from app.db import connect, init_db
    from app import main

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    response = TestClient(main.app).post("/api/control/messages", json={"message": "查看岗位统计"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    conversation = payload["conversation"]
    assert conversation["intent_type"] == "stats"
    assert conversation["evidence"]["parser"] == "local_rules"
    assert conversation["evidence"]["reasoning_summary"]
    assert {event["kind"] for event in conversation["evidence"]["events"]} >= {"判断摘要", "工具调用"}
    assert all(event["summary"] for event in conversation["evidence"]["events"])
    with connect() as conn:
        saved = conn.execute("SELECT user_text, response_text FROM control_conversations").fetchone()
    assert saved["user_text"] == "查看岗位统计"
    assert saved["response_text"] == conversation["response_text"]


def test_control_message_api_exposes_non_submitting_search_events(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-api-search.sqlite3"))
    from app import main
    from app.db import init_db

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "run_controlled_job_discovery", lambda filters: {"status": "完成", "note": "测试发现完成"})
    response = TestClient(main.app).post("/api/control/messages", json={"message": "找杭州 Agent 实习"})

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    events = conversation["evidence"]["events"]
    assert any(event["kind"] == "工具调用" and event["status"] == "执行中" for event in events)
    assert any(event["kind"] == "工具结果" and event["status"] == "已完成" for event in events)
    assert any(event["kind"] == "安全结论" and "未发送消息" in event["summary"] for event in events)


def test_control_message_api_rejects_non_object_or_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-api-error.sqlite3"))
    from app.db import init_db
    from app import main

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    client = TestClient(main.app)
    non_object = client.post("/api/control/messages", json=["查看岗位统计"])
    invalid_json = client.post("/api/control/messages", content="not-json", headers={"Content-Type": "application/json"})

    assert non_object.status_code == 400
    assert non_object.json() == {"ok": False, "error": "请求体必须是 JSON 对象。"}
    assert invalid_json.status_code == 400
    assert invalid_json.json() == {"ok": False, "error": "请求体必须是 JSON 对象。"}


def test_control_message_api_uses_valid_llm_intent_without_exposing_hidden_reasoning(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-api-model.sqlite3"))
    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True
        profile = {"name": "快速模型"}
        model = "fast-model"

        def __init__(self):
            self.messages = []

        def complete_json(self, messages):
            self.messages = messages
            return {"type": "stats", "filters": {}, "reason": "用户要求查看本地岗位统计。"}

    init_db()
    fake_client = FakeClient()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: fake_client if task_type == "control_intent" else None)
    response = TestClient(main.app).post("/api/control/messages", json={"message": "请汇总现在数据库里的职位数量 13800138000"})

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    assert conversation["intent_type"] == "stats"
    assert conversation["evidence"]["parser"] == "llm_json"
    assert conversation["evidence"]["model"] == {"profile": "快速模型", "name": "fast-model", "task_type": "control_intent"}
    assert any(event["kind"] == "模型调用" and event["status"] == "完成" for event in conversation["evidence"]["events"])
    assert "13800138000" not in fake_client.messages[-1]["content"]


def test_control_message_api_rejects_invalid_llm_intent_and_falls_back_to_local_rules(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-api-model-fallback.sqlite3"))
    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "send_message", "filters": {"message": "现在就发"}, "reason": "不应执行"}

        def log_error(self, _message):
            pass

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    response = TestClient(main.app).post("/api/control/messages", json={"message": "帮我直接处理一下"})

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"
    assert any(event["kind"] == "模型调用" and event["status"] == "已回退" for event in conversation["evidence"]["events"])


def test_control_message_api_rejects_extra_model_filter_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-api-model-extra-field.sqlite3"))
    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "stats", "filters": {"browser_action": "send"}, "reason": "不应执行"}

        def log_error(self, _message):
            pass

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    response = TestClient(main.app).post("/api/control/messages", json={"message": "帮我直接处理一下"})

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"


def test_control_message_api_rejects_model_selected_company_research(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-model-company-research.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "company_research", "filters": {"job_id": 1, "search_depth": "deep"}, "reason": "不应选择岗位"}

        def log_error(self, _message):
            pass

    calls = []
    init_db()
    with connect() as conn:
        create_memory_test_job(conn, utc_now())
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    monkeypatch.setattr(main, "search_company", lambda *args: calls.append(args) or [])
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "判断一下企业是否可靠"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"
    assert any(event["kind"] == "模型调用" and event["status"] == "已回退" for event in conversation["evidence"]["events"])
    assert calls == []


def test_control_message_api_rejects_model_selected_match_review(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-model-match-review.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "job_match_review", "filters": {"job_id": 1}, "reason": "不应选择岗位"}

        def log_error(self, _message):
            pass

    calls = []
    init_db()
    with connect() as conn:
        create_memory_test_job(conn, utc_now())
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    monkeypatch.setattr(main, "run_job_match_review", lambda job_id: calls.append(job_id) or {})
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "帮我判断这个机会是否合适"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"
    assert calls == []


def test_control_message_api_rejects_model_selected_job_comparison(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-model-compare.sqlite3"))
    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "compare_jobs", "filters": {"job_ids": [1, 2]}, "reason": "不应选择岗位"}

        def log_error(self, _message):
            pass

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "帮我在这两个机会中选一个"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"


def test_control_message_api_rejects_model_selected_job_list(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-model-list.sqlite3"))
    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "list_jobs", "filters": {"risk_level": "低"}, "reason": "不应选择筛选条件"}

        def log_error(self, _message):
            pass

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "帮我整理一下现在的机会"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"


def test_control_message_api_rejects_model_selected_interview_preparation(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-model-interview.sqlite3"))
    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "prepare_interview", "filters": {"job_id": 1}, "reason": "不应选择岗位"}

        def log_error(self, _message):
            pass

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "帮我开始准备一下"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"


def test_control_message_api_rejects_model_selected_job_status_update(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-model-status.sqlite3"))
    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True
        profile = {"name": "不合规模型"}
        model = "unsafe-model"

        def complete_json(self, _messages):
            return {"type": "update_job_status", "filters": {"job_id": 1, "status": "待投递"}, "reason": "不应选择岗位"}

        def log_error(self, _message):
            pass

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "control_intent" else None)
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "帮我改一下求职流程"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert conversation["evidence"]["parser"] == "local_rules"


def test_control_memory_requires_explicit_job_selection_and_resolves_current_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-memory.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    init_db()
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    client = TestClient(main.app)

    missing = client.post("/api/control/messages", json={"message": "当前岗位怎么样"}).json()["conversation"]
    selected = client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"}).json()["conversation"]
    current = client.post("/api/control/messages", json={"message": "当前岗位怎么样"}).json()["conversation"]

    assert missing["intent_type"] == "help"
    assert "还没有当前岗位" in missing["response_text"]
    assert selected["intent_type"] == "select_job"
    assert selected["memory"]["active_job"]["id"] == job_id
    assert current["intent_type"] == "explain_job"
    assert "记忆测试科技" in current["response_text"]
    assert current["memory"]["active_job"]["id"] == job_id


def test_control_memory_can_save_preference_and_prepare_selected_job_locally(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-memory-prepare.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    init_db()
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    client = TestClient(main.app)

    selected = client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    remembered = client.post("/api/control/messages", json={"message": "记住：不考虑驻场岗位"}).json()["conversation"]
    prepared = client.post("/api/control/messages", json={"message": "为当前岗位创建投递准备"}).json()["conversation"]

    assert selected.status_code == 200
    assert remembered["intent_type"] == "remember_preference"
    assert remembered["memory"]["preferences"][0]["content"] == "不考虑驻场岗位"
    assert prepared["intent_type"] == "prepare_application"
    assert "未打开平台、未上传简历、未点击投递" in " ".join(event["summary"] for event in prepared["evidence"]["events"])
    with connect() as conn:
        preparation = conn.execute("SELECT status FROM application_preparations WHERE job_id = ?", (job_id,)).fetchone()
    assert preparation["status"] == "待确认"


def test_control_company_research_requires_an_explicit_current_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-company-research-missing.sqlite3"))
    from app import main
    from app.db import init_db

    called = []
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "search_company", lambda *args: called.append(args) or [])
    init_db()
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "查当前岗位的公司风险"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert "还没有当前岗位" in conversation["response_text"]
    assert called == []


def test_control_company_research_persists_public_sources_without_platform_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-company-research.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now
    from app.services.research import SearchResult

    calls = []

    def fake_search(company, title, city, depth):
        calls.append((company, title, city, depth))
        return [SearchResult(title="企业公开资料", url="https://example.test/company", summary="公开查询摘要")]

    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "search_company", fake_search)
    init_db()
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    conversation = client.post(
        "/api/control/messages", json={"message": "深度查询当前岗位的公司风险"}
    ).json()["conversation"]
    with connect() as conn:
        source = conn.execute(
            "SELECT source_title, source_url, summary FROM company_research WHERE job_id = ?", (job_id,)
        ).fetchone()
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'company_risk_research'"
        ).fetchone()
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]

    assert conversation["intent_type"] == "company_research"
    assert conversation["evidence"]["execution"]["mode"] == "chat_direct_public_research"
    assert calls == [("记忆测试科技", "AI Agent 开发实习生", "杭州", "deep")]
    assert source["source_title"] == "企业公开资料"
    assert source["source_url"] == "https://example.test/company"
    decision = loads(action["decision_json"], {})
    assert decision["recruitment_platform_accessed"] is False
    assert draft_count == 0
    assert "未访问招聘平台" in conversation["response_text"] or any(
        "未访问招聘平台" in event["summary"] for event in conversation["evidence"]["events"]
    )


def test_control_company_research_blocks_missing_company_without_network_call(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-company-research-no-company.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    called = []
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "search_company", lambda *args: called.append(args) or [])
    init_db()
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
        conn.execute("UPDATE job_postings SET company = '' WHERE id = ?", (job_id,))
    client = TestClient(main.app)
    conversation = client.post(
        "/api/control/messages", json={"message": f"查询岗位 #{job_id} 的公司风险"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "company_research"
    assert "公司名称为空" in conversation["response_text"]
    assert called == []


def test_control_match_review_requires_an_explicit_current_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-match-review-missing.sqlite3"))
    from app import main
    from app.db import init_db

    called = []
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "run_job_match_review", lambda job_id: called.append(job_id) or {})
    init_db()
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "为当前岗位做深度匹配复核"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert "还没有当前岗位" in conversation["response_text"]
    assert called == []


def test_control_match_review_runs_for_selected_job_without_browser_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-match-review.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    calls = []

    def fake_review(job_id):
        calls.append(job_id)
        return {
            "status": "已完成",
            "note": "已生成补充性深度匹配复核，本地评分和岗位状态未改变。",
            "model_called": True,
            "model_profile": "复核模型",
            "model_name": "review-model",
            "content": {
                "conclusion": "岗位方向与已确认的 RAG 项目实践存在交集。",
                "gaps": ["FastAPI 项目经历需要如实确认。"],
                "questions_to_confirm": ["团队当前 Agent 工作流主要解决什么问题？"],
                "model_fields": ["conclusion", "gaps", "questions_to_confirm"],
            },
        }

    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "run_job_match_review", fake_review)
    init_db()
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    conversation = client.post(
        "/api/control/messages", json={"message": "为当前岗位做深度匹配复核"}
    ).json()["conversation"]
    with connect() as conn:
        job = conn.execute(
            "SELECT match_score, recommendation, risk_level, status FROM job_postings WHERE id = ?", (job_id,)
        ).fetchone()
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'control_request' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]

    assert conversation["intent_type"] == "job_match_review"
    assert conversation["evidence"]["execution"]["mode"] == "chat_direct_model_review"
    assert calls == [job_id]
    assert "岗位方向与已确认的 RAG 项目实践存在交集" in conversation["response_text"]
    assert tuple(job) == (86, "必投", "低", "待确认")
    decision = loads(action["decision_json"], {})
    assert decision["browser_accessed"] is False
    assert decision["job_score_changed"] is False
    assert decision["job_status_changed"] is False
    assert draft_count == 0


def test_control_compares_two_jobs_without_model_or_workflow_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-compare.sqlite3"))
    from app import main
    from app.db import connect, dumps, init_db, loads, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        first_job_id = create_memory_test_job(conn, utc_now())
        first_resume_id = conn.execute("SELECT selected_resume_id FROM job_postings WHERE id = ?", (first_job_id,)).fetchone()["selected_resume_id"]
        conn.execute(
            "UPDATE job_postings SET salary_text = ?, extracted_json = ? WHERE id = ?",
            ("250-350 元/天", dumps({"scoring": {"matched_skills": ["Python", "FastAPI"], "missing_skills": ["Docker"]}}), first_job_id),
        )
        second_job_id = conn.execute(
            """
            INSERT INTO job_postings (
                platform, title, company, city, salary_text, jd_text, selected_resume_id,
                extracted_json, match_score, match_level, risk_level, recommendation, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "猎聘", "RAG 开发实习生", "比较测试科技", "杭州", "200-260 元/天", "参与 RAG 应用开发。", first_resume_id,
                dumps({"scoring": {"matched_skills": ["Python"], "missing_skills": ["向量数据库", "Docker"]}}),
                79, "中匹配", "低", "可冲", "待确认", utc_now(), utc_now(),
            ),
        ).lastrowid

    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": f"比较岗位 #{first_job_id} 和岗位 #{second_job_id}"}
    ).json()["conversation"]
    with connect() as conn:
        job_rows = conn.execute(
            "SELECT id, match_score, recommendation, risk_level, status FROM job_postings WHERE id IN (?, ?) ORDER BY id",
            (first_job_id, second_job_id),
        ).fetchall()
        plan = conn.execute("SELECT * FROM control_plans ORDER BY id DESC LIMIT 1").fetchone()
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'control_request' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]

    assert conversation["intent_type"] == "compare_jobs"
    assert conversation["evidence"]["parser"] == "local_rules"
    assert conversation["evidence"]["execution"]["mode"] == "chat_direct_local_comparison"
    assert conversation["evidence"]["execution"]["result"]["preferred_job_id"] == first_job_id
    assert f"建议优先处理岗位 #{first_job_id}" in conversation["response_text"]
    assert "250-350 元/天" in conversation["response_text"]
    assert [tuple(row) for row in job_rows] == [
        (first_job_id, 86, "必投", "低", "待确认"),
        (second_job_id, 79, "可冲", "低", "待确认"),
    ]
    assert plan["action_type"] == "compare_jobs"
    assert plan["status"] == "已完成"
    decision = loads(action["decision_json"], {})
    assert decision["model_called"] is False
    assert decision["browser_accessed"] is False
    assert decision["job_score_changed"] is False
    assert decision["job_status_changed"] is False
    assert draft_count == 0


def test_control_compares_current_job_with_explicit_other_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-compare-current.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        active_job_id = create_memory_test_job(conn, utc_now())
        resume_id = conn.execute("SELECT selected_resume_id FROM job_postings WHERE id = ?", (active_job_id,)).fetchone()["selected_resume_id"]
        other_job_id = conn.execute(
            """
            INSERT INTO job_postings (
                platform, title, company, city, jd_text, selected_resume_id,
                match_score, match_level, risk_level, recommendation, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Boss 直聘", "AI 应用开发实习生", "当前比较测试", "杭州", "参与 RAG 应用开发。", resume_id,
                76, "中匹配", "低", "可冲", "待确认", utc_now(), utc_now(),
            ),
        ).lastrowid

    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{active_job_id}"})
    conversation = client.post(
        "/api/control/messages", json={"message": f"比较当前岗位和岗位 #{other_job_id}"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "compare_jobs"
    assert conversation["evidence"]["parser"] == "local_rules"
    assert conversation["evidence"]["execution"]["result"]["preferred_job_id"] == active_job_id
    assert f"建议优先处理岗位 #{active_job_id}" in conversation["response_text"]
    assert conversation["memory"]["active_job"]["id"] == active_job_id


def test_control_comparison_reports_missing_job_without_model_or_browser(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-compare-missing.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())

    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": f"比较岗位 #{job_id} 和岗位 #999"}
    ).json()["conversation"]
    with connect() as conn:
        plan = conn.execute("SELECT * FROM control_plans ORDER BY id DESC LIMIT 1").fetchone()
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'control_request' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert conversation["intent_type"] == "compare_jobs"
    assert conversation["evidence"]["execution"]["status"] == "未找到"
    assert "没有找到岗位 #999" in conversation["response_text"]
    assert plan["status"] == "未找到"
    decision = loads(action["decision_json"], {})
    assert decision["model_called"] is False
    assert decision["browser_accessed"] is False


def test_control_lists_high_match_low_risk_jobs_without_workflow_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-job-list.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        eligible_job_id = create_memory_test_job(conn, utc_now())
        high_risk_job_id = create_memory_test_job(conn, utc_now())
        medium_match_job_id = create_memory_test_job(conn, utc_now())
        conn.execute("UPDATE job_postings SET salary_text = ? WHERE id = ?", ("250-350 元/天", eligible_job_id))
        conn.execute("UPDATE job_postings SET match_score = ?, risk_level = ? WHERE id = ?", (98, "高", high_risk_job_id))
        conn.execute("UPDATE job_postings SET match_score = ?, match_level = ? WHERE id = ?", (76, "中匹配", medium_match_job_id))

    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "列出高匹配低风险岗位"}
    ).json()["conversation"]
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, match_score, match_level, risk_level, recommendation, status FROM job_postings ORDER BY id"
        ).fetchall()
        plan = conn.execute("SELECT * FROM control_plans ORDER BY id DESC LIMIT 1").fetchone()
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'control_request' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]

    assert conversation["intent_type"] == "list_jobs"
    assert conversation["evidence"]["parser"] == "local_rules"
    result = conversation["evidence"]["execution"]["result"]
    assert result["returned_job_ids"] == [eligible_job_id]
    assert result["returned_count"] == 1
    assert f"#{eligible_job_id}" in conversation["response_text"]
    assert f"#{high_risk_job_id}" not in conversation["response_text"]
    assert f"#{medium_match_job_id}" not in conversation["response_text"]
    assert [tuple(row) for row in rows] == [
        (eligible_job_id, 86, "高匹配", "低", "必投", "待确认"),
        (high_risk_job_id, 98, "高匹配", "高", "必投", "待确认"),
        (medium_match_job_id, 76, "中匹配", "低", "必投", "待确认"),
    ]
    assert plan["action_type"] == "list_jobs"
    assert plan["status"] == "已完成"
    decision = loads(action["decision_json"], {})
    assert decision["model_called"] is False
    assert decision["browser_accessed"] is False
    assert decision["job_score_changed"] is False
    assert decision["job_status_changed"] is False
    assert draft_count == 0


def test_control_job_list_reports_empty_result_and_keeps_explicit_job_explanation(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-job-list-empty.sqlite3"))
    from app import main
    from app.db import init_db
    from app.services.control_layer import parse_control_intent

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "看看高匹配岗位"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "list_jobs"
    assert conversation["evidence"]["execution"]["result"]["returned_count"] == 0
    assert "本地没有符合" in conversation["response_text"]
    assert parse_control_intent("查看岗位 #12") == {"type": "explain_job", "filters": {"job_id": 12}}


def test_control_interview_preparation_waits_for_manual_interview_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-interview-wait.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
        conn.execute("UPDATE job_postings SET status = ? WHERE id = ?", ("面试邀请", job_id))

    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    conversation = client.post(
        "/api/control/messages", json={"message": "为当前岗位准备面试"}
    ).json()["conversation"]
    with connect() as conn:
        job = conn.execute("SELECT status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        prep_count = conn.execute("SELECT COUNT(*) AS count FROM interview_preparations WHERE job_id = ?", (job_id,)).fetchone()["count"]
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'control_request' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert conversation["intent_type"] == "prepare_interview"
    assert conversation["evidence"]["execution"]["status"] == "等待人工确认"
    assert "人工确认面试时间、形式和流程" in conversation["response_text"]
    assert job["status"] == "面试邀请"
    assert prep_count == 0
    decision = loads(action["decision_json"], {})
    assert decision["model_called"] is False
    assert decision["browser_accessed"] is False
    assert decision["job_status_changed"] is False
    assert decision["interview_time_confirmed"] is False


def test_control_interview_preparation_creates_once_for_confirmed_current_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-interview-ready.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
        conn.execute("UPDATE job_postings SET status = ? WHERE id = ?", ("待面试", job_id))

    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    created = client.post(
        "/api/control/messages", json={"message": "为当前岗位准备面试"}
    ).json()["conversation"]
    existing = client.post(
        "/api/control/messages", json={"message": "为当前岗位准备面试"}
    ).json()["conversation"]
    with connect() as conn:
        job = conn.execute("SELECT status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        preps = conn.execute("SELECT id, job_id FROM interview_preparations WHERE job_id = ?", (job_id,)).fetchall()
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'control_request' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    created_result = created["evidence"]["execution"]["result"]
    existing_result = existing["evidence"]["execution"]["result"]
    assert created["intent_type"] == "prepare_interview"
    assert created_result["created"] is True
    assert created_result["interview_id"] == preps[0]["id"]
    assert existing_result["created"] is False
    assert existing_result["interview_id"] == preps[0]["id"]
    assert len(preps) == 1
    assert job["status"] == "待面试"
    decision = loads(action["decision_json"], {})
    assert decision["model_called"] is False
    assert decision["browser_accessed"] is False
    assert decision["job_status_changed"] is False
    assert decision["interview_time_confirmed"] is False


def test_control_status_update_waits_for_confirmation_then_creates_interview_prep(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-status-confirm.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
        conn.execute("UPDATE job_postings SET status = ? WHERE id = ?", ("面试邀请", job_id))

    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    conversation = client.post(
        "/api/control/messages", json={"message": "将当前岗位标记为待面试"}
    ).json()["conversation"]
    with connect() as conn:
        before_job = conn.execute("SELECT status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        plan = conn.execute("SELECT * FROM control_plans ORDER BY id DESC LIMIT 1").fetchone()
        prep_count = conn.execute("SELECT COUNT(*) AS count FROM interview_preparations WHERE job_id = ?", (job_id,)).fetchone()["count"]

    assert conversation["intent_type"] == "update_job_status"
    assert conversation["evidence"]["plan_id"] == plan["id"]
    assert before_job["status"] == "面试邀请"
    assert prep_count == 0
    assert plan["status"] == "待确认"
    assert loads(plan["payload_json"], {}) == {"job_id": job_id, "source_status": "面试邀请", "target_status": "待面试"}

    confirmed = client.post(f"/control/plans/{plan['id']}/confirm", follow_redirects=False)
    assert confirmed.status_code == 303
    with connect() as conn:
        after_job = conn.execute("SELECT status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        completed_plan = conn.execute("SELECT * FROM control_plans WHERE id = ?", (plan["id"],)).fetchone()
        preps = conn.execute("SELECT id FROM interview_preparations WHERE job_id = ?", (job_id,)).fetchall()
        action = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'job_status_update' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert after_job["status"] == "待面试"
    assert completed_plan["status"] == "已完成"
    assert len(preps) == 1
    result = loads(completed_plan["result_json"], {})
    assert result["previous_status"] == "面试邀请"
    assert result["status"] == "待面试"
    assert result["interview_preparation"]["created"] is True
    decision = loads(action["decision_json"], {})
    assert decision["user_confirmed"] is True
    assert decision["model_called"] is False
    assert decision["browser_accessed"] is False
    assert decision["external_effect"] is False


def test_control_status_update_rejects_stale_plan_without_overwriting_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-status-stale.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())

    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    client.post("/api/control/messages", json={"message": "将当前岗位标记为已沟通"})
    with connect() as conn:
        plan = conn.execute("SELECT * FROM control_plans ORDER BY id DESC LIMIT 1").fetchone()
        conn.execute("UPDATE job_postings SET status = ? WHERE id = ?", ("待投递", job_id))

    response = client.post(f"/control/plans/{plan['id']}/confirm", follow_redirects=False)
    assert response.status_code == 303
    with connect() as conn:
        job = conn.execute("SELECT status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        failed_plan = conn.execute("SELECT * FROM control_plans WHERE id = ?", (plan["id"],)).fetchone()

    assert job["status"] == "待投递"
    assert failed_plan["status"] == "失败"
    assert "未覆盖" in loads(failed_plan["result_json"], {})["error"]


def test_control_communication_preparation_requires_an_explicit_current_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-communication-missing.sqlite3"))
    from app import main
    from app.db import init_db

    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    init_db()
    conversation = TestClient(main.app).post(
        "/api/control/messages", json={"message": "为当前岗位准备沟通"}
    ).json()["conversation"]

    assert conversation["intent_type"] == "help"
    assert "还没有当前岗位" in conversation["response_text"]
    assert conversation["evidence"]["events"][0]["kind"] == "工作记忆"


def test_control_communication_preparation_rejects_low_match_or_high_risk_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-communication-ineligible.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    init_db()
    with connect() as conn:
        low_match_job_id = create_memory_test_job(conn, utc_now())
        high_risk_job_id = create_memory_test_job(conn, utc_now())
        conn.execute("UPDATE job_postings SET recommendation = ? WHERE id = ?", ("跳过", low_match_job_id))
        conn.execute("UPDATE job_postings SET risk_level = ? WHERE id = ?", ("高", high_risk_job_id))
    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{low_match_job_id}"})
    low_match = client.post("/api/control/messages", json={"message": "为当前岗位准备沟通"}).json()["conversation"]
    high_risk = client.post(
        "/api/control/messages", json={"message": f"为岗位 #{high_risk_job_id} 准备沟通"}
    ).json()["conversation"]
    with connect() as conn:
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]

    assert low_match["intent_type"] == "prepare_communication"
    assert "暂未生成" in low_match["response_text"]
    assert high_risk["intent_type"] == "prepare_communication"
    assert "风险不是“低/低风险”" in high_risk["response_text"]
    assert draft_count == 0


def test_control_communication_preparation_creates_local_draft_without_browser_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-communication.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    calls = []
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    monkeypatch.setattr(main, "fill_message_in_controlled_edge", lambda *args, **kwargs: calls.append("fill"))
    monkeypatch.setattr(main, "send_message_in_controlled_edge", lambda *args, **kwargs: calls.append("send"))
    init_db()
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    conversation = client.post("/api/control/messages", json={"message": "为当前岗位准备沟通"}).json()["conversation"]
    with connect() as conn:
        draft = conn.execute(
            "SELECT job_id, platform, draft_type, status, communication_mode, message, reason FROM message_drafts"
        ).fetchone()
        log = conn.execute(
            "SELECT decision_json FROM agent_action_logs WHERE action_type = 'communication_preparation'"
        ).fetchone()
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]

    assert conversation["intent_type"] == "prepare_communication"
    assert conversation["evidence"]["execution"]["mode"] == "chat_direct_local_preparation"
    assert draft["job_id"] == job_id
    assert draft["draft_type"] == "聊天沟通准备"
    assert draft["status"] == "待确认"
    assert draft["communication_mode"] == "draft"
    assert "主要工作内容" in draft["message"]
    assert "不来自真实 HR 对话" in draft["reason"]
    assert calls == []
    assert capture_count == 0
    assert loads(log["decision_json"], {})["message_sent"] is False


def test_control_communication_preparation_marks_unsupported_platform_manual_only(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-communication-platform.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    init_db()
    with connect() as conn:
        job_id = create_memory_test_job(conn, utc_now())
        conn.execute("UPDATE job_postings SET platform = ? WHERE id = ?", ("实习僧", job_id))
    client = TestClient(main.app)
    client.post("/api/control/messages", json={"message": f"选择岗位 #{job_id}"})
    conversation = client.post("/api/control/messages", json={"message": "为当前岗位准备沟通"}).json()["conversation"]

    result = conversation["evidence"]["execution"]["result"]
    assert result["platform_capability"] == "manual_only"
    assert "移动端人工处理" in result["platform_note"]
    assert "实习僧 PC 端" in conversation["response_text"]


def test_control_memory_preferences_can_be_updated_and_deleted_from_page(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-memory-edit.sqlite3"))
    from app import main
    from app.db import connect, init_db

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    client = TestClient(main.app)
    created = client.post("/api/control/messages", json={"message": "记住：优先考虑成长性"}).json()["conversation"]
    memory_id = created["memory"]["preferences"][0]["id"]

    updated = client.post(f"/control/memories/{memory_id}/update", data={"content": "优先考虑工程成长性"}, follow_redirects=False)
    with connect() as conn:
        saved = conn.execute("SELECT value_json FROM control_memories WHERE id = ?", (memory_id,)).fetchone()
    deleted = client.post(f"/control/memories/{memory_id}/delete", follow_redirects=False)
    with connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) AS count FROM control_memories WHERE id = ?", (memory_id,)).fetchone()["count"]

    assert updated.status_code == 303
    assert "工程成长性" in saved["value_json"]
    assert deleted.status_code == 303
    assert remaining == 0


def test_control_memory_rejects_sensitive_preference_content(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-memory-sensitive.sqlite3"))
    from app import main
    from app.db import connect, init_db

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task_type: None)
    response = TestClient(main.app).post("/api/control/messages", json={"message": "记住：API Key=sk-abcdefghijklmnop"})

    assert response.status_code == 200
    conversation = response.json()["conversation"]
    assert "未保存" in conversation["response_text"]
    assert any(event["status"] == "已拦截" for event in conversation["evidence"]["events"])
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM control_memories").fetchone()["count"]
    assert count == 0


def test_control_layer_marks_broadcast_only_after_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control-ignore.sqlite3"))
    from app.db import connect, init_db, utc_now
    from app.main import app

    init_db()
    with connect() as conn:
        capture_id = conn.execute(
            "INSERT INTO conversation_captures (platform, message_type, created_at) VALUES (?, ?, ?)",
            ("Boss 直聘", "无需回复", utc_now()),
        ).lastrowid
    client = TestClient(app)
    client.post("/control", data={"message": f"将对话 #{capture_id} 的群发消息标为忽略"})
    with connect() as conn:
        plan = conn.execute("SELECT id FROM control_plans").fetchone()
        before = conn.execute("SELECT feedback_status FROM conversation_captures WHERE id = ?", (capture_id,)).fetchone()
    assert before["feedback_status"] == ""
    client.post(f"/control/plans/{plan['id']}/confirm")
    with connect() as conn:
        after = conn.execute("SELECT feedback_status, expected_message_type FROM conversation_captures WHERE id = ?", (capture_id,)).fetchone()
    assert after["feedback_status"] == "正确"
    assert after["expected_message_type"] == "无需回复"
