from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import TASK_TYPES, database_path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [row_to_dict(row) or {} for row in rows]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def loads(value: str | None, fallback: Any = None) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidate_profile (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL DEFAULT '',
                education TEXT NOT NULL DEFAULT '',
                github_url TEXT NOT NULL DEFAULT '',
                demo_url TEXT NOT NULL DEFAULT '',
                target_roles TEXT NOT NULL DEFAULT '[]',
                skills_json TEXT NOT NULL DEFAULT '[]',
                projects_json TEXT NOT NULL DEFAULT '[]',
                preferences_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS resume_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER,
                name TEXT NOT NULL,
                target_role TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL DEFAULT '',
                file_type TEXT NOT NULL DEFAULT '',
                parsed_text TEXT NOT NULL DEFAULT '',
                extracted_profile_json TEXT NOT NULL DEFAULT '{}',
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(profile_id) REFERENCES candidate_profile(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS job_postings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                salary_text TEXT NOT NULL DEFAULT '',
                internship_days TEXT NOT NULL DEFAULT '',
                internship_duration TEXT NOT NULL DEFAULT '',
                jd_text TEXT NOT NULL,
                extracted_json TEXT NOT NULL DEFAULT '{}',
                selected_resume_id INTEGER,
                match_score INTEGER NOT NULL DEFAULT 0,
                match_level TEXT NOT NULL DEFAULT '',
                risk_level TEXT NOT NULL DEFAULT '',
                recommendation TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '待分析',
                skip_reason TEXT NOT NULL DEFAULT '',
                generated_message TEXT NOT NULL DEFAULT '',
                generated_email TEXT NOT NULL DEFAULT '',
                analysis_error TEXT NOT NULL DEFAULT '',
                analysis_source TEXT NOT NULL DEFAULT 'local_rules',
                search_depth TEXT NOT NULL DEFAULT 'auto',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(selected_resume_id) REFERENCES resume_versions(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS company_research (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                company TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                source_title TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                risk_signals_json TEXT NOT NULL DEFAULT '[]',
                searched_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES job_postings(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS job_search_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL DEFAULT '',
                keyword TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                search_url TEXT NOT NULL DEFAULT '',
                browser_channel TEXT NOT NULL DEFAULT 'msedge',
                status TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_run_id INTEGER NOT NULL,
                job_id INTEGER,
                platform TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '候选',
                error_message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(search_run_id) REFERENCES job_search_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(job_id) REFERENCES job_postings(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS application_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES job_postings(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS conversation_captures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                platform TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                page_title TEXT NOT NULL DEFAULT '',
                raw_visible_text TEXT NOT NULL DEFAULT '',
                conversation_text TEXT NOT NULL DEFAULT '',
                ignored_lines_json TEXT NOT NULL DEFAULT '[]',
                message_type TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                action_required INTEGER NOT NULL DEFAULT 0,
                feedback_status TEXT NOT NULL DEFAULT '',
                expected_message_type TEXT NOT NULL DEFAULT '',
                feedback_note TEXT NOT NULL DEFAULT '',
                feedback_updated_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES job_postings(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS message_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                capture_id INTEGER,
                job_id INTEGER,
                platform TEXT NOT NULL DEFAULT '',
                draft_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '待确认',
                reason TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                risk_flags_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(capture_id) REFERENCES conversation_captures(id) ON DELETE SET NULL,
                FOREIGN KEY(job_id) REFERENCES job_postings(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS interview_preparations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER,
                source_text TEXT NOT NULL DEFAULT '',
                prep_plan_json TEXT NOT NULL DEFAULT '{}',
                question_bank_json TEXT NOT NULL DEFAULT '[]',
                review_markdown TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(job_id) REFERENCES job_postings(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS model_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                base_url TEXT NOT NULL DEFAULT '',
                api_key_env TEXT NOT NULL DEFAULT 'OPENAI_COMPATIBLE_API_KEY',
                model TEXT NOT NULL DEFAULT '',
                temperature REAL NOT NULL DEFAULT 0.2,
                input_cost_per_million REAL NOT NULL DEFAULT 0,
                output_cost_per_million REAL NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS model_routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_type TEXT NOT NULL UNIQUE,
                profile_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(profile_id) REFERENCES model_profiles(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS model_call_logs (
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

            CREATE TABLE IF NOT EXISTS app_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                value_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        ensure_columns(conn)
        seed_defaults(conn)


def ensure_columns(conn: sqlite3.Connection) -> None:
    job_columns = table_column_names(conn, "job_postings")
    if "analysis_source" not in job_columns:
        conn.execute("ALTER TABLE job_postings ADD COLUMN analysis_source TEXT NOT NULL DEFAULT 'local_rules'")

    conversation_columns = table_column_names(conn, "conversation_captures")
    conversation_defaults = {
        "raw_visible_text": "TEXT NOT NULL DEFAULT ''",
        "ignored_lines_json": "TEXT NOT NULL DEFAULT '[]'",
        "feedback_status": "TEXT NOT NULL DEFAULT ''",
        "expected_message_type": "TEXT NOT NULL DEFAULT ''",
        "feedback_note": "TEXT NOT NULL DEFAULT ''",
        "feedback_updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in conversation_defaults.items():
        if column not in conversation_columns:
            conn.execute(f"ALTER TABLE conversation_captures ADD COLUMN {column} {definition}")


def table_column_names(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def seed_defaults(conn: sqlite3.Connection) -> None:
    now = utc_now()
    profile_count = conn.execute("SELECT COUNT(*) FROM candidate_profile").fetchone()[0]
    if profile_count == 0:
        conn.execute(
            """
            INSERT INTO candidate_profile (
                name, education, github_url, demo_url, target_roles,
                skills_json, projects_json, preferences_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "",
                "智能科学与技术本科在读",
                "https://github.com/yuhui4756-hub",
                "https://github.com/yuhui4756-hub/ai-companion",
                dumps(["AI 应用开发实习", "Agent 开发实习", "AI 后端实习"]),
                dumps(["Python", "FastAPI", "SQLite", "RAG", "FTS5/BM25", "Ollama", "DeepSeek API", "pytest"]),
                dumps(
                    [
                        {
                            "name": "所依 - 本地优先 AI 伴侣与 RAG 知识库应用",
                            "highlights": [
                                "文档解析、结构化切片和混合检索",
                                "RAG 检索评测和来源 trace",
                                "本地优先隐私边界",
                            ],
                        }
                    ]
                ),
                dumps(
                    {
                        "cities": ["北京", "上海", "广州", "深圳", "杭州", "重庆", "成都"],
                        "min_salary_per_day": 150,
                        "internship_days": "5 天左右",
                    }
                ),
                now,
                now,
            ),
        )

    resume_count = conn.execute("SELECT COUNT(*) FROM resume_versions").fetchone()[0]
    if resume_count == 0:
        profile_id = conn.execute("SELECT id FROM candidate_profile ORDER BY id LIMIT 1").fetchone()[0]
        versions = [
            ("AI 应用开发版", "AI 应用开发实习", 1),
            ("Agent 开发版", "Agent 开发实习", 0),
            ("AI 后端版", "AI 后端 / 大模型应用后端实习", 0),
            ("通用版", "AI 应用相关实习", 0),
        ]
        for name, role, is_default in versions:
            conn.execute(
                """
                INSERT INTO resume_versions (
                    profile_id, name, target_role, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (profile_id, name, role, is_default, now, now),
            )

    model_count = conn.execute("SELECT COUNT(*) FROM model_profiles").fetchone()[0]
    if model_count == 0:
        conn.execute(
            """
            INSERT INTO model_profiles (
                name, base_url, api_key_env, model, temperature,
                input_cost_per_million, output_cost_per_million, is_default,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "默认 OpenAI-compatible",
                "",
                "OPENAI_COMPATIBLE_API_KEY",
                "",
                0.2,
                0,
                0,
                1,
                now,
                now,
            ),
        )

    default_profile = conn.execute("SELECT id FROM model_profiles WHERE is_default = 1 LIMIT 1").fetchone()
    profile_id = default_profile[0] if default_profile else None
    for task_type, _label in TASK_TYPES:
        exists = conn.execute("SELECT id FROM model_routes WHERE task_type = ?", (task_type,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO model_routes (task_type, profile_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (task_type, profile_id, now, now),
            )

    default_settings = {
        "communication_policy": {
            "mode": "draft",
            "max_auto_followups": 2,
        },
        "blacklist_companies": [],
        "blacklist_keywords": [
            "培训费",
            "贷款",
            "押金",
            "无薪",
            "课程顾问",
            "电话销售",
            "邀约",
            "拉新",
            "地推",
            "兼职推广",
            "收费内推",
            "包装简历",
        ],
    }
    for key, value in default_settings.items():
        exists = conn.execute("SELECT id FROM app_settings WHERE key = ?", (key,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO app_settings (key, value_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (key, dumps(value), now, now),
            )


def fetch_one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute(query, params).fetchone())


def fetch_all(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return rows_to_dicts(conn.execute(query, params).fetchall())


def get_setting(key: str, fallback: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value_json FROM app_settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return fallback
    return loads(row["value_json"], fallback)


def set_setting(key: str, value: Any) -> None:
    now = utc_now()
    with connect() as conn:
        existing = conn.execute("SELECT id FROM app_settings WHERE key = ?", (key,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE app_settings SET value_json = ?, updated_at = ? WHERE key = ?",
                (dumps(value), now, key),
            )
        else:
            conn.execute(
                "INSERT INTO app_settings (key, value_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (key, dumps(value), now, now),
            )
