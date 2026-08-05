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
