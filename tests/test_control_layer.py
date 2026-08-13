from fastapi.testclient import TestClient


def test_control_layer_generates_and_confirms_search_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "control.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads

    init_db()
    called = []
    monkeypatch.setattr(main, "run_controlled_job_discovery", lambda filters: called.append(filters) or {"status": "完成", "note": "测试"})
    client = TestClient(main.app)

    response = client.post("/control", data={"message": "找杭州 Agent 实习，日薪至少 200"}, follow_redirects=False)
    assert response.status_code == 303
    page = client.get("/control")
    assert "受控 Edge" in page.text
    with connect() as conn:
        plan = conn.execute("SELECT * FROM control_plans").fetchone()
        conversation = conn.execute("SELECT * FROM control_conversations").fetchone()
    assert plan["status"] == "待确认"
    assert loads(plan["payload_json"], {}) == {"role": "Agent 开发实习", "city": "杭州", "min_salary_per_day": 200}
    assert conversation["intent_type"] == "search_draft"

    confirmed = client.post(f"/control/plans/{plan['id']}/confirm", follow_redirects=False)
    assert confirmed.status_code == 303
    assert called == [{"role": "Agent 开发实习", "city": "杭州", "min_salary_per_day": 200}]
    with connect() as conn:
        completed = conn.execute("SELECT status FROM control_plans WHERE id = ?", (plan["id"],)).fetchone()
    assert completed["status"] == "已完成"


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
