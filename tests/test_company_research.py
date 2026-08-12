from urllib.parse import unquote

from fastapi.testclient import TestClient

from app.services.research import SearchResult


def test_company_risk_research_is_manual_and_persists_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "company-research.sqlite3"))

    from app import main
    from app.db import connect, init_db

    calls = []

    def fake_search(company, title, city, depth):
        calls.append((company, title, city, depth))
        return [SearchResult(title="企业公开资料", url="https://example.test/company", summary="公开查询摘要")]

    monkeypatch.setattr(main, "search_company", fake_search)
    init_db()
    client = TestClient(main.app)

    created = client.post(
        "/jobs/analyze",
        data={
            "jd_text": "公司名称：杭州测试智能科技有限公司\nAI 应用开发实习生\n要求 Python、FastAPI、RAG。",
            "search_depth": "deep",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    job_id = int(created.headers["location"].split("/")[-1])
    assert calls == []
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM company_research WHERE job_id = ?", (job_id,)).fetchone()["count"] == 0

    reanalyzed = client.post(f"/jobs/{job_id}/reanalyze", data={}, follow_redirects=False)
    assert reanalyzed.status_code == 303
    assert calls == []
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM company_research WHERE job_id = ?", (job_id,)).fetchone()["count"] == 0

    researched = client.post(
        f"/jobs/{job_id}/company-research",
        data={"search_depth": "standard"},
        follow_redirects=False,
    )
    assert researched.status_code == 303
    assert calls == [("杭州测试智能科技有限公司", "AI 应用开发实习生", "杭州", "standard")]
    with connect() as conn:
        result = conn.execute(
            "SELECT source_title, source_url, summary FROM company_research WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        action = conn.execute(
            "SELECT action_type, status, decision_json FROM agent_action_logs WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    assert result["source_title"] == "企业公开资料"
    assert result["source_url"] == "https://example.test/company"
    assert result["summary"] == "公开查询摘要"
    assert action["action_type"] == "company_risk_research"
    assert action["status"] == "完成"
    assert '"user_triggered": true' in action["decision_json"]

    refreshed = client.post(
        f"/jobs/{job_id}/company-research",
        data={"search_depth": "quick"},
        follow_redirects=False,
    )
    assert refreshed.status_code == 303
    assert calls[-1] == ("杭州测试智能科技有限公司", "AI 应用开发实习生", "杭州", "quick")
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM company_research WHERE job_id = ?", (job_id,)).fetchone()["count"]
    assert count == 1


def test_company_risk_research_requires_company_name(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "company-research-missing.sqlite3"))

    from app import main
    from app.db import init_db

    called = False

    def fake_search(*_args):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(main, "search_company", fake_search)
    init_db()
    client = TestClient(main.app)
    created = client.post(
        "/jobs/analyze",
        data={"jd_text": "AI 应用开发实习生\n要求 Python 和 FastAPI。"},
        follow_redirects=False,
    )
    job_id = int(created.headers["location"].split("/")[-1])

    response = client.post(f"/jobs/{job_id}/company-research", follow_redirects=False)

    assert response.status_code == 303
    assert "公司名称为空" in unquote(response.headers["location"])
    assert not called
