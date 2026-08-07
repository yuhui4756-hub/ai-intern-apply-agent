import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.services.job_fetcher import FetchResult
from app.services.job_searcher import SearchCandidate, SearchResult


def assert_no_running_asyncio_loop(message: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError(message)


def test_job_form_and_reanalysis_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.sqlite3"))

    from app import main
    from app.db import init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    form = client.get("/jobs/new")
    assert form.status_code == 200
    assert "本地规则分析" in form.text

    jd = "公司名称：杭州测试智能科技有限公司\nAI Agent 开发实习生\n要求 Python、RAG、FastAPI。"
    response = client.post(
        "/jobs/analyze",
        data={
            "platform": "smoke",
            "source_url": "https://example.invalid/job",
            "selected_resume_id": "1",
            "search_depth": "quick",
            "jd_text": jd,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert detail.status_code == 200
    assert "杭州测试智能科技有限公司" in detail.text
    assert "分析来源" in detail.text

    reanalyze = client.post(response.headers["location"] + "/reanalyze", data={"search_depth": "quick"}, follow_redirects=False)
    assert reanalyze.status_code == 303
    refreshed = client.get(reanalyze.headers["location"])
    assert "已重新分析岗位" in refreshed.text


def test_bulk_import_and_status_update_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "bulk.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    batch_text = """
    公司名称：杭州批量智能科技有限公司
    AI 应用开发实习生
    要求 Python、RAG、FastAPI，每周 5 天。

    ---

    公司名称：风险培训科技有限公司
    AI 实习生
    入职前需要缴纳培训费，可贷款。
    """
    response = client.post(
        "/jobs/bulk-analyze",
        data={
            "platform": "batch-smoke",
            "selected_resume_id": "1",
            "search_depth": "quick",
            "batch_jd_text": batch_text,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    listing = client.get(response.headers["location"])
    assert listing.status_code == 200
    assert "已导入 2 条岗位" in listing.text
    assert "杭州批量智能科技有限公司" in listing.text
    assert "风险培训科技有限公司" in listing.text
    assert "复制" in listing.text

    with connect() as conn:
        rows = conn.execute("SELECT id, recommendation, status FROM job_postings ORDER BY id").fetchall()
    assert len(rows) == 2
    assert {row["recommendation"] for row in rows} >= {"跳过"}

    actionable_id = next(row["id"] for row in rows if row["recommendation"] != "跳过")
    update = client.post(
        "/jobs/bulk-status",
        data={"job_ids": [str(actionable_id)], "status": "待投递"},
        follow_redirects=False,
    )
    assert update.status_code == 303
    with connect() as conn:
        status = conn.execute("SELECT status FROM job_postings WHERE id = ?", (actionable_id,)).fetchone()["status"]
    assert status == "待投递"


def test_import_job_from_url_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "url.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        main,
        "fetch_job_from_url",
        lambda _url, fetch_mode="auto", browser_channel="msedge": FetchResult(
            url="https://jobs.example.com/ai-intern",
            final_url="https://jobs.example.com/ai-intern",
            title="AI 应用开发实习生 - 链接测试",
            text="公司名称：杭州链接智能科技有限公司\nAI 应用开发实习生\n要求 Python、RAG、FastAPI，每周 5 天。",
            fetch_mode=fetch_mode,
        ),
    )
    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/jobs/import-url",
        data={
            "source_url": "https://jobs.example.com/ai-intern",
            "selected_resume_id": "1",
            "search_depth": "quick",
            "fetch_mode": "browser",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    detail = client.get(response.headers["location"])
    assert "已通过浏览器渲染导入并完成分析" in detail.text
    assert "杭州链接智能科技有限公司" in detail.text
    with connect() as conn:
        row = conn.execute("SELECT source_url, platform FROM job_postings ORDER BY id DESC LIMIT 1").fetchone()
    assert row["source_url"] == "https://jobs.example.com/ai-intern"
    assert row["platform"] == "岗位链接"


def test_extension_capture_job_creates_analyzed_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "extension-job.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/example.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳扩展智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
            "links": [],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["redirect_url"].startswith("/jobs/")
    with connect() as conn:
        row = conn.execute("SELECT platform, source_url, company, match_level, generated_message FROM job_postings ORDER BY id DESC LIMIT 1").fetchone()
        event = conn.execute("SELECT event_type FROM application_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["platform"] == "Boss 直聘"
    assert row["source_url"] == "https://www.zhipin.com/job_detail/example.html"
    assert row["company"] == "深圳扩展智能科技有限公司"
    assert row["match_level"]
    assert row["generated_message"] == ""
    assert event["event_type"] == "浏览器扩展采集"


def test_extension_capture_search_creates_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "extension-search.sqlite3"))

    from app import main  # noqa: F401
    from app.db import connect, init_db

    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "search",
            "url": "https://www.zhipin.com/web/geek/job?query=AI%E5%BA%94%E7%94%A8%E5%BC%80%E5%8F%91&city=101280600",
            "title": "AI 应用开发招聘",
            "text": "AI 应用开发实习生 深圳扩展智能科技有限公司",
            "links": [
                {
                    "href": "https://www.zhipin.com/job_detail/extension-1.html",
                    "text": "AI 应用开发实习生",
                    "context": "AI 应用开发实习生\n深圳扩展智能科技有限公司\nPython FastAPI RAG",
                },
                {
                    "href": "https://www.zhipin.com/web/geek/job?query=AI",
                    "text": "搜索页",
                    "context": "搜索页",
                },
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["candidate_count"] == 1
    assert payload["redirect_url"].startswith("/searches/")
    with connect() as conn:
        run = conn.execute("SELECT platform, keyword, browser_channel, status FROM job_search_runs ORDER BY id DESC LIMIT 1").fetchone()
        candidate = conn.execute("SELECT title, company, source_url FROM job_candidates ORDER BY id DESC LIMIT 1").fetchone()
    assert run["platform"] == "Boss 直聘"
    assert run["keyword"] == "AI应用开发"
    assert run["browser_channel"] == "extension"
    assert run["status"] == "完成"
    assert candidate["title"] == "AI 应用开发实习生"
    assert candidate["company"] == "深圳扩展智能科技有限公司"


def test_extension_capture_accepts_chrome_injection_result_array(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "extension-array.sqlite3"))

    from app import main  # noqa: F401
    from app.db import connect, init_db

    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json=[
            {
                "frameId": 0,
                "result": {
                    "capture_type": "search",
                    "url": "https://www.zhipin.com/web/geek/job?query=AI",
                    "title": "AI 招聘",
                    "text": "AI 应用开发实习生",
                    "cards": [
                        {
                            "href": "https://www.zhipin.com/job_detail/array-1.html",
                            "title": "AI 应用开发实习生",
                            "text": "AI 应用开发实习生\n深圳数组智能科技有限公司\n200-300元/天\nPython FastAPI RAG",
                        }
                    ],
                },
            }
        ],
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["candidate_count"] == 1
    with connect() as conn:
        candidate = conn.execute("SELECT company FROM job_candidates ORDER BY id DESC LIMIT 1").fetchone()
    assert candidate["company"] == "深圳数组智能科技有限公司"


def test_extension_capture_search_uses_current_page_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "extension-search-cards.sqlite3"))

    from app import main  # noqa: F401
    from app.db import connect, init_db

    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "search",
            "url": "https://www.shixiseng.com/interns?keyword=Agent",
            "title": "Agent 实习",
            "text": "页面上的可见搜索结果",
            "links": [],
            "cards": [
                {
                    "href": "https://www.shixiseng.com/intern/inn_agent_1",
                    "title": "大模型 Agent 开发实习生",
                    "text": "大模型 Agent 开发实习生\n广州卡片智能科技有限公司\n150～250元/天\nPython FastAPI RAG",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["candidate_count"] == 1
    assert payload["source_count"] == 1
    with connect() as conn:
        candidate = conn.execute("SELECT title, company, summary FROM job_candidates ORDER BY id DESC LIMIT 1").fetchone()
    assert candidate["title"] == "大模型 Agent 开发实习生"
    assert candidate["company"] == "广州卡片智能科技有限公司"
    assert "150～250元/天" in candidate["summary"]


def test_extension_candidate_import_falls_back_to_search_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "extension-candidate-fallback.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        main,
        "fetch_job_from_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("浏览器类型无效")),
    )
    init_db()
    client = TestClient(main.app)

    capture = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "search",
            "url": "https://www.zhipin.com/web/geek/job?query=AI",
            "title": "AI 招聘",
            "text": "AI 应用开发实习生",
            "cards": [
                {
                    "href": "https://www.zhipin.com/job_detail/fallback-1.html",
                    "title": "AI 应用开发实习生",
                    "text": "AI 应用开发实习生\n深圳摘要智能科技有限公司\n200-300元/天\nPython FastAPI RAG",
                }
            ],
        },
    )
    assert capture.status_code == 200
    with connect() as conn:
        candidate_id = conn.execute("SELECT id FROM job_candidates ORDER BY id DESC LIMIT 1").fetchone()["id"]

    imported = client.post(
        f"/candidates/{candidate_id}/import",
        data={"selected_resume_id": "1", "fetch_mode": "auto", "browser_channel": "extension"},
        follow_redirects=False,
    )

    assert imported.status_code == 303
    detail = client.get(imported.headers["location"])
    assert "深圳摘要智能科技有限公司" in detail.text
    assert "200-300元/天" in detail.text
    assert "详情页抓取失败" in detail.text
    with connect() as conn:
        candidate = conn.execute("SELECT status, error_message FROM job_candidates WHERE id = ?", (candidate_id,)).fetchone()
        job = conn.execute("SELECT salary_text, generated_message FROM job_postings ORDER BY id DESC LIMIT 1").fetchone()
    assert candidate["status"] == "已导入"
    assert "搜索结果摘要" in candidate["error_message"]
    assert job["salary_text"] == "200-300元/天"
    assert job["generated_message"] == ""


def test_extension_job_capture_refreshes_existing_candidate_job(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "extension-refresh-existing.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        main,
        "fetch_job_from_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("详情页需要登录")),
    )
    init_db()
    client = TestClient(main.app)
    source_url = "https://www.zhipin.com/job_detail/refresh-1.html"

    capture = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "search",
            "url": "https://www.zhipin.com/web/geek/job?query=AI",
            "title": "AI 招聘",
            "text": "AI 应用开发实习生",
            "cards": [
                {
                    "href": source_url,
                    "title": "AI 应用开发实习生",
                    "text": "AI 应用开发实习生\n深圳回填智能科技有限公司\n200-300元/天\nPython FastAPI RAG",
                }
            ],
        },
    )
    assert capture.status_code == 200
    with connect() as conn:
        candidate_id = conn.execute("SELECT id FROM job_candidates ORDER BY id DESC LIMIT 1").fetchone()["id"]

    fallback_import = client.post(
        f"/candidates/{candidate_id}/import",
        data={"selected_resume_id": "1", "fetch_mode": "auto", "browser_channel": "extension"},
        follow_redirects=False,
    )
    assert fallback_import.status_code == 303
    with connect() as conn:
        first_job = conn.execute("SELECT id, jd_text FROM job_postings WHERE source_url = ?", (source_url,)).fetchone()
    assert "搜索结果摘要" in first_job["jd_text"]

    refresh = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": source_url + "?securityId=abc",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳回填智能科技有限公司\n岗位职责：负责 RAG 知识库和 Agent 工具调用。\n任职要求：Python、FastAPI、SQLite，每周 5 天。",
            "links": [],
        },
    )

    assert refresh.status_code == 200
    payload = refresh.json()
    assert payload["ok"] is True
    assert payload["job_id"] == first_job["id"]
    assert payload["updated"] is True
    assert payload["linked_candidate_count"] == 1
    with connect() as conn:
        job_count = conn.execute("SELECT COUNT(*) AS count FROM job_postings WHERE source_url LIKE ?", ("https://www.zhipin.com/job_detail/refresh-1.html%",)).fetchone()["count"]
        job = conn.execute("SELECT jd_text, generated_message FROM job_postings WHERE id = ?", (first_job["id"],)).fetchone()
        candidate = conn.execute("SELECT job_id, status, error_message FROM job_candidates WHERE id = ?", (candidate_id,)).fetchone()
        event = conn.execute("SELECT event_type FROM application_events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (first_job["id"],)).fetchone()
    assert job_count == 1
    assert "岗位职责：负责 RAG 知识库和 Agent 工具调用" in job["jd_text"]
    assert job["generated_message"] == ""
    assert candidate["job_id"] == first_job["id"]
    assert candidate["status"] == "已导入"
    assert candidate["error_message"] == ""
    assert event["event_type"] == "浏览器扩展刷新"


def test_extension_conversation_capture_creates_safe_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "conversation-draft.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/chat-safe.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳对话智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert job_response.status_code == 200

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/web/geek/chat",
            "title": "深圳对话智能科技有限公司 HR 对话",
            "text": "HR：您好，请问想了解什么？\n我：您好，我想了解 AI 应用开发实习生。\nHR：可以的，你想了解工作内容还是实习周期？",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["capture_type"] == "conversation"
    assert payload["message_type"] == "岗位沟通"
    with connect() as conn:
        capture = conn.execute("SELECT message_type, action_required FROM conversation_captures ORDER BY id DESC LIMIT 1").fetchone()
        draft = conn.execute("SELECT status, message, draft_type FROM message_drafts ORDER BY id DESC LIMIT 1").fetchone()
        action_log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
        patrol = conn.execute("SELECT status, checked_count, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture["message_type"] == "岗位沟通"
    assert capture["action_required"] == 0
    assert draft["status"] == "待确认"
    assert draft["draft_type"] == "岗位沟通"
    assert "主要工作内容" in draft["message"]
    assert action_log["action_type"] == "conversation_capture"
    assert action_log["status"] == "岗位沟通"
    assert "draft_status" in action_log["decision_json"]
    assert patrol["status"] == "已处理"
    assert patrol["checked_count"] == 1
    assert patrol["new_count"] == 1
    assert patrol["skipped_count"] == 0


def test_liepin_resume_button_does_not_trigger_manual_review():
    from app.services.conversation import classify_conversation, prepare_conversation_text

    text = """
    张女士 杭州聚泽工程项目管理有限...
    成本助理实习生 统招本... 经验不限 2-3k
    杭州聚泽工程项目管理有限...
    不需要融资 杭州-余杭区
    我们正在招成本助理实习生，您可以看下职位信息，有兴趣可以聊一聊
    再考虑一下
    发简历
    18:32
    我们为您生成了合适的打招呼语，去使用>
    不支持此消息查看，请登录“猎聘APP”查看消息内容！
    发简历
    交换手机号
    交换微信号
    请输入文字，按Enter键发送
    发送
    """

    prepared = prepare_conversation_text(text)
    clean = prepared["clean_text"]
    result = classify_conversation(text, {"title": "成本助理实习生"})

    assert "发简历" not in clean
    assert "交换手机号" not in clean
    assert "发简历" in prepared["ignored_lines"]
    assert result["message_type"] == "无需回复"
    assert result["action_required"] is False
    assert result["draft_message"] == ""


def test_hr_resume_request_still_triggers_manual_review():
    from app.services.conversation import classify_conversation

    result = classify_conversation("HR：麻烦你发一下简历附件，我这边先看看。")

    assert result["message_type"] == "需要我处理"
    assert result["action_required"] is True
    assert result["draft_message"] == ""


def test_broadcast_recommendation_is_reply_gate_skip():
    from app.services.conversation import classify_conversation

    result = classify_conversation(
        "卢女士 晶科能源有限公司\n储能销售经理\n赵先生您好！我们正在招聘储能销售经理，期待您的投递。\n发简历"
    )

    assert result["message_type"] == "无需回复"
    assert result["action_required"] is False
    assert result["draft_message"] == ""
    assert result["reply_gate"] == "skip"


def test_extension_conversation_capture_broadcast_saves_no_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "conversation-broadcast-skip.sqlite3"))

    from app import main
    from app.db import connect, init_db

    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://c.liepin.com/?time=1786081283285",
            "title": "我的首页_猎聘",
            "platform": "猎聘",
            "text": "卢女士 晶科能源有限公司\n储能销售经理\n赵先生您好！我们正在招聘储能销售经理，期待您的投递。\n发简历",
            "text_scope": "conversation_panel",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message_type"] == "无需回复"
    assert payload["skipped"] is True
    with connect() as conn:
        capture = conn.execute("SELECT message_type, action_required FROM conversation_captures ORDER BY id DESC LIMIT 1").fetchone()
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        patrol = conn.execute(
            "SELECT status, checked_count, new_count, skipped_count, fingerprint_key, fingerprint FROM message_patrol_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert capture["message_type"] == "无需回复"
    assert capture["action_required"] == 0
    assert draft_count == 0
    assert patrol["status"] == "无需回复"
    assert patrol["checked_count"] == 1
    assert patrol["new_count"] == 0
    assert patrol["skipped_count"] == 1
    assert patrol["fingerprint_key"]
    assert patrol["fingerprint"]


def test_message_draft_status_update_writes_action_log(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "draft-action-log.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/draft-log.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳日志智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    captured = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/draft-log.html",
            "title": "深圳日志智能科技有限公司 HR 对话",
            "text": "HR：可以的，你想了解工作内容还是实习周期？",
        },
    )
    draft_id = captured.json()["draft_id"]

    response = client.post(
        f"/message-drafts/{draft_id}",
        data={"status": "已发送", "message": "您好，想了解工作内容和实习周期。"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        action_log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert action_log["action_type"] == "draft_status_update"
    assert action_log["status"] == "已发送"
    assert "待确认 -> 已发送" in action_log["summary"]
    assert "message_length" in action_log["decision_json"]
    assert '"allowed": true' in action_log["decision_json"]


def test_send_gate_blocks_interview_invite_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "draft-send-gate-interview.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/send-gate-interview.html",
            "title": "AI Agent 开发实习生",
            "text": "公司名称：杭州发送闸门智能科技有限公司\nAI Agent 开发实习生\n要求 Python、RAG、FastAPI。",
        },
    )
    job_id = job_response.json()["job_id"]
    captured = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/send-gate-interview.html",
            "title": "杭州发送闸门智能科技有限公司 HR 对话",
            "text": "HR：我们可以约一个线上面试时间，你什么时候方便？",
        },
    )
    draft_id = captured.json()["draft_id"]

    response = client.post(
        f"/message-drafts/{draft_id}",
        data={"status": "已发送", "message": "您好，我这周三下午方便。"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        draft = conn.execute("SELECT status, message FROM message_drafts WHERE id = ?", (draft_id,)).fetchone()
        sent_events = conn.execute(
            "SELECT COUNT(*) AS count FROM application_events WHERE job_id = ? AND event_type = '沟通草稿已发送'",
            (job_id,),
        ).fetchone()["count"]
        action_log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert draft["status"] == "需要我处理"
    assert draft["message"] == "您好，我这周三下午方便。"
    assert sent_events == 0
    assert action_log["action_type"] == "draft_send_gate"
    assert action_log["status"] == "已拦截"
    assert "面试邀请" in action_log["summary"]
    assert '"allowed": false' in action_log["decision_json"]


def test_send_gate_blocks_low_match_job_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "draft-send-gate-low-match.sqlite3"))

    from app import main
    from app.db import connect, init_db, utc_now

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/send-gate-low.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳低匹配智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG。",
        },
    )
    job_id = job_response.json()["job_id"]
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET recommendation = ?, match_level = ?, updated_at = ? WHERE id = ?",
            ("跳过", "低匹配", now, job_id),
        )
        cursor = conn.execute(
            """
            INSERT INTO message_drafts (
                job_id, platform, draft_type, status, reason, message,
                risk_flags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, "Boss 直聘", "岗位沟通", "待确认", "测试未来自动发送闸门", "您好，想了解岗位内容。", "[]", now, now),
        )
        draft_id = int(cursor.lastrowid)

    response = client.post(
        f"/message-drafts/{draft_id}",
        data={"status": "已发送", "message": "您好，想了解岗位内容。"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        draft = conn.execute("SELECT status FROM message_drafts WHERE id = ?", (draft_id,)).fetchone()
        action_log = conn.execute("SELECT action_type, status, summary FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert draft["status"] == "待确认"
    assert action_log["action_type"] == "draft_send_gate"
    assert action_log["status"] == "已拦截"
    assert "低匹配" in action_log["summary"]


def test_communications_page_shows_executor_status_and_demo_button(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communications-executor-status.sqlite3"))

    from app import main
    from app.db import init_db

    init_db()
    client = TestClient(main.app)

    response = client.get("/communications")

    assert response.status_code == 200
    assert "自动回复执行器" in response.text
    assert "待确认候选 0" in response.text
    assert "创建本地演练草稿" in response.text
    assert "暂无 dry-run 记录" in response.text


def test_demo_draft_creates_pending_candidate_for_executor(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-demo-draft.sqlite3"))

    from app import main
    from app.db import connect, init_db

    init_db()
    client = TestClient(main.app)

    created = client.post("/communication-executor/demo-draft", data={"return_to": "/communications"}, follow_redirects=False)

    assert created.status_code == 303
    assert created.headers["location"].startswith("/communications")
    with connect() as conn:
        draft = conn.execute(
            """
            SELECT d.status, d.draft_type, d.message, d.reason, j.analysis_source, j.status AS job_status
            FROM message_drafts d
            LEFT JOIN job_postings j ON j.id = d.job_id
            WHERE d.reason LIKE '%本地演练%'
            ORDER BY d.id DESC
            LIMIT 1
            """
        ).fetchone()
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts WHERE reason LIKE '%本地演练%'").fetchone()["count"]
        action_log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert draft is not None
    assert draft_count == 1
    assert draft["status"] == "待确认"
    assert draft["draft_type"] == "岗位沟通"
    assert "主要工作内容" in draft["message"]
    assert "本地演练" in draft["reason"]
    assert draft["analysis_source"] == "local_demo"
    assert draft["job_status"] == "演练"
    assert action_log["action_type"] == "demo_draft_created"
    assert action_log["status"] == "已创建"
    assert '"real_platform_data": false' in action_log["decision_json"]

    existing = client.post("/communication-executor/demo-draft", data={"return_to": "/communications"}, follow_redirects=False)
    assert existing.status_code == 303
    with connect() as conn:
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts WHERE reason LIKE '%本地演练%'").fetchone()["count"]
        action_log = conn.execute("SELECT action_type, status FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert draft_count == 1
    assert action_log["action_type"] == "demo_draft_created"
    assert action_log["status"] == "已存在"

    dry_run = client.post("/communication-executor/dry-run", data={"return_to": "/communications"}, follow_redirects=False)

    assert dry_run.status_code == 303
    with connect() as conn:
        action_log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert action_log["action_type"] == "communication_executor_dry_run"
    assert action_log["status"] == "演练完成"
    assert "计划发送 1 条" in action_log["summary"]
    assert '"candidate_count": 1' in action_log["decision_json"]
    assert '"allowed_count": 1' in action_log["decision_json"]
    assert '"blocked_count": 0' in action_log["decision_json"]


def test_communication_executor_dry_run_plans_without_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-executor-dry-run.sqlite3"))

    from app import main
    from app.db import connect, init_db, utc_now

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    allowed_job = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/executor-allowed.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳演练智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert allowed_job.status_code == 200
    captured = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/executor-allowed.html",
            "title": "深圳演练智能科技有限公司 HR 对话",
            "text": "HR：可以的，你想了解工作内容还是实习周期？",
        },
    )
    allowed_draft_id = captured.json()["draft_id"]

    blocked_job = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/executor-blocked.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳拦截智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG。",
        },
    )
    blocked_job_id = blocked_job.json()["job_id"]
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET recommendation = ?, match_level = ?, updated_at = ? WHERE id = ?",
            ("跳过", "低匹配", now, blocked_job_id),
        )
        cursor = conn.execute(
            """
            INSERT INTO message_drafts (
                job_id, platform, draft_type, status, reason, message,
                risk_flags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (blocked_job_id, "Boss 直聘", "岗位沟通", "待确认", "低匹配测试", "您好，想了解岗位内容。", "[]", now, now),
        )
        blocked_draft_id = int(cursor.lastrowid)

    response = client.post("/communication-executor/dry-run", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/communications")
    with connect() as conn:
        drafts = conn.execute(
            "SELECT id, status FROM message_drafts WHERE id IN (?, ?) ORDER BY id",
            (allowed_draft_id, blocked_draft_id),
        ).fetchall()
        action_log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert [row["status"] for row in drafts] == ["待确认", "待确认"]
    assert action_log["action_type"] == "communication_executor_dry_run"
    assert action_log["status"] == "演练完成"
    assert "计划发送 1 条，拦截 1 条" in action_log["summary"]
    assert '"allowed_count": 1' in action_log["decision_json"]
    assert '"blocked_count": 1' in action_log["decision_json"]
    assert "计划发送" in action_log["decision_json"]
    assert "拦截" in action_log["decision_json"]
    assert "主要工作内容" not in action_log["decision_json"]


def test_communication_executor_dry_run_respects_mode_off(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-executor-off.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting, utc_now

    init_db()
    set_setting("communication_policy", {"mode": "off", "max_auto_followups": 2})
    client = TestClient(main.app)
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO message_drafts (
                platform, draft_type, status, reason, message,
                risk_flags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("Boss 直聘", "岗位沟通", "待确认", "关闭模式测试", "您好，想了解岗位内容。", "[]", now, now),
        )
        draft_id = int(cursor.lastrowid)

    response = client.post("/communication-executor/dry-run", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        draft = conn.execute("SELECT status FROM message_drafts WHERE id = ?", (draft_id,)).fetchone()
        action_log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert draft["status"] == "待确认"
    assert action_log["action_type"] == "communication_executor_dry_run"
    assert action_log["status"] == "已关闭"
    assert '"candidate_count": 0' in action_log["decision_json"]


def test_communication_browser_dry_run_maps_platform_strategy_without_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-browser-dry-run.sqlite3"))

    from app import main
    from app.db import connect, init_db, utc_now

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    allowed_job = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/browser-allowed.html",
            "platform": "Boss 直聘",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳浏览器映射智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert allowed_job.status_code == 200
    captured = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/browser-allowed.html",
            "platform": "Boss 直聘",
            "title": "深圳浏览器映射智能科技有限公司 HR 对话",
            "text": "HR：可以的，你想了解工作内容还是实习周期？",
        },
    )
    allowed_draft_id = captured.json()["draft_id"]

    blocked_job = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/browser-blocked.html",
            "platform": "Boss 直聘",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳浏览器拦截智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG。",
        },
    )
    blocked_job_id = blocked_job.json()["job_id"]
    now = utc_now()
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET recommendation = ?, match_level = ?, updated_at = ? WHERE id = ?",
            ("跳过", "低匹配", now, blocked_job_id),
        )
        cursor = conn.execute(
            """
            INSERT INTO message_drafts (
                job_id, platform, draft_type, status, reason, message,
                risk_flags_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (blocked_job_id, "Boss 直聘", "岗位沟通", "待确认", "低匹配测试", "您好，想了解岗位内容。", "[]", now, now),
        )
        blocked_draft_id = int(cursor.lastrowid)

    response = client.post("/communication-executor/browser-dry-run", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/communications")
    with connect() as conn:
        drafts = conn.execute(
            "SELECT id, status FROM message_drafts WHERE id IN (?, ?) ORDER BY id",
            (allowed_draft_id, blocked_draft_id),
        ).fetchall()
        action_log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert [row["status"] for row in drafts] == ["待确认", "待确认"]
    assert action_log["action_type"] == "communication_browser_dry_run"
    assert action_log["status"] == "映射完成"
    assert "可浏览器定位 1 条" in action_log["summary"]
    assert '"browser_ready_count": 1' in action_log["decision_json"]
    assert '"browser_skipped_count": 1' in action_log["decision_json"]
    assert "dry_run_ready" in action_log["decision_json"]
    assert "selector_candidates" in action_log["decision_json"]
    assert "zhipin.com" in action_log["decision_json"]
    assert '"browser_clicked": false' in action_log["decision_json"]
    assert '"message_filled": false' in action_log["decision_json"]
    assert "主要工作内容" not in action_log["decision_json"]


def test_communication_browser_strategy_service_handles_unsupported_platform():
    from app.services.communication_browser import build_browser_send_adapter_plan

    plan = build_browser_send_adapter_plan(
        {
            "ok": True,
            "dry_run": True,
            "trigger_type": "test",
            "status": "演练完成",
            "note": "",
            "policy_mode": "draft",
            "candidate_count": 1,
            "allowed_count": 1,
            "blocked_count": 0,
            "plans": [
                {
                    "draft_id": 1,
                    "job_id": 2,
                    "platform": "未知平台",
                    "company": "测试公司",
                    "job_title": "AI 应用开发实习生",
                    "source_url": "https://jobs.example.com/chat",
                    "message_length": 20,
                    "gate_allowed": True,
                    "gate_reasons": [],
                }
            ],
        }
    )

    assert plan["status"] == "映射完成"
    assert plan["browser_manual_count"] == 1
    assert plan["browser_plans"][0]["browser_action"] == "manual_locate"
    assert plan["browser_plans"][0]["message_text_included"] is False


def test_communication_browser_probe_matches_page_snapshot():
    from app.services.communication_browser import (
        build_browser_send_adapter_plan,
        normalize_probe_text,
        probe_browser_send_adapter_plan,
        text_digest,
    )

    browser_plan = build_browser_send_adapter_plan(
        {
            "ok": True,
            "dry_run": True,
            "trigger_type": "test",
            "status": "演练完成",
            "note": "",
            "policy_mode": "draft",
            "candidate_count": 1,
            "allowed_count": 1,
            "blocked_count": 0,
            "plans": [
                {
                    "draft_id": 1,
                    "job_id": 2,
                    "platform": "Boss 直聘",
                    "company": "深圳探测智能科技有限公司",
                    "job_title": "AI 应用开发实习生",
                    "source_url": "https://www.zhipin.com/job_detail/probe-ready.html",
                    "message_length": 24,
                    "gate_allowed": True,
                    "gate_reasons": [],
                }
            ],
        }
    )
    page_text = "深圳探测智能科技有限公司 AI 应用开发实习生 HR：可以沟通，请输入内容后发送"
    result = probe_browser_send_adapter_plan(
        browser_plan,
        page_snapshots=[
            {
                "url": "https://www.zhipin.com/job_detail/probe-ready.html",
                "title": "深圳探测智能科技有限公司 HR 对话",
                "host": "www.zhipin.com",
                "text_length": len(page_text),
                "text_digest": text_digest(page_text),
                "normalized_text": normalize_probe_text(page_text),
                "selectors": {
                    "[class*='chat']": 1,
                    "textarea": 1,
                    "button:has-text('发送')": 1,
                },
            }
        ],
    )

    assert result["status"] == "探测完成"
    assert result["probe_ready_count"] == 1
    probe = result["probe_results"][0]
    assert probe["probe_status"] == "probe_ready"
    assert probe["message_text_included"] is False
    assert probe["matched_page"]["company_match"] is True
    assert probe["matched_page"]["message_input_count"] == 1
    assert probe["matched_page"]["send_button_count"] == 1
    assert "深圳探测智能科技有限公司" not in probe["matched_page"]["text_digest"]


def test_communication_browser_probe_route_logs_dry_run_without_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-browser-probe-route.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/probe-route.html",
            "platform": "Boss 直聘",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳路由探测智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG。",
        },
    )
    assert job_response.status_code == 200
    captured = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/probe-route.html",
            "platform": "Boss 直聘",
            "title": "深圳路由探测智能科技有限公司 HR 对话",
            "text": "HR：可以的，你想了解工作内容还是实习周期？",
        },
    )
    draft_id = captured.json()["draft_id"]

    def fake_probe(browser_plan):
        return {
            **browser_plan,
            "status": "探测完成",
            "note": "探测 1 条浏览器定位计划：可定位 1 条，部分匹配 0 条，未找到 0 条，跳过 0 条。",
            "browser_probe_dry_run": True,
            "browser_connected": True,
            "page_count": 1,
            "probe_ready_count": 1,
            "probe_partial_count": 0,
            "probe_not_found_count": 0,
            "probe_skipped_count": 0,
            "probe_results": [
                {
                    "draft_id": draft_id,
                    "probe_status": "probe_ready",
                    "message_text_included": False,
                    "matched_page": {
                        "host": "www.zhipin.com",
                        "text_length": 120,
                        "text_digest": "sha256:test|len:120",
                        "domain_match": True,
                        "company_match": True,
                        "job_title_match": True,
                        "message_input_count": 1,
                        "send_button_count": 1,
                    },
                }
            ],
            "message_text_saved": False,
        }

    monkeypatch.setattr(main, "probe_browser_send_adapter_plan", fake_probe)

    response = client.post("/communication-executor/browser-probe-dry-run", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/communications")
    with connect() as conn:
        draft = conn.execute("SELECT status FROM message_drafts WHERE id = ?", (draft_id,)).fetchone()
        action_log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert draft["status"] == "待确认"
    assert action_log["action_type"] == "communication_browser_probe"
    assert action_log["status"] == "探测完成"
    assert "可定位 1 条" in action_log["summary"]
    assert '"browser_connected": true' in action_log["decision_json"]
    assert '"probe_ready_count": 1' in action_log["decision_json"]
    assert '"browser_clicked": false' in action_log["decision_json"]
    assert '"message_filled": false' in action_log["decision_json"]
    assert "主要工作内容" not in action_log["decision_json"]


def test_conversation_capture_stores_cleaning_debug_and_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "conversation-feedback.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.liepin.com/job/debug-1.html",
            "title": "成本助理实习生",
            "text": "公司名称：杭州聚泽工程项目管理有限公司\n成本助理实习生\n支持 AI 工具整理数据。",
        },
    )
    job_id = job_response.json()["job_id"]
    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.liepin.com/im/chat/debug-1",
            "title": "猎聘 HR 对话",
            "text": "HR：我们正在招成本助理实习生，您可以看下职位信息，有兴趣可以聊一聊\n发简历\n交换手机号\n请输入文字，按Enter键发送",
        },
    )
    assert response.status_code == 200
    capture_id = response.json()["capture_id"]

    with connect() as conn:
        capture = conn.execute("SELECT * FROM conversation_captures WHERE id = ?", (capture_id,)).fetchone()
    assert capture["job_id"] == job_id
    assert "发简历" in capture["raw_visible_text"]
    assert "发简历" not in capture["conversation_text"]
    assert "发简历" in capture["ignored_lines_json"]

    feedback = client.post(
        f"/conversation-captures/{capture_id}/feedback",
        data={
            "feedback_status": "误判",
            "expected_message_type": "岗位沟通",
            "feedback_note": "猎聘底部发简历按钮不应触发人工处理。",
        },
        follow_redirects=False,
    )
    assert feedback.status_code == 303

    with connect() as conn:
        capture = conn.execute("SELECT feedback_status, expected_message_type, feedback_note FROM conversation_captures WHERE id = ?", (capture_id,)).fetchone()
        event = conn.execute("SELECT event_type, content FROM application_events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        action_log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture["feedback_status"] == "误判"
    assert capture["expected_message_type"] == "岗位沟通"
    assert "猎聘底部发简历按钮" in capture["feedback_note"]
    assert event["event_type"] == "对话分类反馈"
    assert action_log["action_type"] == "conversation_feedback"
    assert action_log["status"] == "误判"
    assert "expected_message_type" in action_log["decision_json"]


def test_settings_updates_communication_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-settings.sqlite3"))

    from app import main
    from app.db import connect, get_setting, init_db

    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/settings/communication",
        data={"mode": "autonomous", "max_auto_followups": "3"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    policy = get_setting("communication_policy", {})
    assert policy["mode"] == "autonomous"
    assert policy["max_auto_followups"] == 3
    with connect() as conn:
        action_log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert action_log["action_type"] == "communication_policy_update"
    assert action_log["status"] == "autonomous"
    assert "new_max_auto_followups" in action_log["decision_json"]


def test_conversation_capture_skips_when_communication_mode_off(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-off.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting

    init_db()
    set_setting("communication_policy", {"mode": "off", "max_auto_followups": 2})
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/web/geek/chat",
            "title": "HR 对话",
            "text": "HR：想了解工作内容吗？",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["skipped"] is True
    assert payload["message_type"] == "沟通模式已关闭"
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        log_count = conn.execute("SELECT COUNT(*) AS count FROM agent_action_logs").fetchone()["count"]
        patrol_count = conn.execute("SELECT COUNT(*) AS count FROM message_patrol_runs").fetchone()["count"]
    assert count == 0
    assert log_count == 0
    assert patrol_count == 0


def test_automation_control_pause_resume_updates_setting_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "automation-control.sqlite3"))

    from app import main
    from app.db import connect, get_setting, init_db

    init_db()
    client = TestClient(main.app)

    pause = client.post(
        "/settings/automation-control",
        data={"action": "pause", "pause_reason": "今天暂停自动处理", "return_to": "/communications"},
        follow_redirects=False,
    )

    assert pause.status_code == 303
    assert pause.headers["location"].startswith("/communications")
    control = get_setting("automation_control", {})
    assert control["paused"] is True
    assert control["pause_reason"] == "今天暂停自动处理"
    with connect() as conn:
        log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert log["action_type"] == "automation_control_update"
    assert log["status"] == "已暂停"
    assert "今天暂停自动处理" in log["decision_json"]

    resume = client.post(
        "/settings/automation-control",
        data={"action": "resume", "return_to": "/settings"},
        follow_redirects=False,
    )

    assert resume.status_code == 303
    assert resume.headers["location"].startswith("/settings")
    control = get_setting("automation_control", {})
    assert control["paused"] is False
    assert control["pause_reason"] == ""


def test_message_patrol_policy_update_and_manual_tick(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "message-patrol-policy.sqlite3"))

    from app import main
    from app.db import connect, get_setting, init_db

    init_db()
    client = TestClient(main.app)

    saved = client.post(
        "/settings/message-patrol",
        data={
            "enabled": "on",
            "interval_seconds": "60",
            "cooldown_seconds": "0",
            "return_to": "/settings",
        },
        follow_redirects=False,
    )

    assert saved.status_code == 303
    policy = get_setting("message_patrol_policy", {})
    assert policy["enabled"] is True
    assert policy["interval_seconds"] == 60
    assert policy["cooldown_seconds"] == 0
    assert policy["next_tick_at"]
    monkeypatch.setattr(
        main,
        "capture_browser_patrol_observations",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("没有检测到应用打开的 Edge 调试窗口")),
    )

    tick = client.post("/message-patrol/tick", data={"return_to": "/communications"}, follow_redirects=False)

    assert tick.status_code == 303
    with connect() as conn:
        patrol = conn.execute("SELECT trigger_type, scope, status, checked_count, new_count, skipped_count, note FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
        action_log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    policy = get_setting("message_patrol_policy", {})
    assert patrol["trigger_type"] == "manual_browser"
    assert patrol["scope"] == "scheduled_patrol"
    assert patrol["status"] == "浏览器未连接"
    assert patrol["checked_count"] == 0
    assert patrol["new_count"] == 0
    assert patrol["skipped_count"] == 1
    assert "没有检测到应用打开的 Edge 调试窗口" in patrol["note"]
    assert action_log["action_type"] == "message_patrol_run"
    assert action_log["status"] == "浏览器未连接"
    assert "edge_cdp" in action_log["decision_json"]
    assert policy["last_status"] == "浏览器未连接"
    assert policy["last_tick_at"]


def test_message_patrol_tick_respects_pause_without_browser_or_model(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "message-patrol-paused.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting, utc_now

    init_db()
    set_setting(
        "message_patrol_policy",
        {
            "enabled": True,
            "interval_seconds": 60,
            "cooldown_seconds": 0,
            "last_tick_at": "",
            "next_tick_at": "",
            "last_status": "",
            "updated_at": utc_now(),
        },
    )
    set_setting("automation_control", {"paused": True, "pause_reason": "暂停测试", "updated_at": utc_now()})
    monkeypatch.setattr(
        main,
        "client_for_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("message patrol tick must not call LLM")),
    )
    client = TestClient(main.app)

    response = client.post("/message-patrol/tick", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        patrol = conn.execute("SELECT status, checked_count, new_count, skipped_count, note FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
        captures = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        drafts = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
    assert patrol["status"] == "已暂停"
    assert patrol["checked_count"] == 0
    assert patrol["new_count"] == 0
    assert patrol["skipped_count"] == 1
    assert "未读取浏览器页面" in patrol["note"]
    assert captures == 0
    assert drafts == 0


def test_conversation_capture_skips_when_automation_paused(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "automation-paused.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting, utc_now

    init_db()
    set_setting("automation_control", {"paused": True, "pause_reason": "外出暂停", "updated_at": utc_now()})
    monkeypatch.setattr(
        main,
        "classify_conversation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("paused capture must not analyze text")),
    )
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/web/geek/chat",
            "title": "HR 对话",
            "text": "HR：可以聊一下工作内容和实习周期吗？",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["skipped"] is True
    assert payload["message_type"] == "自动化已暂停"
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
        patrol = conn.execute("SELECT status, checked_count, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture_count == 0
    assert draft_count == 0
    assert log["action_type"] == "automation_paused"
    assert log["status"] == "已暂停"
    assert "工作内容" not in log["summary"]
    assert "工作内容" not in log["decision_json"]
    assert patrol["status"] == "已暂停"
    assert patrol["checked_count"] == 1
    assert patrol["new_count"] == 0
    assert patrol["skipped_count"] == 1


def test_duplicate_conversation_capture_skips_llm_and_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "conversation-diff.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/diff-1.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳差分智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert job_response.status_code == 200

    llm_calls = {"count": 0}

    def count_llm_call(text, job, fallback):
        llm_calls["count"] += 1
        return fallback

    monkeypatch.setattr(main, "try_llm_conversation_decision", count_llm_call)
    conversation = "HR：您好，这里是深圳差分智能科技有限公司，想了解工作内容还是实习周期？\n我：想了解 AI 应用开发岗位。"
    first = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/diff-1.html",
            "title": "深圳差分智能科技有限公司 HR 对话",
            "text": conversation,
        },
    )
    second = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/diff-1.html",
            "title": "深圳差分智能科技有限公司 HR 对话",
            "text": conversation,
        },
    )
    changed = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/diff-1.html",
            "title": "深圳差分智能科技有限公司 HR 对话",
            "text": conversation + "\nHR：这边主要做 RAG 知识库和 Agent 工具调用。",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert changed.status_code == 200
    assert second.json()["skipped"] is True
    assert second.json()["message_type"] == "无新内容"
    assert "skipped" not in changed.json()
    assert llm_calls["count"] == 2
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        diff_log = conn.execute("SELECT action_type, status, summary FROM agent_action_logs WHERE action_type = 'conversation_diff_check' ORDER BY id DESC LIMIT 1").fetchone()
        patrol_rows = conn.execute("SELECT status, checked_count, new_count, skipped_count FROM message_patrol_runs ORDER BY id").fetchall()
    assert capture_count == 2
    assert draft_count == 2
    assert diff_log["status"] == "无新内容"
    assert "上一条采集一致" in diff_log["summary"]
    assert [row["status"] for row in patrol_rows] == ["已处理", "无新内容", "已处理"]
    assert [row["new_count"] for row in patrol_rows] == [1, 0, 1]
    assert [row["skipped_count"] for row in patrol_rows] == [0, 1, 0]


def test_message_patrol_observation_dry_run_records_metadata_without_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "patrol-observation-dry-run.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/patrol-dry-run.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳观察智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert job_response.status_code == 200
    monkeypatch.setattr(
        main,
        "classify_conversation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not classify conversation")),
    )
    monkeypatch.setattr(
        main,
        "try_llm_conversation_decision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dry-run must not call LLM")),
    )

    response = client.post(
        "/api/message-patrol/observations",
        json={
            "executor": "browser_extension",
            "dry_run": True,
            "observations": [
                {
                    "url": "https://www.zhipin.com/job_detail/patrol-dry-run.html",
                    "title": "深圳观察智能科技有限公司 HR 对话",
                    "platform": "Boss 直聘",
                    "text": "HR：您好，这里是深圳观察智能科技有限公司，请问你想了解工作内容还是实习周期？\n我：想了解 AI 应用开发岗位。",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["checked_count"] == 1
    assert payload["new_count"] == 1
    assert payload["skipped_count"] == 0
    assert payload["results"][0]["status"] == "观察完成"
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        patrol = conn.execute("SELECT status, checked_count, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
        action_log = conn.execute("SELECT action_type, status, summary, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture_count == 0
    assert draft_count == 0
    assert patrol["status"] == "观察完成"
    assert patrol["checked_count"] == 1
    assert patrol["new_count"] == 1
    assert patrol["skipped_count"] == 0
    assert action_log["action_type"] == "message_patrol_observation"
    assert action_log["status"] == "观察完成"
    assert "工作内容" not in action_log["summary"]
    assert "工作内容" not in action_log["decision_json"]


def test_message_patrol_observation_does_not_match_generic_title_across_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "patrol-generic-title.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/web/geek/jobs?query=ai%E5%BA%94%E7%94%A8%E5%BC%80%E5%8F%91",
            "title": "AI应用开发",
            "platform": "Boss 直聘",
            "text": "公司名称：新旦智能\n岗位名称：AI应用开发\n要求 Python、FastAPI、RAG。",
        },
    )
    assert job_response.status_code == 200

    response = client.post(
        "/api/message-patrol/observations",
        json={
            "executor": "edge_cdp",
            "dry_run": True,
            "observations": [
                {
                    "url": "https://c.liepin.com/?time=1786081283285",
                    "title": "我的首页_猎聘",
                    "platform": "猎聘",
                    "text_scope": "page_body",
                    "text": "我的沟通\nAI应用开发\n发简历\n赵先生您好！我们正在招聘储能销售经理，期待您的投递。",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["new_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["results"][0]["status"] == "无需回复"
    assert payload["results"][0]["job_id"] is None
    with connect() as conn:
        patrol = conn.execute("SELECT job_id, source_url, page_title, status FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
        action_log = conn.execute("SELECT decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert patrol["job_id"] is None
    assert patrol["source_url"] == "https://c.liepin.com/?time=1786081283285"
    assert patrol["page_title"] == "我的首页_猎聘"
    assert patrol["status"] == "无需回复"
    assert "job:" not in action_log["decision_json"]
    assert "https://c.liepin.com/" in action_log["decision_json"]


def test_message_patrol_observation_dry_run_skips_duplicate_without_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "patrol-observation-duplicate.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/patrol-duplicate.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳重复智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert job_response.status_code == 200
    llm_calls = {"count": 0}

    def count_llm_call(text, job, fallback):
        llm_calls["count"] += 1
        return fallback

    monkeypatch.setattr(main, "try_llm_conversation_decision", count_llm_call)
    conversation = "HR：您好，这里是深圳重复智能科技有限公司，请问你想了解工作内容还是实习周期？\n我：想了解 AI 应用开发岗位。"
    first = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/patrol-duplicate.html",
            "title": "深圳重复智能科技有限公司 HR 对话",
            "text": conversation,
        },
    )
    assert first.status_code == 200
    assert llm_calls["count"] == 1

    response = client.post(
        "/api/message-patrol/observations",
        json={
            "executor": "playwright",
            "dry_run": True,
            "observations": [
                {
                    "url": "https://www.zhipin.com/job_detail/patrol-duplicate.html",
                    "title": "深圳重复智能科技有限公司 HR 对话",
                    "text": conversation,
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["new_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["results"][0]["status"] == "无新内容"
    assert payload["results"][0]["existing_capture_id"] == first.json()["capture_id"]
    assert llm_calls["count"] == 1
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        patrol = conn.execute("SELECT status, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture_count == 1
    assert draft_count == 1
    assert patrol["status"] == "无新内容"
    assert patrol["new_count"] == 0
    assert patrol["skipped_count"] == 1


def test_message_patrol_observation_can_process_changed_content_when_not_dry_run(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "patrol-observation-process.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    monkeypatch.setattr(main, "try_llm_conversation_decision", lambda _text, _job, fallback: fallback)
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/patrol-process.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳处理智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert job_response.status_code == 200

    response = client.post(
        "/api/message-patrol/observations",
        json={
            "executor": "playwright",
            "dry_run": False,
            "trigger_type": "executor",
            "observations": [
                {
                    "url": "https://www.zhipin.com/job_detail/patrol-process.html",
                    "title": "深圳处理智能科技有限公司 HR 对话",
                    "text": "HR：您好，这里是深圳处理智能科技有限公司，请问你想了解主要工作内容还是实习周期？\n我：想先了解 AI 应用开发岗位。",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["dry_run"] is False
    assert payload["checked_count"] == 1
    assert payload["new_count"] == 1
    assert payload["results"][0]["capture_id"]
    assert payload["results"][0]["draft_id"]
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft = conn.execute("SELECT status, draft_type, message FROM message_drafts ORDER BY id DESC LIMIT 1").fetchone()
        patrol = conn.execute("SELECT trigger_type, scope, status, new_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture_count == 1
    assert draft["status"] == "待确认"
    assert draft["draft_type"] == "岗位沟通"
    assert draft["message"]
    assert patrol["trigger_type"] == "executor"
    assert patrol["scope"] == "scheduled_patrol"
    assert patrol["status"] == "已处理"
    assert patrol["new_count"] == 1


def test_message_patrol_observation_short_text_is_skipped_without_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "patrol-observation-short.sqlite3"))

    from app import main
    from app.db import connect, init_db

    init_db()
    monkeypatch.setattr(
        main,
        "classify_conversation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("short observation must not classify")),
    )
    client = TestClient(main.app)

    response = client.post(
        "/api/message-patrol/observations",
        json={
            "dry_run": True,
            "observations": [
                {
                    "url": "https://www.zhipin.com/web/geek/chat",
                    "title": "HR 对话",
                    "text": "发简历",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["checked_count"] == 1
    assert payload["new_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["results"][0]["status"] == "文本过短"
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        patrol = conn.execute("SELECT status, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture_count == 0
    assert draft_count == 0
    assert patrol["status"] == "文本过短"
    assert patrol["skipped_count"] == 1


def test_browser_patrol_dry_run_route_uses_open_edge_observations_without_capture(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "browser-patrol-dry-run.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting, utc_now

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    set_setting("automation_control", {"paused": True, "pause_reason": "只暂停后台自动化", "updated_at": utc_now()})
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/browser-patrol.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳浏览器巡检智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert job_response.status_code == 200

    def capture_patrol_without_async_loop(**_kwargs):
        assert_no_running_asyncio_loop("browser dry-run should run outside the FastAPI event loop")
        return [
            {
                "url": "https://www.zhipin.com/job_detail/browser-patrol.html",
                "title": "深圳浏览器巡检智能科技有限公司 HR 对话",
                "platform": "Boss 直聘",
                "text": "HR：您好，这里是深圳浏览器巡检智能科技有限公司，请问你想了解工作内容还是实习周期？\n我：想了解 AI 应用开发岗位。",
            }
        ]

    monkeypatch.setattr(
        main,
        "capture_browser_patrol_observations",
        capture_patrol_without_async_loop,
    )
    monkeypatch.setattr(
        main,
        "classify_conversation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("browser dry-run must not classify")),
    )

    response = client.post("/message-patrol/browser-dry-run", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/communications")
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        patrol = conn.execute("SELECT trigger_type, scope, status, checked_count, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
        action_log = conn.execute("SELECT action_type, status, decision_json FROM agent_action_logs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture_count == 0
    assert draft_count == 0
    assert patrol["trigger_type"] == "manual_browser"
    assert patrol["scope"] == "manual_browser_patrol"
    assert patrol["status"] == "观察完成"
    assert patrol["checked_count"] == 1
    assert patrol["new_count"] == 1
    assert patrol["skipped_count"] == 0
    assert action_log["action_type"] == "message_patrol_observation"
    assert action_log["status"] == "观察完成"
    assert "工作内容" not in action_log["decision_json"]

    second = client.post("/message-patrol/browser-dry-run", data={"return_to": "/communications"}, follow_redirects=False)

    assert second.status_code == 303
    with connect() as conn:
        capture_count = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        draft_count = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        patrol = conn.execute("SELECT status, checked_count, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert capture_count == 0
    assert draft_count == 0
    assert patrol["status"] == "无新内容"
    assert patrol["checked_count"] == 1
    assert patrol["new_count"] == 0
    assert patrol["skipped_count"] == 1


def test_message_patrol_dry_run_can_ignore_same_broadcast_message(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "patrol-ignore-broadcast.sqlite3"))

    from app import main
    from app.db import connect, get_setting, init_db

    init_db()
    client = TestClient(main.app)
    observation = {
        "url": "https://c.liepin.com/?time=1786081283285",
        "title": "我的首页_猎聘",
        "platform": "猎聘",
        "text": "卢女士 晶科能源有限公司\n储能销售经理\n赵先生您好！我们正在招聘储能销售经理，期待您的投递。\n发简历",
        "text_scope": "conversation_panel",
    }

    first = client.post(
        "/api/message-patrol/observations",
        json={
            "dry_run": True,
            "executor": "test",
            "trigger_type": "manual_browser",
            "scope": "manual_browser_patrol",
            "observations": [observation],
        },
    )

    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["results"][0]["status"] == "无需回复"
    assert first_payload["new_count"] == 0
    assert first_payload["skipped_count"] == 1
    run_id = first_payload["results"][0]["patrol_run_id"]
    page = client.get("/communications")
    assert page.status_code == 200
    assert "忽略相同消息" in page.text

    ignored = client.post(
        f"/message-patrol/runs/{run_id}/ignore",
        data={"return_to": "/communications"},
        follow_redirects=False,
    )

    assert ignored.status_code == 303
    assert ignored.headers["location"].startswith("/communications")
    ignored_settings = get_setting("ignored_message_fingerprints", {})
    assert ignored_settings

    second = client.post(
        "/api/message-patrol/observations",
        json={
            "dry_run": True,
            "executor": "test",
            "trigger_type": "manual_browser",
            "scope": "manual_browser_patrol",
            "observations": [observation],
        },
    )

    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["results"][0]["status"] == "已忽略"
    assert second_payload["new_count"] == 0
    assert second_payload["skipped_count"] == 1
    with connect() as conn:
        patrol = conn.execute("SELECT status, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
        captures = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        drafts = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
    assert patrol["status"] == "已忽略"
    assert patrol["new_count"] == 0
    assert patrol["skipped_count"] == 1
    assert captures == 0
    assert drafts == 0


def test_browser_patrol_skips_broad_liepin_home_body_without_panel():
    from app.services.browser_patrol import capture_page_observation

    class FakeLocator:
        def inner_text(self, timeout=5000):
            return "我的沟通\nAI应用开发\n发简历\n交换手机号\n赵先生您好！我们正在招聘储能销售经理，期待您的投递。"

    class FakePage:
        url = "https://c.liepin.com/?time=1786081283285"

        def title(self):
            return "我的首页_猎聘"

        def locator(self, _selector):
            return FakeLocator()

        def evaluate(self, _script):
            return ""

    assert capture_page_observation(FakePage()) is None


def test_browser_patrol_prefers_liepin_conversation_panel_over_page_body():
    from app.services.browser_patrol import capture_page_observation

    panel_text = "卢女士 晶科能源有限公司\n储能销售经理\n赵先生您好！我们正在招聘储能销售经理，期待您的投递。\n发简历"

    class FakeLocator:
        def inner_text(self, timeout=5000):
            return "我的首页_猎聘\nAI应用开发\n推荐职位\n" + panel_text

    class FakePage:
        url = "https://c.liepin.com/?time=1786081283285"

        def title(self):
            return "我的首页_猎聘"

        def locator(self, _selector):
            return FakeLocator()

        def evaluate(self, _script):
            return panel_text

    observation = capture_page_observation(FakePage())

    assert observation is not None
    assert observation["platform"] == "猎聘"
    assert observation["text"] == panel_text
    assert observation["text_scope"] == "conversation_panel"


def test_message_patrol_tick_uses_browser_executor_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "browser-patrol-tick.sqlite3"))

    from app import main
    from app.db import connect, get_setting, init_db, set_setting, utc_now

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    set_setting(
        "message_patrol_policy",
        {
            "enabled": True,
            "interval_seconds": 60,
            "cooldown_seconds": 0,
            "last_tick_at": "",
            "next_tick_at": "",
            "last_status": "",
            "updated_at": utc_now(),
        },
    )
    client = TestClient(main.app)
    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/browser-scheduled.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳定时巡检智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    assert job_response.status_code == 200

    def capture_tick_without_async_loop(**_kwargs):
        assert_no_running_asyncio_loop("message patrol tick should run outside the FastAPI event loop")
        return [
            {
                "url": "https://www.zhipin.com/job_detail/browser-scheduled.html",
                "title": "深圳定时巡检智能科技有限公司 HR 对话",
                "platform": "Boss 直聘",
                "text": "HR：您好，这里是深圳定时巡检智能科技有限公司，请问你想了解工作内容还是实习周期？\n我：想了解 AI 应用开发岗位。",
            }
        ]

    monkeypatch.setattr(
        main,
        "capture_browser_patrol_observations",
        capture_tick_without_async_loop,
    )
    monkeypatch.setattr(
        main,
        "try_llm_conversation_decision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scheduled browser dry-run must not call LLM")),
    )

    response = client.post("/message-patrol/tick", data={"return_to": "/communications"}, follow_redirects=False)

    assert response.status_code == 303
    with connect() as conn:
        patrol = conn.execute("SELECT trigger_type, scope, status, checked_count, new_count, skipped_count FROM message_patrol_runs ORDER BY id DESC LIMIT 1").fetchone()
        captures = conn.execute("SELECT COUNT(*) AS count FROM conversation_captures").fetchone()["count"]
        drafts = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
    policy = get_setting("message_patrol_policy", {})
    assert patrol["trigger_type"] == "manual_browser"
    assert patrol["scope"] == "scheduled_patrol"
    assert patrol["status"] == "观察完成"
    assert patrol["checked_count"] == 1
    assert patrol["new_count"] == 1
    assert patrol["skipped_count"] == 0
    assert captures == 0
    assert drafts == 0
    assert policy["last_status"] == "观察完成"
    assert policy["last_tick_at"]


def test_browser_patrol_open_route_delegates_to_controlled_edge(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "browser-patrol-open.sqlite3"))

    from app import main
    from app.db import init_db

    init_db()
    client = TestClient(main.app)
    opened = {}
    monkeypatch.setattr(main, "open_message_patrol_browser", lambda start_url="": opened.setdefault("url", start_url) or "about:blank")

    response = client.post(
        "/message-patrol/open-browser",
        data={"return_to": "/communications", "start_url": ""},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/communications")
    assert opened["url"] == ""


def test_autonomous_mode_pauses_after_followup_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-autonomous-limit.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting, utc_now

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    set_setting("communication_policy", {"mode": "autonomous", "max_auto_followups": 2})
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/autonomous-limit.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳自主智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    job_id = job_response.json()["job_id"]
    now = utc_now()
    with connect() as conn:
        for index in range(2):
            conn.execute(
                """
                INSERT INTO message_drafts (
                    job_id, platform, draft_type, status, reason,
                    message, risk_flags_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, "Boss 直聘", "岗位沟通", "已发送", f"历史第 {index + 1} 轮", "您好", "[]", now, now),
            )

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/autonomous-limit.html",
            "title": "深圳自主智能科技有限公司 HR 对话",
            "text": "HR：可以的，你想了解工作内容还是实习周期？",
        },
    )

    assert response.status_code == 200
    with connect() as conn:
        draft = conn.execute(
            "SELECT draft_type, status, message, communication_mode, followup_index, followup_limit, reason, risk_flags_json FROM message_drafts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert draft["draft_type"] == "自主询问暂停"
    assert draft["status"] == "需要我处理"
    assert draft["message"] == ""
    assert draft["communication_mode"] == "autonomous"
    assert draft["followup_index"] == 2
    assert draft["followup_limit"] == 2
    assert "2 轮上限" in draft["reason"]
    assert "2 轮上限" in draft["risk_flags_json"]


def test_autonomous_mode_creates_followup_candidate_with_round_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-autonomous-candidate.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    set_setting("communication_policy", {"mode": "autonomous", "max_auto_followups": 2})
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/autonomous-candidate.html",
            "title": "AI 应用开发实习生",
            "text": "公司名称：深圳候选智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG，每周 5 天。",
        },
    )
    job_id = job_response.json()["job_id"]
    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/job_detail/autonomous-candidate.html",
            "title": "深圳候选智能科技有限公司 HR 对话",
            "text": "HR：可以的，你想了解工作内容还是实习周期？",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["communication_mode"] == "autonomous"
    with connect() as conn:
        draft = conn.execute(
            "SELECT job_id, draft_type, status, communication_mode, followup_index, followup_limit, reason, message FROM message_drafts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert draft["job_id"] == job_id
    assert draft["draft_type"] == "自主询问候选"
    assert draft["status"] == "待确认"
    assert draft["communication_mode"] == "autonomous"
    assert draft["followup_index"] == 1
    assert draft["followup_limit"] == 2
    assert "已发送 0/2 轮" in draft["reason"]
    assert "主要工作内容" in draft["message"]


def test_autonomous_mode_pauses_when_job_not_matched(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "communication-autonomous-no-job.sqlite3"))

    from app import main
    from app.db import connect, init_db, set_setting

    init_db()
    set_setting("communication_policy", {"mode": "autonomous", "max_auto_followups": 2})
    client = TestClient(main.app)

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/web/geek/chat/no-job",
            "title": "未匹配 HR 对话",
            "text": "HR：可以的，你想了解工作内容还是实习周期？",
        },
    )

    assert response.status_code == 200
    with connect() as conn:
        draft = conn.execute(
            "SELECT draft_type, status, message, communication_mode, followup_index, followup_limit, reason, risk_flags_json FROM message_drafts ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert draft["draft_type"] == "自主询问暂停"
    assert draft["status"] == "需要我处理"
    assert draft["message"] == ""
    assert draft["communication_mode"] == "autonomous"
    assert draft["followup_index"] == 0
    assert draft["followup_limit"] == 2
    assert "未匹配到岗位" in draft["reason"]
    assert "未匹配到岗位" in draft["risk_flags_json"]


def test_extension_conversation_capture_marks_interview_invite(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "conversation-interview.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    init_db()
    client = TestClient(main.app)

    job_response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "job",
            "url": "https://www.zhipin.com/job_detail/chat-interview.html",
            "title": "AI Agent 开发实习生",
            "text": "公司名称：杭州面试智能科技有限公司\nAI Agent 开发实习生\n要求 Python、RAG、FastAPI。",
        },
    )
    job_id = job_response.json()["job_id"]

    response = client.post(
        "/api/extension/capture",
        json={
            "capture_type": "conversation",
            "url": "https://www.zhipin.com/web/geek/chat",
            "title": "杭州面试智能科技有限公司 HR 对话",
            "text": "HR：你对 AI Agent 开发实习生感兴趣的话，我们可以约一个线上面试时间，你什么时候方便？",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["message_type"] == "面试邀请"
    with connect() as conn:
        job = conn.execute("SELECT status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        draft = conn.execute("SELECT status, message, draft_type FROM message_drafts ORDER BY id DESC LIMIT 1").fetchone()
        event = conn.execute("SELECT event_type FROM application_events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    assert job["status"] == "面试邀请"
    assert draft["status"] == "需要我处理"
    assert draft["message"] == ""
    assert draft["draft_type"] == "面试邀请"
    assert event["event_type"] == "面试邀请识别"


def test_conversation_llm_unknown_type_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "conversation-llm-guard.sqlite3"))

    from app import main
    from app.db import init_db

    class FakeClient:
        configured = True

        def complete_json(self, _messages):
            return {"message_type": "乱码类型", "draft_message": "不应该使用"}

    init_db()
    monkeypatch.setattr(main, "client_for_task", lambda _task: FakeClient())
    fallback = {
        "message_type": "岗位沟通",
        "summary": "本地摘要",
        "action_required": False,
        "reason": "本地规则",
        "draft_message": "本地草稿",
        "risk_flags": [],
    }

    result = main.try_llm_conversation_decision("HR：想了解工作内容吗？", None, fallback)

    assert result == fallback


def test_analysis_prefers_rule_salary_over_bad_llm_salary(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "salary-guard.sqlite3"))

    from app import main
    from app.db import init_db

    init_db()
    monkeypatch.setattr(
        main,
        "try_llm_jd_extract",
        lambda _text: (
            {
                "title": "AI 应用开发实习生",
                "company": "杭州薪资智能科技有限公司",
                "salary_text": "每周 5 天",
                "required_skills": ["Python", "RAG"],
            },
            "",
        ),
    )
    result = main.analyze_job_payload(
        "公司名称：杭州薪资智能科技有限公司\nAI 应用开发实习生\n薪资：150～250元/天，每周 5 天，要求 Python RAG。",
        None,
        generate_messages=False,
    )

    assert result["extracted"]["salary_text"] == "150～250元/天"
    assert result["messages"] == {"message": "", "email": ""}


def test_search_run_and_candidate_import_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "search.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "search_company", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        main,
        "search_jobs_with_browser",
        lambda platform, keyword, city, browser_channel="msedge": SearchResult(
            platform=platform,
            keyword=keyword,
            city=city,
            search_url="https://jobs.example.com/search?q=AI",
            browser_channel=browser_channel,
            candidates=[
                SearchCandidate(
                    title="AI Agent 开发实习生",
                    company="杭州搜索智能科技有限公司",
                    city=city,
                    source_url="https://jobs.example.com/detail/1",
                    summary="AI Agent 开发实习生 Python RAG FastAPI",
                )
            ],
        ),
    )
    monkeypatch.setattr(
        main,
        "fetch_job_from_url",
        lambda _url, fetch_mode="auto", browser_channel="msedge": FetchResult(
            url="https://jobs.example.com/detail/1",
            final_url="https://jobs.example.com/detail/1",
            title="AI Agent 开发实习生",
            text="公司名称：杭州搜索智能科技有限公司\nAI Agent 开发实习生\n要求 Python、RAG、FastAPI，每周 5 天。",
            fetch_mode="http",
        ),
    )
    init_db()
    client = TestClient(main.app)

    page = client.get("/searches")
    assert page.status_code == 200
    assert "岗位搜索" in page.text

    search_response = client.post(
        "/searches",
        data={"platform": "Boss 直聘", "keyword": "AI Agent 实习", "city": "杭州", "browser_channel": "msedge"},
        follow_redirects=False,
    )
    assert search_response.status_code == 303
    detail = client.get(search_response.headers["location"])
    assert "已采集 1 个候选岗位" in detail.text
    assert "杭州搜索智能科技有限公司" in detail.text

    with connect() as conn:
        candidate_id = conn.execute("SELECT id FROM job_candidates LIMIT 1").fetchone()["id"]

    import_response = client.post(
        f"/candidates/{candidate_id}/import",
        data={"selected_resume_id": "1", "fetch_mode": "auto", "browser_channel": "msedge"},
        follow_redirects=False,
    )
    assert import_response.status_code == 303
    imported = client.get(import_response.headers["location"])
    assert "杭州搜索智能科技有限公司" in imported.text
    with connect() as conn:
        status = conn.execute("SELECT status FROM job_candidates WHERE id = ?", (candidate_id,)).fetchone()["status"]
    assert status == "已导入"


def test_manual_edge_search_capture_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "manual-search.sqlite3"))

    from app import main
    from app.db import connect, init_db

    monkeypatch.setattr(main, "open_manual_search_in_edge", lambda platform, keyword, city: "https://jobs.example.com/search?q=AI")
    def capture_without_async_loop(platform, keyword, city, browser_channel="msedge"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise AssertionError("browser capture should run outside the FastAPI event loop")
        return SearchResult(
            platform=platform,
            keyword=keyword,
            city=city,
            search_url="https://jobs.example.com/current",
            browser_channel=browser_channel,
            candidates=[
                SearchCandidate(
                    title="AI 应用开发实习生",
                    company="当前页面智能科技有限公司",
                    city=city,
                    source_url="https://jobs.example.com/current/1",
                    summary="AI 应用开发实习生 Python FastAPI RAG",
                )
            ],
        )

    monkeypatch.setattr(main, "capture_current_search_page", capture_without_async_loop)
    init_db()
    client = TestClient(main.app)

    opened = client.post(
        "/searches/open-manual",
        data={"platform": "Boss 直聘", "keyword": "AI 应用开发实习", "city": "杭州"},
        follow_redirects=False,
    )
    assert opened.status_code == 303
    assert "notice=" in opened.headers["location"]
    page_after_open = client.get("/searches")
    assert 'value="AI 应用开发实习"' in page_after_open.text
    assert 'value="杭州"' in page_after_open.text

    captured = client.post(
        "/searches/capture-current",
        data={"browser_channel": "msedge"},
        follow_redirects=False,
    )
    assert captured.status_code == 303
    detail = client.get(captured.headers["location"])
    assert "当前页面智能科技有限公司" in detail.text
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM job_candidates").fetchone()["count"]
        run = conn.execute("SELECT keyword, city FROM job_search_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert count == 1
    assert run["keyword"] == "AI 应用开发实习"
    assert run["city"] == "杭州"


def test_manual_edge_search_launch_uses_debug_profile(tmp_path, monkeypatch):
    from app.services import job_searcher

    launched = []

    class DummyProcess:
        pass

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(job_searcher, "find_edge_executable", lambda: Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"))
    monkeypatch.setattr(job_searcher, "is_debug_endpoint_ready", lambda timeout_seconds=1: False)
    monkeypatch.setattr(job_searcher, "wait_for_debug_endpoint", lambda timeout_seconds=8: True)
    monkeypatch.setattr(
        job_searcher.subprocess,
        "Popen",
        lambda args, **kwargs: launched.append((args, kwargs)) or DummyProcess(),
    )

    search_url = job_searcher.open_manual_search_in_edge("Boss 直聘", "AI 应用开发实习", "深圳")

    assert search_url.startswith("https://www.zhipin.com/")
    args, kwargs = launched[0]
    assert f"--remote-debugging-port={job_searcher.EDGE_DEBUG_PORT}" in args
    assert "--remote-allow-origins=*" in args
    assert "--new-window" in args
    assert any(str(tmp_path / "AIInternApplyAgent" / "browser" / "manual-msedge") in arg for arg in args)
    assert kwargs["stdout"] == job_searcher.subprocess.DEVNULL


def test_failed_auto_search_creates_failed_run(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "failed-search.sqlite3"))

    from app import main
    from app.db import connect, init_db

    def fail_search(*_args, **_kwargs):
        raise ValueError("Edge 未连接")

    monkeypatch.setattr(main, "search_jobs_with_browser", fail_search)
    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/searches",
        data={"platform": "Boss 直聘", "keyword": "AI Agent 实习", "city": "杭州", "browser_channel": "msedge"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/searches/" in response.headers["location"]
    detail = client.get(response.headers["location"])
    assert "Edge 未连接" in detail.text
    with connect() as conn:
        row = conn.execute("SELECT status, note FROM job_search_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "失败"
    assert "Edge 未连接" in row["note"]


def test_model_profile_blank_api_key_env_uses_provider_suggestion(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "settings.sqlite3"))

    from app import main  # noqa: F401
    from app.db import connect, init_db

    init_db()
    client = TestClient(main.app)

    response = client.post(
        "/settings/model-profiles",
        data={
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "",
            "model": "deepseek-v4-flash",
            "temperature": "0.2",
            "input_cost_per_million": "0",
            "output_cost_per_million": "0",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with connect() as conn:
        row = conn.execute("SELECT api_key_env FROM model_profiles WHERE name = ?", ("DeepSeek",)).fetchone()
    assert row["api_key_env"] == "DEEPSEEK_API_KEY"
