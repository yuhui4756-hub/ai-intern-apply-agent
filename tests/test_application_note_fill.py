from fastapi.testclient import TestClient

from app.services import application_browser


def application_plan() -> dict[str, object]:
    return {
        "browser_action": "dry_run_ready",
        "platform": "Boss 直聘",
        "company": "测试智能科技",
        "job_title": "AI Agent 开发实习生",
        "source_url": "https://www.zhipin.com/job_detail/application-note.html",
        "page_match": {"domains": ["zhipin.com"], "source_url_host": "www.zhipin.com"},
        "selector_candidates": {
            "application_button": ["button:has-text('立即投递')"],
            "resume_control": ["input[type='file']"],
            "application_note": ["textarea[placeholder*='附言']"],
        },
    }


def application_snapshot(url: str, extra_text: str = "") -> dict[str, object]:
    text = f"测试智能科技 AI Agent 开发实习生 投递附言 {extra_text}"
    return {
        "url": url,
        "title": "AI Agent 开发实习生 - 测试智能科技",
        "host": "www.zhipin.com",
        "text_length": len(text),
        "text_digest": application_browser.text_digest(text),
        "normalized_text": application_browser.normalize_text(text),
        "selectors": {
            "button:has-text('立即投递')": 1,
            "input[type='file']": 0,
            "textarea[placeholder*='附言']": 1,
        },
    }


def test_application_note_fill_requires_one_verified_safe_target(monkeypatch):
    target = {
        "id": "application-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/application-note.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/application-page",
    }
    expressions = []

    monkeypatch.setattr(application_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(application_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        application_browser,
        "capture_application_target_snapshot",
        lambda *_args: application_snapshot(target["url"]),
    )
    monkeypatch.setattr(
        application_browser,
        "evaluate_cdp_expression",
        lambda _target, expression: expressions.append(expression) or {"ok": True, "selector": "textarea[placeholder*='附言']"},
    )

    result = application_browser.fill_application_note_in_controlled_edge(application_plan(), "您好，我对岗位很感兴趣。")

    assert result["status"] == "已填入"
    assert result["browser_clicked"] is False
    assert result["resume_uploaded"] is False
    assert len(expressions) == 1
    assert "const message" in expressions[0]
    assert "button.click" not in expressions[0]
    assert "input[type='file']" not in expressions[0]


def test_application_note_fill_stops_on_sensitive_page(monkeypatch):
    target = {
        "id": "application-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/application-note.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/application-page",
    }
    actions = []

    monkeypatch.setattr(application_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(application_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        application_browser,
        "capture_application_target_snapshot",
        lambda *_args: application_snapshot(target["url"], "请完成验证码验证"),
    )
    monkeypatch.setattr(application_browser, "evaluate_cdp_expression", lambda *_args: actions.append(True))

    try:
        application_browser.fill_application_note_in_controlled_edge(application_plan(), "您好，我对岗位很感兴趣。")
    except ValueError as exc:
        assert "未找到身份匹配" in str(exc)
    else:
        raise AssertionError("验证码页面不能进入投递附言填入")

    assert not actions


def test_application_note_fill_stops_when_multiple_pages_match(monkeypatch):
    targets = [
        {
            "id": "application-page-one",
            "type": "page",
            "url": "https://www.zhipin.com/job_detail/application-note-one.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/application-page-one",
        },
        {
            "id": "application-page-two",
            "type": "page",
            "url": "https://www.zhipin.com/job_detail/application-note-two.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/application-page-two",
        },
    ]
    actions = []

    monkeypatch.setattr(application_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(application_browser, "read_controlled_edge_targets", lambda: targets)
    monkeypatch.setattr(
        application_browser,
        "capture_application_target_snapshot",
        lambda target, _plan: application_snapshot(target["url"]),
    )
    monkeypatch.setattr(application_browser, "evaluate_cdp_expression", lambda *_args: actions.append(True))

    try:
        application_browser.fill_application_note_in_controlled_edge(application_plan(), "您好，我对岗位很感兴趣。")
    except ValueError as exc:
        assert "多个可填写的投递页面" in str(exc)
    else:
        raise AssertionError("多个候选页面时不能填入投递附言")

    assert not actions


def test_application_note_fill_route_requires_confirmation_and_audits_without_message(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "application-note-fill.sqlite3"))

    from app import main
    from app.db import connect, init_db, utc_now

    init_db()
    client = TestClient(main.app)
    now = utc_now()
    message = "您好，我对 AI Agent 开发实习很感兴趣，期待进一步沟通。"
    with connect() as conn:
        resume_id = conn.execute("SELECT id FROM resume_versions ORDER BY id LIMIT 1").fetchone()["id"]
        job_id = int(
            conn.execute(
                """
                INSERT INTO job_postings (
                    platform, source_url, title, company, jd_text, risk_level, recommendation,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Boss 直聘",
                    "https://www.zhipin.com/job_detail/application-note.html",
                    "AI Agent 开发实习生",
                    "测试智能科技",
                    "Python、FastAPI",
                    "低",
                    "必投",
                    "待投递",
                    now,
                    now,
                ),
            ).lastrowid
        )
        preparation_id = int(
            conn.execute(
                """
                INSERT INTO application_preparations (
                    job_id, resume_id, application_message, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, resume_id, message, "已确认", now, now),
            ).lastrowid
        )

    original_fill = main.run_application_browser_note_fill
    invoked = []
    monkeypatch.setattr(main, "run_application_browser_note_fill", lambda _id: invoked.append(_id) or {"status": "已填入", "note": "测试填入"})
    denied = client.post(
        f"/applications/{preparation_id}/browser-fill-note",
        data={"confirmation": "错误确认", "return_to": "/applications"},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert not invoked

    confirmed = client.post(
        f"/applications/{preparation_id}/browser-fill-note",
        data={"confirmation": "填入投递附言", "return_to": "/applications"},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert invoked == [preparation_id]
    monkeypatch.setattr(main, "run_application_browser_note_fill", original_fill)

    listing = client.get("/applications")
    assert listing.status_code == 200
    assert "投递附言（可选）" in listing.text
    assert f'action="/applications/{preparation_id}/browser-fill-note"' in listing.text
    assert "确认短语" in listing.text

    def fake_fill(_plan, _message):
        return {"status": "已填入", "note": "已填入当前 Edge 的投递附言，未上传简历、未点击投递或提交。", "filled_selector": "textarea", "application_message_filled": True}

    monkeypatch.setattr(main, "fill_application_note_in_controlled_edge", fake_fill)
    result = main.run_application_browser_note_fill(preparation_id)

    assert result["status"] == "已填入"
    with connect() as conn:
        log = conn.execute("SELECT action_type, status, decision_json, summary FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert log["action_type"] == "application_browser_fill"
    assert log["status"] == "已填入"
    assert '"application_message_length"' in log["decision_json"]
    assert message not in log["decision_json"]
    assert message not in log["summary"]
