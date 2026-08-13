from fastapi.testclient import TestClient

from app.services.job_searcher import SearchCandidate, SearchResult


def test_candidate_calibration_report_summarizes_feedback_without_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "candidate-calibration-report.sqlite3"))

    from app import main
    from app.db import connect, init_db, utc_now

    init_db()
    liepin_run = main.save_search_result(
        SearchResult(
            platform="猎聘",
            keyword="Agent 开发实习",
            city="北京",
            search_url="https://www.liepin.com/zhaopin/?key=agent",
            browser_channel="msedge",
            candidates=[
                SearchCandidate("AI Agent 开发实习生", "北京测试科技", "北京", "https://www.liepin.com/job/1", "Python RAG"),
                SearchCandidate("职位专场导航", "", "北京", "https://www.liepin.com/career", "不是具体岗位"),
                SearchCandidate("待观察岗位", "北京测试科技", "北京", "https://www.liepin.com/job/3", "等待详情"),
            ],
        )
    )
    boss_run = main.save_search_result(
        SearchResult(
            platform="Boss 直聘",
            keyword="AI 应用开发实习",
            city="杭州",
            search_url="https://www.zhipin.com/web/geek/job?query=ai",
            browser_channel="msedge",
            candidates=[SearchCandidate("AI 应用开发实习生", "杭州测试智能", "杭州", "https://www.zhipin.com/job_detail/1.html", "FastAPI")],
        )
    )
    now = utc_now()
    with connect() as conn:
        liepin_candidates = conn.execute(
            "SELECT id, title FROM job_candidates WHERE search_run_id = ? ORDER BY id", (liepin_run,)
        ).fetchall()
        boss_candidate = conn.execute(
            "SELECT id FROM job_candidates WHERE search_run_id = ?", (boss_run,)
        ).fetchone()
        conn.execute(
            "UPDATE job_candidates SET feedback_status = ?, feedback_updated_at = ? WHERE id = ?",
            ("正确", now, liepin_candidates[0]["id"]),
        )
        conn.execute(
            """
            UPDATE job_candidates
            SET feedback_status = ?, expected_screening = ?, feedback_note = ?, feedback_updated_at = ?
            WHERE id = ?
            """,
            ("误判", "跳过", "导航链接不是岗位", now, liepin_candidates[1]["id"]),
        )
        conn.execute(
            "UPDATE job_candidates SET feedback_status = ?, feedback_updated_at = ? WHERE id = ?",
            ("待观察", now, liepin_candidates[2]["id"]),
        )
        conn.execute(
            "UPDATE job_candidates SET status = ? WHERE id = ?",
            ("详情待补充", boss_candidate["id"]),
        )
        before_logs = conn.execute("SELECT COUNT(*) AS count FROM agent_action_logs").fetchone()["count"]
        before_models = conn.execute("SELECT COUNT(*) AS count FROM model_call_logs").fetchone()["count"]

        report = main.candidate_calibration_report(conn)

    assert report["total_candidates"] == 4
    assert report["reviewed_count"] == 3
    assert report["feedback_counts"] == {"正确": 1, "误判": 1, "待观察": 1}
    assert report["detail_pending_count"] == 1
    assert report["expected_summary"] == [{"expected_screening": "跳过", "count": 1}]
    liepin = next(item for item in report["platform_summary"] if item["platform"] == "猎聘")
    assert liepin["precision_percent"] == 50
    assert liepin["observing_count"] == 1
    assert report["recent_misclassifications"][0]["feedback_note"] == "导航链接不是岗位"

    client = TestClient(main.app)
    page = client.get("/calibration/candidates")

    assert page.status_code == 200
    assert "候选校准报告" in page.text
    assert "已标注准确率" in page.text
    assert "导航链接不是岗位" in page.text
    with connect() as conn:
        after_logs = conn.execute("SELECT COUNT(*) AS count FROM agent_action_logs").fetchone()["count"]
        after_models = conn.execute("SELECT COUNT(*) AS count FROM model_call_logs").fetchone()["count"]
    assert after_logs == before_logs
    assert after_models == before_models
