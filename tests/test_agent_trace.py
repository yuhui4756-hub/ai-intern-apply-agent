import asyncio
import json

import pytest

from app.db import connect, init_db, utc_now
from app.services.llm import LLMResult, OpenAICompatibleClient, model_trace


def test_init_db_adds_trace_columns_and_indexes_to_an_existing_database(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-agent.sqlite3"
    monkeypatch.setenv("APP_DB_PATH", str(db_path))
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE model_call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL,
                model_profile TEXT NOT NULL DEFAULT '',
                model_name TEXT NOT NULL DEFAULT '',
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost REAL NOT NULL DEFAULT 0,
                usage_source TEXT NOT NULL DEFAULT 'estimated',
                status TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE agent_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                model_profile_id INTEGER,
                auto_communication INTEGER NOT NULL DEFAULT 0,
                active_job_id INTEGER,
                summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE agent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                events_json TEXT NOT NULL DEFAULT '[]',
                model_profile_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE agent_tool_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                message_id INTEGER,
                tool_name TEXT NOT NULL DEFAULT '',
                permission_level TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                arguments_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )

    init_db()
    with connect() as conn:
        model_columns = {row["name"] for row in conn.execute("PRAGMA table_info(model_call_logs)")}
        message_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_messages)")}
        tool_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_tool_runs)")}
        indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}

    assert "trace_id" in model_columns
    assert "trace_id" in message_columns
    assert "trace_id" in tool_columns
    assert {"idx_model_call_logs_trace", "idx_agent_messages_trace", "idx_agent_tool_runs_trace"} <= indexes


def test_model_call_log_uses_current_local_trace_without_storing_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "trace.sqlite3"))
    init_db()
    client = OpenAICompatibleClient(
        {
            "name": "测试模型",
            "base_url": "https://example.invalid",
            "api_key_env": "TEST_AGENT_TRACE_KEY",
            "model": "test-model",
        },
        "agent_chat",
    )

    with model_trace("agent-test-trace"):
        client.log_call(LLMResult(content="不保存输入内容", input_tokens=12, output_tokens=5, usage_source="test"), "success", "")

    with connect() as conn:
        row = conn.execute("SELECT trace_id, task_type, input_tokens, output_tokens, error_message FROM model_call_logs").fetchone()

    assert dict(row) == {
        "trace_id": "agent-test-trace",
        "task_type": "agent_chat",
        "input_tokens": 12,
        "output_tokens": 5,
        "error_message": "",
    }


def test_invalid_json_is_logged_once_with_usage_as_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "trace-invalid-json.sqlite3"))
    init_db()
    client = OpenAICompatibleClient(
        {
            "name": "测试模型",
            "base_url": "https://example.invalid",
            "api_key_env": "TEST_AGENT_TRACE_KEY",
            "model": "test-model",
        },
        "agent_chat",
    )
    monkeypatch.setattr(
        client,
        "complete",
        lambda *_args, **_kwargs: LLMResult(content="not-json", input_tokens=9, output_tokens=4, usage_source="test"),
    )

    with model_trace("agent-invalid-json"), pytest.raises(json.JSONDecodeError):
        client.complete_json([])

    with connect() as conn:
        rows = conn.execute(
            "SELECT trace_id, status, input_tokens, output_tokens, error_message FROM model_call_logs"
        ).fetchall()

    assert len(rows) == 1
    row = dict(rows[0])
    assert row["trace_id"] == "agent-invalid-json"
    assert row["status"] == "error"
    assert row["input_tokens"] == 9
    assert row["output_tokens"] == 4
    assert "JSON parse failed" in row["error_message"]


def test_agent_harness_does_not_duplicate_a_logged_json_parse_error(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "trace-harness-invalid-json.sqlite3"))
    from app.services import desktop_agent

    init_db()
    client = OpenAICompatibleClient(
        {
            "name": "测试模型",
            "base_url": "https://example.invalid",
            "api_key_env": "TEST_AGENT_TRACE_KEY",
            "model": "test-model",
        },
        "agent_chat",
    )
    monkeypatch.setattr(
        client,
        "complete",
        lambda *_args, **_kwargs: LLMResult(content="not-json", input_tokens=7, output_tokens=3, usage_source="test"),
    )

    with model_trace("agent-harness-invalid-json"):
        plan, events, response = asyncio.run(desktop_agent.request_contract_plan(client, []))

    with connect() as conn:
        rows = conn.execute(
            "SELECT trace_id, status, input_tokens, output_tokens, error_message FROM model_call_logs"
        ).fetchall()

    assert plan is None
    assert response == "模型调用失败，本轮没有执行任何工具。"
    assert events[0]["kind"] == "模型"
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["trace_id"] == "agent-harness-invalid-json"
    assert row["status"] == "error"
    assert (row["input_tokens"], row["output_tokens"]) == (7, 3)
