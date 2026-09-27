from fastapi.testclient import TestClient


def test_calibration_dashboard_separates_page_observations_from_human_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "calibration-dashboard.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now
    from app.services.job_searcher import SearchCandidate, SearchResult

    init_db()
    now = utc_now()
    boss_run = main.save_search_result(
        SearchResult(
            platform="Boss 直聘",
            keyword="AI 应用开发实习",
            city="深圳",
            search_url="https://www.zhipin.com/web/geek/jobs?query=AI",
            browser_channel="msedge",
            candidates=[
                SearchCandidate(
                    title="Agent Python 后端开发",
                    company="Java",
                    city="深圳",
                    source_url="https://www.zhipin.com/job_detail/dashboard-1.html",
                    summary="Agent Python 后端开发\n薪资数字未能从页面文本读取\nPython FastAPI",
                )
            ],
        )
    )
    empty_run = main.save_search_result(
        SearchResult(
            platform="猎聘",
            keyword="Python",
            city="深圳",
            search_url="https://www.liepin.com/zhaopin/?key=Python&city=深圳",
            browser_channel="msedge",
            candidates=[],
            note="已登录页面显示暂无合适职位。",
        )
    )
    with connect() as conn:
        candidate_id = conn.execute(
            "SELECT id FROM job_candidates WHERE search_run_id = ?", (boss_run,)
        ).fetchone()["id"]
        conn.execute(
            """
            UPDATE job_candidates
            SET feedback_status = ?, expected_screening = ?, feedback_note = ?, feedback_updated_at = ?
            WHERE id = ?
            """,
            ("误判", "人工复核", "公司字段为技术词，需要人工确认。", now, candidate_id),
        )
        imported_job_id = conn.execute(
            """
            INSERT INTO job_postings (
                platform, title, company, city, jd_text, extracted_json, match_score,
                match_level, risk_level, recommendation, status, analysis_source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Boss 直聘",
                "AI Agent 开发实习生",
                "校准详情科技",
                "深圳",
                "Python RAG FastAPI",
                '{"extracted":{"required_skills":["Python","RAG"]},"scoring":{}}',
                82,
                "高匹配",
                "低",
                "必投",
                "待确认",
                "llm_plus_rules",
                now,
                now,
            ),
        ).lastrowid
        conn.execute("UPDATE job_candidates SET job_id = ?, status = ? WHERE id = ?", (imported_job_id, "已导入", candidate_id))
        before_jobs = conn.execute("SELECT COUNT(*) AS count FROM job_postings").fetchone()["count"]
        before_logs = conn.execute("SELECT COUNT(*) AS count FROM model_call_logs").fetchone()["count"]
        report = main.calibration_quality_report(conn)
        after_jobs = conn.execute("SELECT COUNT(*) AS count FROM job_postings").fetchone()["count"]
        after_logs = conn.execute("SELECT COUNT(*) AS count FROM model_call_logs").fetchone()["count"]

    assert report["search_summary"] == {
        "run_count": 2,
        "completed_count": 1,
        "empty_count": 1,
        "failed_count": 0,
        "candidate_run_count": 1,
        "candidate_count": 1,
        "candidate_yield_percent": 50,
    }
    boss = next(item for item in report["platform_summary"] if item["platform"] == "Boss 直聘")
    liepin = next(item for item in report["platform_summary"] if item["platform"] == "猎聘")
    assert boss["candidate_count"] == 1
    assert liepin["empty_count"] == 1
    assert report["field_health"]["flag_counts"] == [
        {"label": "公司字段疑似非公司", "count": 1},
        {"label": "薪资未解析", "count": 1},
    ]
    assert report["detail_quality"] == {
        "candidate_count": 1,
        "imported_count": 1,
        "import_rate_percent": 100,
        "pending_count": 0,
        "failed_count": 0,
        "score_levels": [{"label": "高匹配", "count": 1}],
        "risk_levels": [{"label": "低", "count": 1}],
        "source_summary": [{"label": "llm_plus_rules", "count": 1}],
        "skill_extraction_coverage_percent": 100,
        "average_required_skill_count": 2.0,
    }
    assert report["candidate_feedback"]["reviewed_count"] == 1
    assert report["candidate_feedback"]["feedback_counts"] == {"误判": 1}
    assert before_jobs == after_jobs
    assert before_logs == after_logs
    assert empty_run != boss_run


def test_calibration_dashboard_page_and_json_export_are_local_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "calibration-dashboard-page.sqlite3"))
    from app import main
    from app.db import init_db

    init_db()
    client = TestClient(main.app)

    page = client.get("/calibration")
    exported = client.get("/calibration/export.json")

    assert page.status_code == 200
    assert "质量校准报告" in page.text
    assert "读数口径" in page.text
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["ok"] is True
    assert payload["report"]["search_summary"]["run_count"] == 0
