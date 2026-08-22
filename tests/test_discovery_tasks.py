from app.services.job_fetcher import FetchResult
from app.services.job_searcher import SearchCandidate, SearchResult


def _result(platform: str, keyword: str, city: str, suffix: str) -> SearchResult:
    return SearchResult(
        platform=platform,
        keyword=keyword,
        city=city,
        search_url=f"https://jobs.example.com/search/{suffix}",
        browser_channel="msedge",
        candidates=[
            SearchCandidate(
                title="AI Agent 开发实习生",
                company=f"测试智能科技 {suffix}",
                city=city,
                source_url=f"https://jobs.example.com/detail/{suffix}",
                summary="AI Agent 开发实习生，Python FastAPI RAG，每周 5 天，实习 3 个月。",
            )
        ],
    )


def _detail(url: str) -> FetchResult:
    suffix = url.rsplit("/", 1)[-1]
    return FetchResult(
        url=url,
        final_url=url,
        title="AI Agent 开发实习生",
        text=(
            f"公司名称：测试智能科技 {suffix}\n"
            "AI Agent 开发实习生\n"
            "要求 Python、FastAPI、RAG，每周到岗 5 天，实习 3 个月。"
        ),
        fetch_mode="controlled_edge",
    )


def _prepare(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "discovery-task.sqlite3"))
    from app import main
    from app.db import init_db

    init_db()
    monkeypatch.setattr(main, "search_company", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(main, "try_llm_jd_extract", lambda _text: ({}, ""))
    return main


def test_discovery_task_persists_steps_and_keeps_outbound_actions_disabled(tmp_path, monkeypatch):
    main = _prepare(tmp_path, monkeypatch)
    from app.db import connect

    search_calls = []

    def fake_search(platform, keyword, city, limit):
        search_calls.append((platform, keyword, city, limit))
        return _result(platform, keyword, city, str(len(search_calls)))

    monkeypatch.setattr(main, "search_jobs_in_controlled_edge", fake_search)
    monkeypatch.setattr(main, "fetch_job_from_controlled_edge", _detail)

    task_id = main.create_controlled_job_discovery_task({"role": "AI 应用开发实习", "city": "杭州"})
    result = main.execute_controlled_job_discovery_task(task_id)

    assert result["status"] == "完成"
    assert len(search_calls) == main.JOB_DISCOVERY_SEARCH_PAGE_LIMIT
    with connect() as conn:
        task = conn.execute("SELECT * FROM job_discovery_tasks WHERE id = ?", (task_id,)).fetchone()
        steps = conn.execute(
            "SELECT phase, status FROM job_discovery_task_steps WHERE discovery_task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        runs = conn.execute("SELECT discovery_task_id FROM job_search_runs WHERE discovery_task_id = ?", (task_id,)).fetchall()
        jobs = conn.execute("SELECT COUNT(*) AS count FROM job_postings").fetchone()["count"]
        drafts = conn.execute("SELECT COUNT(*) AS count FROM message_drafts").fetchone()["count"]
        preparations = conn.execute("SELECT COUNT(*) AS count FROM application_preparations").fetchone()["count"]
    assert task["status"] == "完成"
    assert task["imported_count"] == 3
    assert [row["phase"] for row in steps] == ["搜索", "搜索", "搜索", "JD", "JD", "JD"]
    assert all(row["status"] == "完成" for row in steps)
    assert len(runs) == 3
    assert jobs == 3
    assert drafts == 0
    assert preparations == 0


def test_discovery_task_pause_cancel_and_resume_boundaries(tmp_path, monkeypatch):
    main = _prepare(tmp_path, monkeypatch)
    from app.db import connect

    calls = []
    task_id = main.create_controlled_job_discovery_task({"role": "Agent 开发实习", "city": "杭州"})

    def pause_after_first_search(platform, keyword, city, limit):
        calls.append(platform)
        if len(calls) == 1:
            ok, _message = main.pause_discovery_task(task_id)
            assert ok
        return _result(platform, keyword, city, str(len(calls)))

    monkeypatch.setattr(main, "search_jobs_in_controlled_edge", pause_after_first_search)
    monkeypatch.setattr(main, "fetch_job_from_controlled_edge", _detail)
    paused = main.execute_controlled_job_discovery_task(task_id)
    assert paused["status"] == "已暂停"
    assert calls == ["Boss 直聘"]

    monkeypatch.setattr(main, "schedule_discovery_task", lambda _task_id: True)
    resumed, _message = main.resume_discovery_task(task_id)
    assert resumed
    completed = main.execute_controlled_job_discovery_task(task_id)
    assert completed["status"] == "完成"
    assert len(calls) == 3

    cancel_task_id = main.create_controlled_job_discovery_task({"role": "AI 后端实习", "city": "深圳"})
    cancel_calls = []

    def cancel_after_first_search(platform, keyword, city, limit):
        cancel_calls.append(platform)
        if len(cancel_calls) == 1:
            ok, _message = main.cancel_discovery_task(cancel_task_id)
            assert ok
        return _result(platform, keyword, city, f"cancel-{len(cancel_calls)}")

    monkeypatch.setattr(main, "search_jobs_in_controlled_edge", cancel_after_first_search)
    cancelled = main.execute_controlled_job_discovery_task(cancel_task_id)
    assert cancelled["status"] == "已取消"
    assert cancel_calls == ["Boss 直聘"]
    with connect() as conn:
        pending_count = conn.execute(
            "SELECT COUNT(*) AS count FROM job_discovery_task_steps WHERE discovery_task_id = ? AND status = '待执行'",
            (cancel_task_id,),
        ).fetchone()["count"]
        cancelled_count = conn.execute(
            "SELECT COUNT(*) AS count FROM job_discovery_task_steps WHERE discovery_task_id = ? AND status = '已取消'",
            (cancel_task_id,),
        ).fetchone()["count"]
    assert pending_count == 0
    assert cancelled_count == 2


def test_discovery_task_retries_failed_search_and_replays_into_new_task(tmp_path, monkeypatch):
    main = _prepare(tmp_path, monkeypatch)
    from app.db import connect

    calls = []

    def flaky_search(platform, keyword, city, limit):
        calls.append(platform)
        if platform == "Boss 直聘" and calls.count(platform) == 1:
            raise ValueError("受控 Edge 暂时不可用")
        return _result(platform, keyword, city, f"retry-{len(calls)}")

    monkeypatch.setattr(main, "search_jobs_in_controlled_edge", flaky_search)
    monkeypatch.setattr(main, "fetch_job_from_controlled_edge", _detail)
    task_id = main.create_controlled_job_discovery_task({"role": "AI 应用开发实习", "city": "北京"})
    first = main.execute_controlled_job_discovery_task(task_id)
    assert first["status"] == "部分完成"

    monkeypatch.setattr(main, "schedule_discovery_task", lambda _task_id: True)
    resumed, _message = main.resume_discovery_task(task_id)
    assert resumed
    second = main.execute_controlled_job_discovery_task(task_id)
    assert second["status"] == "完成"
    assert calls.count("Boss 直聘") == 2

    replay_id, message = main.replay_discovery_task(task_id)
    assert replay_id and replay_id != task_id
    assert "完整回放" in message
    with connect() as conn:
        replay = conn.execute("SELECT replay_of_task_id, status FROM job_discovery_tasks WHERE id = ?", (replay_id,)).fetchone()
        original_steps = conn.execute(
            "SELECT COUNT(*) AS count FROM job_discovery_task_steps WHERE discovery_task_id = ?", (task_id,)
        ).fetchone()["count"]
        replay_steps = conn.execute(
            "SELECT COUNT(*) AS count FROM job_discovery_task_steps WHERE discovery_task_id = ?", (replay_id,)
        ).fetchone()["count"]
    assert replay["replay_of_task_id"] == task_id
    assert replay["status"] == "待执行"
    assert original_steps == 6
    assert replay_steps == main.JOB_DISCOVERY_SEARCH_PAGE_LIMIT


def test_job_linking_does_not_mark_navigation_anchor_as_imported(tmp_path, monkeypatch):
    main = _prepare(tmp_path, monkeypatch)
    from app.db import connect, utc_now

    now = utc_now()
    with connect() as conn:
        job_id = conn.execute(
            """
            INSERT INTO job_postings (source_url, title, company, jd_text, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("https://jobs.example.com/detail/1", "AI 应用开发实习生", "验证智能科技", "Python RAG", now, now),
        ).lastrowid
        run_id = conn.execute(
            """
            INSERT INTO job_search_runs (platform, keyword, city, browser_channel, status, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("Boss 直聘", "AI 应用开发实习", "杭州", "msedge", "完成", "", now),
        ).lastrowid
        valid_id = conn.execute(
            """
            INSERT INTO job_candidates (search_run_id, platform, title, company, source_url, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, "Boss 直聘", "AI 应用开发实习生", "验证智能科技", "https://jobs.example.com/detail/1", "候选", now, now),
        ).lastrowid
        navigation_id = conn.execute(
            """
            INSERT INTO job_candidates (search_run_id, platform, title, company, source_url, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, "Boss 直聘", "查看更多信息", "举报", "https://jobs.example.com/detail/1", "候选", now, now),
        ).lastrowid
        linked_count = main.link_candidates_to_job(conn, "https://jobs.example.com/detail/1", int(job_id))
        valid = conn.execute("SELECT job_id, status FROM job_candidates WHERE id = ?", (valid_id,)).fetchone()
        navigation = conn.execute("SELECT job_id, status FROM job_candidates WHERE id = ?", (navigation_id,)).fetchone()

    assert linked_count == 1
    assert valid["job_id"] == job_id
    assert valid["status"] == "已导入"
    assert navigation["job_id"] is None
    assert navigation["status"] == "候选"
