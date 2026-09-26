from __future__ import annotations

"""Local, evidence-scoped memory for the chat-first job Agent.

This is intentionally not a transcript store. A memory is created only after
the user explicitly asks to keep a current job conclusion. Its content is
derived from saved structured fields, never the full JD, resume, HR chat,
browser page text, file paths, or screenshots.
"""

import re
import sqlite3
from typing import Any

from ..db import connect, loads, utc_now


MAX_MEMORY_CONTENT = 900
MAX_MEMORY_RESULTS = 8
MEMORY_QUERY_ANCHORS = (
    "岗位",
    "职位",
    "公司",
    "结论",
    "风险",
    "面试",
    "投递",
    "沟通",
    "Python",
    "RAG",
    "Agent",
)
SENSITIVE_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),
    re.compile(r"[\w.+-]+@[\w.-]+"),
    re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|(?:api[_\s-]*key|password|密码|token)\s*[:=]|bearer\s+[a-z0-9._-]{12,})"),
)


def compact(value: Any, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]


def redact_memory_text(value: Any, limit: int = MAX_MEMORY_CONTENT) -> str:
    text = str(value or "")
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub("[已脱敏]", text)
    return compact(text, limit)


def fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agent_evidence_memory_fts'"
    ).fetchone()
    return bool(row)


def replace_fts_row(conn: sqlite3.Connection, memory_id: int, title: str, content: str) -> None:
    if not fts_available(conn):
        return
    conn.execute("DELETE FROM agent_evidence_memory_fts WHERE memory_id = ?", (str(memory_id),))
    conn.execute(
        "INSERT INTO agent_evidence_memory_fts (memory_id, title, content) VALUES (?, ?, ?)",
        (str(memory_id), title, content),
    )


def job_decision_memory_text(job: dict[str, Any]) -> tuple[str, str]:
    extracted = loads(job.get("extracted_json"), {})
    scoring = extracted.get("scoring") if isinstance(extracted, dict) else {}
    scoring = scoring if isinstance(scoring, dict) else {}
    company = redact_memory_text(job.get("company"), 100) or "公司待确认"
    title = redact_memory_text(job.get("title"), 160) or "岗位待确认"
    memory_title = f"岗位结论：{company} - {title}"[:280]
    matched = "、".join(redact_memory_text(item, 70) for item in list(scoring.get("matched_skills") or [])[:5])
    missing = "、".join(redact_memory_text(item, 70) for item in list(scoring.get("missing_skills") or [])[:5])
    notes = "；".join(redact_memory_text(item, 140) for item in list(scoring.get("fit_notes") or [])[:3])
    fields = [
        f"本地岗位 #{int(job['id'])}：{company} - {title}",
        f"状态：{redact_memory_text(job.get('status'), 40) or '待确认'}",
        f"匹配：{int(job.get('match_score') or 0)} 分 / {redact_memory_text(job.get('match_level'), 30) or '待确认'} / {redact_memory_text(job.get('recommendation'), 30) or '待确认'}",
        f"风险：{redact_memory_text(job.get('risk_level'), 30) or '待确认'}",
    ]
    if matched:
        fields.append(f"已有证据：{matched}")
    if missing:
        fields.append(f"待补强：{missing}")
    if notes:
        fields.append(f"评分说明：{notes}")
    skip_reason = redact_memory_text(job.get("skip_reason"), 240)
    if skip_reason:
        fields.append(f"处理原因：{skip_reason}")
    fields.append("来源：用户明确保存的本地结构化岗位结论；不包含 JD、简历、聊天或浏览器正文。")
    return memory_title, redact_memory_text("；".join(fields), MAX_MEMORY_CONTENT)


def save_job_decision_memory(job_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return {"status": "未找到", "note": "没有找到要保存结论的本地岗位。"}
        job = {key: row[key] for key in row.keys()}
        title, content = job_decision_memory_text(job)
        now = utc_now()
        existing = conn.execute(
            "SELECT id FROM agent_evidence_memories WHERE job_id = ?", (job_id,)
        ).fetchone()
        if existing:
            memory_id = int(existing["id"])
            conn.execute(
                """
                UPDATE agent_evidence_memories
                SET title = ?, content = ?, source = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, content, "user_confirmed_local_summary", now, memory_id),
            )
            action = "已更新"
        else:
            cursor = conn.execute(
                """
                INSERT INTO agent_evidence_memories (
                    job_id, memory_type, title, content, source, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, "job_decision", title, content, "user_confirmed_local_summary", now, now),
            )
            memory_id = int(cursor.lastrowid)
            action = "已保存"
        replace_fts_row(conn, memory_id, title, content)
    return {
        "status": action,
        "memory_id": memory_id,
        "job_id": job_id,
        "title": title,
        "note": "已保存本地岗位结论；不包含 JD、简历、聊天、截图或文件路径。",
    }


def serialize_memory(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
    return {
        "id": int(item["id"]),
        "job_id": int(item["job_id"]) if item.get("job_id") else None,
        "memory_type": str(item.get("memory_type") or "job_decision"),
        "title": redact_memory_text(item.get("title"), 280),
        "content": redact_memory_text(item.get("content"), MAX_MEMORY_CONTENT),
        "source": str(item.get("source") or ""),
        "updated_at": str(item.get("updated_at") or ""),
    }


def list_evidence_memories(limit: int = MAX_MEMORY_RESULTS) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_evidence_memories ORDER BY updated_at DESC, id DESC LIMIT ?",
            (max(1, min(limit, MAX_MEMORY_RESULTS)),),
        ).fetchall()
    return [serialize_memory(row) for row in rows]


def query_terms(query: str) -> list[str]:
    terms = re.findall(r"[A-Za-z0-9_+#]+|[\u4e00-\u9fff]+", str(query or ""))
    cleaned = [term[:40] for term in terms if len(term) >= 2]
    source = str(query or "")
    for anchor in MEMORY_QUERY_ANCHORS:
        if anchor.lower() in source.lower() and anchor not in cleaned:
            cleaned.append(anchor)
    return cleaned[:8]


def search_evidence_memories(query: str, limit: int = 4) -> list[dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []
    bounded_limit = max(1, min(limit, MAX_MEMORY_RESULTS))
    with connect() as conn:
        rows: list[sqlite3.Row] = []
        if fts_available(conn):
            fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
            try:
                rows = conn.execute(
                    """
                    SELECT m.*
                    FROM agent_evidence_memory_fts f
                    JOIN agent_evidence_memories m ON CAST(f.memory_id AS INTEGER) = m.id
                    WHERE agent_evidence_memory_fts MATCH ?
                    ORDER BY m.updated_at DESC, m.id DESC
                    LIMIT ?
                    """,
                    (fts_query, bounded_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            clauses = " OR ".join("title LIKE ? OR content LIKE ?" for _ in terms)
            params: list[Any] = [value for term in terms for value in (f"%{term}%", f"%{term}%")]
            rows = conn.execute(
                f"SELECT * FROM agent_evidence_memories WHERE {clauses} ORDER BY updated_at DESC, id DESC LIMIT ?",
                (*params, bounded_limit),
            ).fetchall()
    return [serialize_memory(row) for row in rows]


def delete_evidence_memory(memory_id: int) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT id FROM agent_evidence_memories WHERE id = ?", (memory_id,)).fetchone()
        if not row:
            return False
        if fts_available(conn):
            conn.execute("DELETE FROM agent_evidence_memory_fts WHERE memory_id = ?", (str(memory_id),))
        deleted = conn.execute("DELETE FROM agent_evidence_memories WHERE id = ?", (memory_id,))
    return bool(deleted.rowcount)


def explicit_job_memory_request(text: str) -> bool:
    normalized = str(text or "")
    if re.search(r"(?:不想|不要|不需要|不用|不必|无需|暂不|别)[^，。；;]{0,8}(?:记住|保存|沉淀|留档|加入记忆)", normalized):
        return False
    wants_save = any(token in normalized for token in ("记住", "保存", "沉淀", "留档", "加入记忆"))
    job_scope = any(token in normalized for token in ("岗位", "当前", "这个", "该", "结论", "分析", "原因"))
    return wants_save and job_scope
