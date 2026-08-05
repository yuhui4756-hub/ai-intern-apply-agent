import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.services.job_fetcher import FetchResult
from app.services.job_searcher import SearchCandidate, SearchResult


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
        row = conn.execute("SELECT platform, source_url, company, match_level FROM job_postings ORDER BY id DESC LIMIT 1").fetchone()
        event = conn.execute("SELECT event_type FROM application_events ORDER BY id DESC LIMIT 1").fetchone()
    assert row["platform"] == "Boss 直聘"
    assert row["source_url"] == "https://www.zhipin.com/job_detail/example.html"
    assert row["company"] == "深圳扩展智能科技有限公司"
    assert row["match_level"]
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
