import asyncio

from fastapi.testclient import TestClient


def create_job(conn, now):
    from app.db import dumps

    return conn.execute(
        """
        INSERT INTO job_postings (
            platform, title, company, city, salary_text, jd_text, extracted_json,
            match_score, match_level, risk_level, recommendation, status, skip_reason, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Boss 直聘",
            "AI 应用开发实习生",
            "示例记忆科技",
            "深圳",
            "220-300元/天",
            "这是不应进入岗位记忆的完整 JD 正文，包含页面原文和额外内容。",
            dumps(
                {
                    "scoring": {
                        "matched_skills": ["Python", "RAG", "FastAPI"],
                        "missing_skills": ["Docker"],
                        "fit_notes": ["检索与应用开发经验匹配。"],
                    }
                }
            ),
            86,
            "高匹配",
            "低",
            "必投",
            "待确认",
            "",
            now,
            now,
        ),
    ).lastrowid


def test_job_evidence_memory_saves_only_structured_summary_and_is_searchable(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "agent-memory.sqlite3"))
    from app.db import connect, init_db, utc_now
    from app.services.agent_memory import list_evidence_memories, save_job_decision_memory, search_evidence_memories

    init_db()
    with connect() as conn:
        job_id = create_job(conn, utc_now())

    saved = save_job_decision_memory(int(job_id))
    memories = list_evidence_memories()
    found = search_evidence_memories("示例记忆科技")
    natural_language_found = search_evidence_memories("查找之前记住的岗位结论")

    assert saved["status"] == "已保存"
    assert len(memories) == 1
    assert memories[0]["job_id"] == job_id
    assert "Python、RAG、FastAPI" in memories[0]["content"]
    assert "Docker" in memories[0]["content"]
    assert "完整 JD 正文" not in memories[0]["content"]
    assert "JD、简历、聊天或浏览器正文" in memories[0]["content"]
    assert [memory["id"] for memory in found] == [saved["memory_id"]]
    assert [memory["id"] for memory in natural_language_found] == [saved["memory_id"]]


def test_job_evidence_memory_updates_in_place_and_can_be_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "agent-memory-update.sqlite3"))
    from app.db import connect, init_db, utc_now
    from app.services.agent_memory import delete_evidence_memory, list_evidence_memories, save_job_decision_memory

    init_db()
    with connect() as conn:
        job_id = create_job(conn, utc_now())
    first = save_job_decision_memory(int(job_id))
    with connect() as conn:
        conn.execute("UPDATE job_postings SET match_score = ?, status = ? WHERE id = ?", (91, "已沟通", job_id))
    second = save_job_decision_memory(int(job_id))

    memories = list_evidence_memories()
    assert first["memory_id"] == second["memory_id"]
    assert second["status"] == "已更新"
    assert len(memories) == 1
    assert "91 分" in memories[0]["content"]
    assert "已沟通" in memories[0]["content"]
    assert delete_evidence_memory(first["memory_id"]) is True
    assert list_evidence_memories() == []


def test_evidence_memory_fts_is_backfilled_and_cleans_up_on_job_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "agent-memory-fts.sqlite3"))
    from app.db import connect, init_db, utc_now
    from app.services.agent_memory import save_job_decision_memory

    init_db()
    with connect() as conn:
        job_id = create_job(conn, utc_now())
    saved = save_job_decision_memory(int(job_id))

    init_db()
    with connect() as conn:
        fts_available = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_evidence_memory_fts'"
            ).fetchone()
        )
        if fts_available:
            indexed = conn.execute(
                "SELECT COUNT(*) AS count FROM agent_evidence_memory_fts WHERE memory_id = ?",
                (str(saved["memory_id"]),),
            ).fetchone()["count"]
            assert indexed == 1
        conn.execute("DELETE FROM job_postings WHERE id = ?", (job_id,))
        if fts_available:
            remaining = conn.execute(
                "SELECT COUNT(*) AS count FROM agent_evidence_memory_fts WHERE memory_id = ?",
                (str(saved["memory_id"]),),
            ).fetchone()["count"]
            assert remaining == 0


def test_agent_memory_write_requires_explicit_user_request(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "agent-memory-gate.sqlite3"))
    from app.db import connect, init_db, utc_now
    from app.services import desktop_agent
    from app.services.agent_memory import list_evidence_memories

    init_db()
    with connect() as conn:
        job_id = create_job(conn, utc_now())
    session = desktop_agent.create_session("岗位记忆闸门")
    with connect() as conn:
        conn.execute("UPDATE agent_sessions SET active_job_id = ? WHERE id = ?", (job_id, session["id"]))
    session = desktop_agent.get_session(int(session["id"]))
    assert session is not None
    message_id = desktop_agent.insert_message(int(session["id"]), "user", "测试", [])

    class FakeClient:
        profile = {}

    blocked = asyncio.run(
        desktop_agent.execute_tool(
            session,
            message_id,
            "save_job_evidence",
            {"job_id": int(job_id)},
            FakeClient(),
            "分析当前岗位，但不要保存任何内容",
        )
    )
    saved = asyncio.run(
        desktop_agent.execute_tool(
            session,
            message_id,
            "save_job_evidence",
            {"job_id": int(job_id)},
            FakeClient(),
            "请记住当前岗位的结论",
        )
    )

    assert blocked["status"] == "已阻止"
    assert saved["status"] == "已保存"
    assert len(list_evidence_memories()) == 1


def test_agent_memory_write_cannot_switch_away_from_current_job_without_explicit_id(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "agent-memory-target.sqlite3"))
    from app.db import connect, init_db, utc_now
    from app.services import desktop_agent
    from app.services.agent_memory import list_evidence_memories

    init_db()
    with connect() as conn:
        active_job_id = create_job(conn, utc_now())
        other_job_id = create_job(conn, utc_now())
    session = desktop_agent.create_session("岗位记忆目标")
    with connect() as conn:
        conn.execute("UPDATE agent_sessions SET active_job_id = ? WHERE id = ?", (active_job_id, session["id"]))
    session = desktop_agent.get_session(int(session["id"]))
    assert session is not None
    message_id = desktop_agent.insert_message(int(session["id"]), "user", "测试", [])

    class FakeClient:
        profile = {}

    mismatched = asyncio.run(
        desktop_agent.execute_tool(
            session,
            message_id,
            "save_job_evidence",
            {"job_id": int(other_job_id)},
            FakeClient(),
            "请记住当前岗位的结论",
        )
    )
    explicit = asyncio.run(
        desktop_agent.execute_tool(
            session,
            message_id,
            "save_job_evidence",
            {"job_id": int(other_job_id)},
            FakeClient(),
            f"请记住岗位 #{other_job_id} 的结论",
        )
    )

    assert mismatched["status"] == "已阻止"
    assert explicit["status"] == "已保存"
    assert [memory["job_id"] for memory in list_evidence_memories()] == [other_job_id]


def test_explicit_job_id_accepts_a_labeled_number_without_hash():
    from app.services.desktop_agent import explicit_job_id_in_request

    assert explicit_job_id_in_request("请记住岗位 12 的结论") == 12
    assert explicit_job_id_in_request("请记住职位 #13 的结论") == 13


def test_explicit_memory_request_does_not_treat_a_negated_save_as_authorization():
    from app.services.agent_memory import explicit_job_memory_request

    assert explicit_job_memory_request("请记住当前岗位的结论") is True
    assert explicit_job_memory_request("分析当前岗位，但不要保存任何内容") is False
    assert explicit_job_memory_request("暂不需要你保存这个岗位结论") is False


def test_memory_delete_api_updates_the_local_memory_list(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "agent-memory-api.sqlite3"))
    from app import main
    from app.db import connect, init_db, utc_now
    from app.services.agent_memory import save_job_decision_memory

    init_db()
    with connect() as conn:
        job_id = create_job(conn, utc_now())
    memory = save_job_decision_memory(int(job_id))

    response = TestClient(main.app).delete(f"/api/agent/memories/{memory['memory_id']}")

    assert response.status_code == 200
    assert response.json()["memories"] == []
