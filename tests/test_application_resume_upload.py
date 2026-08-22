from pathlib import Path

from fastapi.testclient import TestClient

from app.services import application_browser


def application_plan() -> dict[str, object]:
    return {
        "browser_action": "dry_run_ready",
        "platform": "Boss 直聘",
        "company": "测试智能科技",
        "job_title": "AI Agent 开发实习生",
        "source_url": "https://www.zhipin.com/job_detail/application-resume.html",
        "page_match": {"domains": ["zhipin.com"], "source_url_host": "www.zhipin.com"},
        "selector_candidates": {
            "application_button": ["button:has-text('立即投递')"],
            "resume_control": ["input[type='file']"],
            "application_note": ["textarea[placeholder*='附言']"],
        },
    }


def application_snapshot(url: str, extra_text: str = "") -> dict[str, object]:
    text = f"测试智能科技 AI Agent 开发实习生 投递简历 {extra_text}"
    return {
        "url": url,
        "title": "AI Agent 开发实习生 - 测试智能科技",
        "host": "www.zhipin.com",
        "text_length": len(text),
        "text_digest": application_browser.text_digest(text),
        "normalized_text": application_browser.normalize_text(text),
        "selectors": {
            "button:has-text('立即投递')": 1,
            "input[type='file']": 1,
            "textarea[placeholder*='附言']": 0,
        },
    }


def test_application_resume_selection_uses_unique_file_input_without_clicking(tmp_path, monkeypatch):
    resume_file = tmp_path / "resume.docx"
    resume_file.write_bytes(b"PK\x03\x04mock-docx")
    target = {
        "id": "application-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/application-resume.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/application-page",
    }
    calls = []

    monkeypatch.setattr(application_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(application_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        application_browser,
        "capture_application_target_snapshot",
        lambda *_args: application_snapshot(target["url"]),
    )

    def fake_send(_target, method, params=None, **_kwargs):
        calls.append((method, params))
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelectorAll":
            return {"nodeIds": [42]}
        if method == "DOM.setFileInputFiles":
            return {}
        raise AssertionError(f"unexpected CDP method: {method}")

    monkeypatch.setattr(application_browser, "send_cdp_command", fake_send)
    monkeypatch.setattr(
        application_browser,
        "evaluate_cdp_expression",
        lambda *_args, **_kwargs: {"input_count": 1, "selected_count": 1},
    )

    result = application_browser.upload_application_resume_in_controlled_edge(application_plan(), str(resume_file))

    assert result["status"] == "已选择简历"
    assert result["file_selection_verified"] is True
    assert result["resume_suffix"] == "docx"
    assert result["resume_size_bytes"] == resume_file.stat().st_size
    assert result["browser_clicked"] is False
    assert result["resume_uploaded"] is True
    assert result["resume_path_saved"] is False
    assert [method for method, _params in calls] == ["DOM.getDocument", "DOM.querySelectorAll", "DOM.setFileInputFiles"]
    upload_params = calls[-1][1]
    assert upload_params["nodeId"] == 42
    assert upload_params["files"] == [str(resume_file)]


def test_application_resume_selection_stops_on_sensitive_or_ambiguous_page(tmp_path, monkeypatch):
    resume_file = tmp_path / "resume.pdf"
    resume_file.write_bytes(b"%PDF-mock")
    target = {"id": "application-page", "type": "page", "url": "https://www.zhipin.com/job_detail/application-resume.html"}
    actions = []

    monkeypatch.setattr(application_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(application_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        application_browser,
        "capture_application_target_snapshot",
        lambda *_args: application_snapshot(target["url"], "请完成验证码验证"),
    )
    monkeypatch.setattr(application_browser, "send_cdp_command", lambda *_args, **_kwargs: actions.append(True))

    try:
        application_browser.upload_application_resume_in_controlled_edge(application_plan(), str(resume_file))
    except ValueError as exc:
        assert "未找到身份匹配" in str(exc)
    else:
        raise AssertionError("验证码页面不能选择简历")
    assert not actions

    monkeypatch.setattr(
        application_browser,
        "capture_application_target_snapshot",
        lambda *_args: application_snapshot(target["url"]),
    )
    monkeypatch.setattr(application_browser, "evaluate_cdp_expression", lambda *_args, **_kwargs: {"input_count": 2, "selected_count": 0})
    try:
        application_browser.upload_application_resume_in_controlled_edge(application_plan(), str(resume_file))
    except ValueError as exc:
        assert "多个简历文件输入框" in str(exc)
    else:
        raise AssertionError("多个文件输入框时不能选择简历")


def test_application_resume_upload_route_requires_confirmation_and_redacts_path(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "application-resume-upload.sqlite3"))
    from app import main
    from app.db import connect, init_db, loads, utc_now

    resume_file = tmp_path / "secret-resume.docx"
    resume_file.write_bytes(b"PK\x03\x04mock-docx")
    init_db()
    now = utc_now()
    with connect() as conn:
        profile_id = conn.execute("SELECT id FROM candidate_profile ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute("UPDATE candidate_profile SET name = ? WHERE id = ?", ("测试候选人", profile_id))
        resume_id = conn.execute("SELECT id FROM resume_versions ORDER BY id LIMIT 1").fetchone()["id"]
        conn.execute(
            "UPDATE resume_versions SET file_path = ?, file_type = ?, parsed_text = ? WHERE id = ?",
            (str(resume_file), "docx", "Python FastAPI RAG 项目实践。" * 30, resume_id),
        )
        job_id = int(
            conn.execute(
                """
                INSERT INTO job_postings (
                    platform, source_url, title, company, jd_text, risk_level, recommendation,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Boss 直聘", "https://www.zhipin.com/job_detail/application-resume.html", "AI Agent 开发实习生",
                    "测试智能科技", "Python、FastAPI、RAG。", "低", "必投", "待投递", now, now,
                ),
            ).lastrowid
        )
        preparation_id = int(
            conn.execute(
                "INSERT INTO application_preparations (job_id, resume_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, resume_id, "已确认", now, now),
            ).lastrowid
        )

    called = []
    monkeypatch.setattr(
        main,
        "upload_application_resume_in_controlled_edge",
        lambda _plan, _path: called.append(_path) or {
            "status": "已选择简历",
            "note": "已在当前 Edge 选择本地简历文件；未点击投递或提交。",
            "file_selection_verified": True,
            "resume_suffix": "docx",
            "resume_size_bytes": resume_file.stat().st_size,
            "file_input_count": 1,
            "resume_uploaded": True,
            "resume_path_saved": False,
            "browser_clicked": False,
        },
    )
    client = TestClient(main.app)

    denied = client.post(
        f"/applications/{preparation_id}/browser-upload-resume",
        data={"confirmation": "错误确认", "return_to": "/applications"},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert called == []

    confirmed = client.post(
        f"/applications/{preparation_id}/browser-upload-resume",
        data={"confirmation": "选择并上传简历", "return_to": "/applications"},
        follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert called == [str(resume_file)]
    with connect() as conn:
        log = conn.execute("SELECT action_type, status, decision_json, summary FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    decision = loads(log["decision_json"], {})
    assert log["action_type"] == "application_browser_resume_upload"
    assert log["status"] == "已选择简历"
    assert decision["resume_id"] == resume_id
    assert decision["resume_file_type"] == "docx"
    assert decision["resume_path_saved"] is False
    assert decision["browser_clicked"] is False
    assert decision["application_submitted"] is False
    assert str(resume_file) not in log["decision_json"]
    assert resume_file.name not in log["summary"]


def test_application_resume_upload_blocks_missing_candidate_name(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "application-resume-name.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now

    init_db()
    now = utc_now()
    with connect() as conn:
        resume_id = conn.execute("SELECT id FROM resume_versions ORDER BY id LIMIT 1").fetchone()["id"]
        job_id = int(
            conn.execute(
                "INSERT INTO job_postings (platform, source_url, title, company, jd_text, risk_level, recommendation, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("Boss 直聘", "https://www.zhipin.com/job_detail/name.html", "AI 实习", "测试科技", "Python", "低", "必投", "待投递", now, now),
            ).lastrowid
        )
        preparation_id = int(
            conn.execute(
                "INSERT INTO application_preparations (job_id, resume_id, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (job_id, resume_id, "已确认", now, now),
            ).lastrowid
        )
    monkeypatch.setattr(main, "upload_application_resume_in_controlled_edge", lambda *_args: (_ for _ in ()).throw(AssertionError("不应调用浏览器上传")))

    result = main.run_application_browser_resume_upload(preparation_id)

    assert result["status"] == "未选择"
    assert "候选人名称尚未填写" in result["note"]
    assert result["resume_uploaded"] is False
