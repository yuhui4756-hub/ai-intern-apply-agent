from fastapi.testclient import TestClient


def test_unread_scan_route_persists_counts_and_audit_only(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "unread-scan.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads

    init_db()
    monkeypatch.setattr(
        main,
        "scan_controlled_edge_unread_conversations",
        lambda: {
            "status": "发现未读",
            "note": "已只读检查 1 个 Boss 直聘/猎聘页面，发现 2 个未读会话标记。",
            "checked_page_count": 1,
            "message_list_page_count": 1,
            "unread_count": 2,
            "error_count": 0,
            "detector_version": "message-list-v2",
            "results": [
                {
                    "platform": "Boss 直聘",
                    "status": "发现未读",
                    "message_list_candidate": True,
                    "unread_count": 2,
                    "unread_badge_count": 2,
                    "signal_types": ["badge", "unread"],
                }
            ],
        },
    )
    client = TestClient(main.app)

    response = client.post("/message-patrol/unread-scan", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/communications?notice=")
    with connect() as conn:
        scan = conn.execute("SELECT * FROM unread_conversation_scans").fetchone()
        action = conn.execute("SELECT * FROM agent_action_logs WHERE action_type = 'unread_conversation_scan'").fetchone()
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        model_call_count = conn.execute("SELECT COUNT(*) AS count FROM model_call_logs").fetchone()["count"]
    assert scan["platform"] == "Boss 直聘"
    assert scan["unread_count"] == 2
    assert scan["unread_badge_count"] == 2
    assert loads(scan["signal_types_json"], []) == ["badge", "unread"]
    assert "正文" in scan["note"]
    decision = loads(action["decision_json"], {})
    assert decision["conversation_opened"] is False
    assert decision["browser_clicked"] is False
    assert decision["message_sent"] is False
    assert "url" not in decision
    assert "title" not in decision
    assert "text" not in decision
    assert capture_count == 0
    assert draft_count == 0
    assert model_call_count == 0

    page = client.get("/communications")
    assert page.status_code == 200
    assert "未读候选会话" in page.text
    assert "message-list-v2" in page.text
