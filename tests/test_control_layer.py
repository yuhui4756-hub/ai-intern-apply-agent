from fastapi.testclient import TestClient


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
