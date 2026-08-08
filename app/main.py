from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .config import ROOT_DIR, TASK_TYPES, looks_masked, mask_secret, recordings_dir, set_env_value, suggest_api_key_env
from .db import connect, dumps, get_setting, init_db, loads, set_setting, utc_now
from .services.analyzer import (
    build_interview_review,
    clean_extracted,
    extract_salary,
    generate_message,
    looks_like_salary_text,
    rule_extract_jd,
    score_job,
)
from .services.browser_patrol import capture_browser_patrol_observations, open_message_patrol_browser
from .services.application_browser import build_application_browser_plan, probe_application_browser_plan
from .services.communication_browser import build_browser_send_adapter_plan, probe_browser_send_adapter_plan
from .services.conversation import classify_conversation, prepare_conversation_text
from .services.github_projects import (
    GitHubProjectError,
    dedupe_urls,
    fetch_github_repo_snapshot,
    github_repo_urls_from_projects,
    merge_project_facts,
    normalize_repo_url,
    project_from_snapshot,
    repo_key,
)
from .services.job_fetcher import ensure_public_http_url, fetch_job_from_url, normalize_visible_text
from .services.job_searcher import (
    SearchResult,
    capture_current_search_page,
    extract_candidates_from_anchors,
    open_manual_search_in_edge,
    search_jobs_with_browser,
)
from .services.llm import OpenAICompatibleClient, client_for_task
from .services.research import search_company
from .services.resume import read_resume_text
from .services.transcription import ALLOWED_RECORDING_EXTENSIONS, TRANSCRIPTION_MODELS, transcribe_recording


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler_task = asyncio.create_task(message_patrol_scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="简历投递 Agent", lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")

BULK_IMPORT_LIMIT = 20
EXTENSION_TEXT_LIMIT = 20000
EXTENSION_LINK_LIMIT = 300
LAST_MANUAL_SEARCH_KEY = "last_manual_search"
COMMUNICATION_POLICY_KEY = "communication_policy"
AUTOMATION_CONTROL_KEY = "automation_control"
MESSAGE_PATROL_POLICY_KEY = "message_patrol_policy"
MESSAGE_PATROL_FINGERPRINTS_KEY = "message_patrol_fingerprints"
IGNORED_MESSAGE_FINGERPRINTS_KEY = "ignored_message_fingerprints"
PATROL_SCHEDULER_POLL_SECONDS = 10
MESSAGE_PATROL_OBSERVATION_LIMIT = 20
COMMUNICATION_EXECUTOR_PLAN_LIMIT = 20
INTERVIEW_PREP_TRIGGER_STATUSES = {"待面试", "面试准备中"}
INTERVIEW_FEEDBACK_STATUSES = ["待练习", "已补强", "已归档"]
APPLICATION_PREPARATION_STATUSES = ["待确认", "已确认", "已跳过"]
COMMUNICATION_MODES = [
    ("off", "关闭"),
    ("draft", "草稿模式"),
    ("autonomous", "自主询问模式"),
]
BATCH_SEPARATOR_RE = re.compile(r"(?m)^\s*(?:-{3,}|={3,}|#{3,}|岗位\s*\d+[:：]?)\s*$")
BATCH_START_MARKERS = [
    re.compile(r"(?m)^\s*公司名称\s*[:：]"),
    re.compile(r"(?m)^\s*(?:岗位名称|职位名称)\s*[:：]"),
]


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def redirect_with_notice(path: str, message: str, notice_type: str = "info") -> RedirectResponse:
    query = urlencode({"notice": message, "notice_type": notice_type})
    separator = "&" if "?" in path else "?"
    return redirect(f"{path}{separator}{query}")


def task_label(task_type: str) -> str:
    return dict(TASK_TYPES).get(task_type, task_type)


def communication_mode_label(value: str) -> str:
    return dict(COMMUNICATION_MODES).get(value or "", "草稿模式")


def action_type_label(value: str) -> str:
    return {
        "conversation_capture": "对话采集",
        "draft_status_update": "草稿处理",
        "conversation_feedback": "分类反馈",
        "communication_policy_update": "沟通设置",
        "automation_control_update": "自动化控制",
        "automation_paused": "暂停跳过",
        "conversation_diff_check": "对话差分",
        "message_patrol_run": "消息巡检",
        "message_patrol_observation": "巡检观察",
        "message_patrol_policy_update": "巡检设置",
        "message_patrol_ignore": "忽略消息",
        "draft_send_gate": "发送闸门",
        "communication_executor_dry_run": "自动回复演练",
        "communication_browser_dry_run": "浏览器发送演练",
        "communication_browser_probe": "浏览器页面探测",
        "demo_draft_created": "演练草稿",
        "interview_prep_auto_create": "面试准备",
        "interview_feedback_update": "面试反馈",
        "interview_practice": "模拟面试",
        "interview_recording": "面试录音",
        "application_preparation": "投递准备",
        "application_browser_open": "投递页面打开",
        "application_browser_probe": "投递页面演练",
        "github_project_refresh": "GitHub 项目刷新",
    }.get(value or "", value or "-")


def communication_policy() -> dict[str, Any]:
    saved = get_setting(COMMUNICATION_POLICY_KEY, {}) or {}
    mode = str(saved.get("mode") or "draft")
    if mode not in dict(COMMUNICATION_MODES):
        mode = "draft"
    try:
        max_followups = int(saved.get("max_auto_followups", 2))
    except (TypeError, ValueError):
        max_followups = 2
    return {
        "mode": mode,
        "mode_label": communication_mode_label(mode),
        "max_auto_followups": max(0, min(max_followups, 10)),
    }


def automation_control() -> dict[str, Any]:
    saved = get_setting(AUTOMATION_CONTROL_KEY, {}) or {}
    paused = bool(saved.get("paused"))
    return {
        "paused": paused,
        "status_label": "已暂停" if paused else "运行中",
        "pause_reason": str(saved.get("pause_reason") or ""),
        "updated_at": str(saved.get("updated_at") or ""),
    }


def parse_utc_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def message_patrol_policy() -> dict[str, Any]:
    saved = get_setting(MESSAGE_PATROL_POLICY_KEY, {}) or {}
    enabled = bool(saved.get("enabled"))
    interval_seconds = clamp_int(saved.get("interval_seconds"), 300, 30, 3600)
    cooldown_seconds = clamp_int(saved.get("cooldown_seconds"), 120, 0, 3600)
    last_tick_at = str(saved.get("last_tick_at") or "")
    next_tick_at = str(saved.get("next_tick_at") or "")
    last_status = str(saved.get("last_status") or "")
    return {
        "enabled": enabled,
        "status_label": "已开启" if enabled else "已关闭",
        "interval_seconds": interval_seconds,
        "cooldown_seconds": cooldown_seconds,
        "last_tick_at": last_tick_at,
        "next_tick_at": next_tick_at,
        "last_status": last_status,
        "updated_at": str(saved.get("updated_at") or ""),
    }


def save_message_patrol_policy(policy: dict[str, Any]) -> None:
    set_setting(
        MESSAGE_PATROL_POLICY_KEY,
        {
            "enabled": bool(policy.get("enabled")),
            "interval_seconds": clamp_int(policy.get("interval_seconds"), 300, 30, 3600),
            "cooldown_seconds": clamp_int(policy.get("cooldown_seconds"), 120, 0, 3600),
            "last_tick_at": str(policy.get("last_tick_at") or ""),
            "next_tick_at": str(policy.get("next_tick_at") or ""),
            "last_status": str(policy.get("last_status") or ""),
            "updated_at": str(policy.get("updated_at") or utc_now()),
        },
    )


def log_agent_action(
    conn: Any,
    *,
    action_type: str,
    status: str,
    summary: str = "",
    platform: str = "",
    job_id: int | None = None,
    capture_id: int | None = None,
    draft_id: int | None = None,
    decision: dict[str, Any] | None = None,
    error_message: str = "",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO agent_action_logs (
            job_id, capture_id, draft_id, platform, action_type, status,
            summary, decision_json, error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            capture_id,
            draft_id,
            platform,
            action_type,
            status,
            summary[:500],
            dumps(decision or {}),
            error_message[:500],
            utc_now(),
        ),
    )
    return int(cursor.lastrowid)


def insert_message_patrol_run(
    conn: Any,
    *,
    trigger_type: str,
    platform: str = "",
    scope: str = "single_conversation",
    status: str,
    checked_count: int = 0,
    new_count: int = 0,
    skipped_count: int = 0,
    error_count: int = 0,
    note: str = "",
    source_url: str = "",
    page_title: str = "",
    fingerprint_key: str = "",
    fingerprint: str = "",
    job_id: int | None = None,
    capture_id: int | None = None,
) -> int:
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO message_patrol_runs (
            job_id, capture_id, platform, source_url, page_title, trigger_type,
            scope, status, checked_count, new_count, skipped_count, error_count,
            note, fingerprint_key, fingerprint, created_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            capture_id,
            platform,
            source_url,
            page_title,
            trigger_type,
            scope,
            status,
            checked_count,
            new_count,
            skipped_count,
            error_count,
            note[:500],
            fingerprint_key,
            fingerprint,
            now,
            now,
        ),
    )
    return int(cursor.lastrowid)


def record_message_patrol_executor_skip(
    *,
    status: str,
    note: str,
    trigger_type: str,
    scope: str = "scheduled_patrol",
    skipped_count: int = 1,
    error_count: int = 0,
    decision: dict[str, Any] | None = None,
) -> int:
    with connect() as conn:
        patrol_run_id = insert_message_patrol_run(
            conn,
            trigger_type=trigger_type,
            scope=scope,
            status=status,
            skipped_count=skipped_count,
            error_count=error_count,
            note=note,
        )
        log_agent_action(
            conn,
            action_type="message_patrol_run",
            status=status,
            summary=note,
            decision={
                "patrol_run_id": patrol_run_id,
                "trigger_type": trigger_type,
                "scope": scope,
                "browser_executor": "edge_cdp",
                "model_called": False,
                **(decision or {}),
            },
        )
    return patrol_run_id


def run_browser_message_patrol_executor(
    *,
    trigger_type: str,
    scope: str = "scheduled_patrol",
    dry_run: bool = True,
) -> dict[str, Any]:
    try:
        observations = capture_browser_patrol_observations(limit=MESSAGE_PATROL_OBSERVATION_LIMIT)
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        disconnected = "没有检测到" in message or "无法连接" in message or "9222" in message
        status = "浏览器未连接" if disconnected else "巡检失败"
        patrol_run_id = record_message_patrol_executor_skip(
            status=status,
            note=message[:500],
            trigger_type=trigger_type,
            scope=scope,
            skipped_count=1 if disconnected else 0,
            error_count=0 if disconnected else 1,
            decision={"dry_run": dry_run},
        )
        return {
            "status": status,
            "note": message,
            "patrol_run_id": patrol_run_id,
            "checked_count": 0,
            "new_count": 0,
            "skipped_count": 1 if disconnected else 0,
            "error_count": 0 if disconnected else 1,
            "skipped": True,
        }

    if not observations:
        note = "已连接 Edge，但没有发现已打开的招聘平台聊天页。"
        patrol_run_id = record_message_patrol_executor_skip(
            status="未发现聊天页",
            note=note,
            trigger_type=trigger_type,
            scope=scope,
            skipped_count=1,
            decision={"dry_run": dry_run},
        )
        return {
            "status": "未发现聊天页",
            "note": note,
            "patrol_run_id": patrol_run_id,
            "checked_count": 0,
            "new_count": 0,
            "skipped_count": 1,
            "error_count": 0,
            "skipped": True,
        }

    results = [
        process_message_patrol_observation(
            observation,
            index=index,
            dry_run=dry_run,
            executor="edge_cdp",
            trigger_type=trigger_type,
            scope=scope,
        )
        for index, observation in enumerate(observations)
    ]
    counts = summarize_observation_results(results)
    if counts["error_count"]:
        status = "部分失败" if counts["checked_count"] else "巡检失败"
    elif counts["new_count"]:
        status = "观察完成" if dry_run else "已处理"
    elif counts["skipped_count"]:
        statuses = {str(item.get("status") or "") for item in results}
        status = "无新内容" if statuses == {"无新内容"} else "已跳过"
    else:
        status = "已完成"
    note = "Edge 巡检完成：检查 {checked_count} 条，新内容 {new_count} 条，跳过 {skipped_count} 条，错误 {error_count} 条。".format(**counts)
    patrol_ids = [int(item["patrol_run_id"]) for item in results if item.get("patrol_run_id")]
    return {
        "status": status,
        "note": note,
        "patrol_run_id": patrol_ids[-1] if patrol_ids else None,
        "patrol_run_ids": patrol_ids,
        "checked_count": counts["checked_count"],
        "new_count": counts["new_count"],
        "skipped_count": counts["skipped_count"],
        "error_count": counts["error_count"],
        "skipped": counts["new_count"] == 0,
        "results": results,
    }


def run_message_patrol_tick(trigger_type: str = "manual", force: bool = False) -> dict[str, Any] | None:
    policy = message_patrol_policy()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")

    if not policy["enabled"] and trigger_type == "scheduler":
        return None

    next_tick = parse_utc_datetime(policy.get("next_tick_at"))
    if trigger_type == "scheduler" and not force and policy["enabled"] and next_tick and now_dt < next_tick:
        return None

    last_tick = parse_utc_datetime(policy.get("last_tick_at"))
    cooldown_seconds = int(policy.get("cooldown_seconds") or 0)
    if (
        trigger_type == "manual"
        and not force
        and policy["enabled"]
        and cooldown_seconds
        and last_tick
        and now_dt < last_tick + timedelta(seconds=cooldown_seconds)
    ):
        remaining = int((last_tick + timedelta(seconds=cooldown_seconds) - now_dt).total_seconds())
        status = "冷却中"
        note = f"距离上次巡检不足 cooldown，还需约 {max(1, remaining)} 秒。"
        with connect() as conn:
            patrol_run_id = insert_message_patrol_run(
                conn,
                trigger_type=trigger_type,
                scope="scheduled_patrol",
                status=status,
                skipped_count=1,
                note=note,
            )
            log_agent_action(
                conn,
                action_type="message_patrol_run",
                status=status,
                summary=note,
                decision={
                    "patrol_run_id": patrol_run_id,
                    "trigger_type": trigger_type,
                    "cooldown_seconds": cooldown_seconds,
                    "last_tick_at": policy["last_tick_at"],
                },
            )
        return {"status": status, "note": note, "patrol_run_id": patrol_run_id, "skipped": True}

    if not policy["enabled"]:
        status = "未启用"
        note = "定时巡检未开启，未读取浏览器页面。"
        checked_count = 0
        skipped_count = 1
    elif automation_control()["paused"]:
        status = "已暂停"
        note = "自动化已暂停，本次调度 tick 未读取浏览器页面。"
        checked_count = 0
        skipped_count = 1
    elif communication_policy()["mode"] == "off":
        status = "沟通关闭"
        note = "沟通模式为关闭，本次调度 tick 未读取浏览器页面。"
        checked_count = 0
        skipped_count = 1
    else:
        executor_trigger = "scheduled_executor" if trigger_type == "scheduler" else "manual_browser"
        result = run_browser_message_patrol_executor(trigger_type=executor_trigger, dry_run=True)
        next_tick_at = ""
        if policy["enabled"]:
            next_tick_at = (now_dt + timedelta(seconds=int(policy["interval_seconds"]))).isoformat(timespec="seconds")
        policy.update(
            {
                "last_tick_at": now,
                "next_tick_at": next_tick_at,
                "last_status": result["status"],
                "updated_at": now,
            }
        )
        save_message_patrol_policy(policy)
        return result

    with connect() as conn:
        patrol_run_id = insert_message_patrol_run(
            conn,
            trigger_type=trigger_type,
            scope="scheduled_patrol",
            status=status,
            checked_count=checked_count,
            skipped_count=skipped_count,
            note=note,
        )
        log_agent_action(
            conn,
            action_type="message_patrol_run",
            status=status,
            summary=note,
            decision={
                "patrol_run_id": patrol_run_id,
                "trigger_type": trigger_type,
                "interval_seconds": policy["interval_seconds"],
                "cooldown_seconds": cooldown_seconds,
                "browser_executor": "not_connected",
                "model_called": False,
            },
        )

    next_tick_at = ""
    if policy["enabled"]:
        next_tick_at = (now_dt + timedelta(seconds=int(policy["interval_seconds"]))).isoformat(timespec="seconds")
    policy.update(
        {
            "last_tick_at": now,
            "next_tick_at": next_tick_at,
            "last_status": status,
            "updated_at": now,
        }
    )
    save_message_patrol_policy(policy)
    return {"status": status, "note": note, "patrol_run_id": patrol_run_id, "skipped": skipped_count > 0}


async def message_patrol_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(PATROL_SCHEDULER_POLL_SECONDS)
        try:
            await asyncio.to_thread(run_message_patrol_tick, "scheduler", False)
        except Exception:
            continue


def parse_json_fields(job: dict[str, Any]) -> dict[str, Any]:
    job = dict(job)
    job["extracted"] = loads(job.get("extracted_json"), {})
    return job


def analysis_source_label(value: str) -> str:
    return {
        "llm_plus_rules": "LLM + 本地规则",
        "local_rules": "本地规则",
        "failed": "待分析",
    }.get(value or "", value or "本地规则")


def auto_search_depth(scoring: dict[str, Any], extracted: dict[str, Any]) -> str:
    if scoring.get("risk_signals"):
        return "quick"
    if scoring.get("level") == "高匹配" and not extracted.get("company"):
        return "deep"
    if scoring.get("level") == "高匹配" or scoring.get("caution_signals"):
        return "deep"
    if scoring.get("level") == "中匹配":
        return "standard"
    return "quick"


def get_resume_text(resume_id: int | None) -> tuple[dict[str, Any] | None, str]:
    if not resume_id:
        return None, ""
    with connect() as conn:
        resume = conn.execute("SELECT * FROM resume_versions WHERE id = ?", (resume_id,)).fetchone()
        if not resume:
            return None, ""
        resume_dict = {key: resume[key] for key in resume.keys()}
        parsed = resume_dict.get("parsed_text") or ""
        file_path = resume_dict.get("file_path") or ""
        if not parsed and file_path:
            parsed = read_resume_text(file_path)
            if parsed:
                conn.execute(
                    """
                    UPDATE resume_versions
                    SET parsed_text = ?, file_type = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (parsed, Path(file_path).suffix.lower().lstrip("."), utc_now(), resume_id),
                )
                resume_dict["parsed_text"] = parsed
        return resume_dict, parsed


def try_llm_jd_extract(jd_text: str) -> tuple[dict[str, Any] | None, str]:
    client = client_for_task("jd_extract")
    if not client or not client.configured:
        return None, ""
    prompt = (
        "你是求职投递 Agent 的 JD 结构化抽取器。请只输出 JSON，不要输出 Markdown。"
        "字段：title, company, city, salary_text, internship_days, internship_duration, "
        "responsibilities(array), requirements(array), required_skills(array), bonus_skills(array), "
        "risk_signals(array), caution_signals(array)。如果不知道，填空字符串或空数组。"
    )
    try:
        return client.complete_json(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": jd_text[:12000]},
            ]
        ), ""
    except Exception as exc:
        client.log_error(str(exc))
        return None, str(exc)


def try_llm_message(extracted: dict[str, Any], scoring: dict[str, Any], fallback: dict[str, str]) -> dict[str, str]:
    client = client_for_task("message_draft")
    if not client or not client.configured:
        return fallback
    prompt = (
        "请基于岗位信息和候选人优势生成投递话术，风格礼貌正式但有学生真诚感。"
        "不要编造经历。输出 JSON：message, email。"
    )
    try:
        result = client.complete_json(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": dumps({"job": extracted, "scoring": scoring})},
            ]
        )
        return {
            "message": result.get("message") or fallback["message"],
            "email": result.get("email") or fallback["email"],
        }
    except Exception as exc:
        client.log_error(str(exc))
        return fallback


def apply_blacklists(extracted: dict[str, Any], jd_text: str) -> None:
    keywords = get_setting("blacklist_keywords", []) or []
    companies = get_setting("blacklist_companies", []) or []
    risk_signals = list(extracted.get("risk_signals") or [])
    for keyword in keywords:
        keyword = str(keyword).strip()
        if keyword and keyword in jd_text and keyword not in risk_signals:
            risk_signals.append(keyword)
    company = str(extracted.get("company") or "").strip()
    for blacklisted in companies:
        blacklisted = str(blacklisted).strip()
        if blacklisted and company and (blacklisted in company or company in blacklisted):
            signal = f"黑名单公司：{blacklisted}"
            if signal not in risk_signals:
                risk_signals.append(signal)
    extracted["risk_signals"] = risk_signals


def analyze_job_payload(
    jd_text: str,
    resume_id: int | None,
    title: str = "",
    company: str = "",
    city: str = "",
    salary_text: str = "",
    search_depth: str = "auto",
    generate_messages: bool = True,
) -> dict[str, Any]:
    _resume, resume_text = get_resume_text(resume_id)
    fallback_extract = rule_extract_jd(
        jd_text,
        fallback_title=title,
        fallback_company=company,
        fallback_city=city,
        fallback_salary=salary_text,
    )
    llm_extract, analysis_error = try_llm_jd_extract(jd_text)
    extracted = clean_extracted({**fallback_extract, **(llm_extract or {})})
    extracted["required_skills"] = list(dict.fromkeys((llm_extract or {}).get("required_skills") or fallback_extract["required_skills"]))
    extracted["risk_signals"] = list(dict.fromkeys((llm_extract or {}).get("risk_signals") or fallback_extract["risk_signals"]))
    extracted["caution_signals"] = list(dict.fromkeys((llm_extract or {}).get("caution_signals") or fallback_extract["caution_signals"]))
    if fallback_extract.get("salary_text"):
        extracted["salary_text"] = fallback_extract["salary_text"]
    elif extracted.get("salary_text") and not looks_like_salary_text(extracted["salary_text"]):
        extracted["salary_text"] = ""
    apply_blacklists(extracted, jd_text)

    scoring = score_job(extracted, jd_text, resume_text)
    messages = try_llm_message(extracted, scoring, generate_message(extracted, scoring)) if generate_messages and jd_text else {"message": "", "email": ""}
    final_depth = auto_search_depth(scoring, extracted) if search_depth == "auto" else search_depth
    source = "llm_plus_rules" if llm_extract else "local_rules"
    if not jd_text:
        source = "failed"
        analysis_error = "JD 文本为空"

    return {
        "extracted": extracted,
        "scoring": scoring,
        "messages": messages,
        "search_depth": final_depth,
        "analysis_error": analysis_error,
        "analysis_source": source,
    }


def insert_company_research(conn: Any, job_id: int, company: str, title: str, city: str, search_depth: str) -> None:
    if not company:
        return
    now = utc_now()
    for result in search_company(company, title, city, search_depth):
        conn.execute(
            """
            INSERT INTO company_research (
                job_id, company, query, source_title, source_url,
                summary, risk_signals_json, searched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                company,
                " ".join([company, title, city]).strip(),
                result.title,
                result.url,
                result.summary,
                dumps([]),
                now,
            ),
        )


def split_batch_jds(raw_text: str) -> list[str]:
    text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    explicit_parts = [part.strip() for part in BATCH_SEPARATOR_RE.split(text) if part.strip()]
    if len(explicit_parts) > 1:
        return explicit_parts

    for marker in BATCH_START_MARKERS:
        starts = [match.start() for match in marker.finditer(text)]
        if len(starts) > 1:
            boundaries = [0] + starts[1:] + [len(text)]
            return [text[boundaries[index] : boundaries[index + 1]].strip() for index in range(len(boundaries) - 1)]

    return [text]


def infer_platform_from_url(url: str) -> str:
    lowered = url.lower()
    if "zhipin.com" in lowered:
        return "Boss 直聘"
    if "liepin.com" in lowered:
        return "猎聘"
    if "shixiseng.com" in lowered:
        return "实习僧"
    if "zhaopin.com" in lowered:
        return "智联招聘"
    if "51job.com" in lowered or "we.51job.com" in lowered:
        return "前程无忧"
    return "岗位链接"


def fetch_mode_label(value: str) -> str:
    return {
        "auto": "自动",
        "http": "普通网页",
        "browser": "浏览器渲染",
    }.get(value or "", value or "自动")


def browser_channel_label(value: str) -> str:
    return {
        "msedge": "Microsoft Edge",
        "edge": "Microsoft Edge",
        "chromium": "Chromium",
        "extension": "浏览器扩展",
    }.get(value or "", value or "Microsoft Edge")


def search_form_value(form: Any, field: str, fallback: str = "") -> str:
    raw = form.get(field)
    if raw is None:
        return fallback
    return str(raw).strip()


def default_search_form() -> dict[str, str]:
    saved = get_setting(LAST_MANUAL_SEARCH_KEY, {}) or {}
    return {
        "platform": str(saved.get("platform") or "Boss 直聘"),
        "keyword": str(saved.get("keyword") or ""),
        "city": str(saved.get("city") or ""),
        "browser_channel": str(saved.get("browser_channel") or "msedge"),
        "search_url": str(saved.get("search_url") or ""),
    }


def save_last_manual_search(
    platform: str,
    keyword: str,
    city: str,
    browser_channel: str = "msedge",
    search_url: str = "",
) -> None:
    set_setting(
        LAST_MANUAL_SEARCH_KEY,
        {
            "platform": platform,
            "keyword": keyword,
            "city": city,
            "browser_channel": browser_channel or "msedge",
            "search_url": search_url,
        },
    )


def initial_job_status(jd_text: str, scoring: dict[str, Any]) -> str:
    if not jd_text:
        return "待分析"
    if scoring["recommendation"] in {"必投", "可冲"}:
        return "待确认"
    return "已归档"


def create_job_record(
    conn: Any,
    *,
    jd_text: str,
    resume_id: int | None,
    platform: str = "",
    source_url: str = "",
    title: str = "",
    company: str = "",
    city: str = "",
    salary_text: str = "",
    search_depth: str = "auto",
    generate_messages: bool = True,
) -> tuple[int, dict[str, Any]]:
    analysis = analyze_job_payload(
        jd_text,
        resume_id,
        title=title,
        company=company,
        city=city,
        salary_text=salary_text,
        search_depth=search_depth,
        generate_messages=generate_messages,
    )
    extracted = analysis["extracted"]
    scoring = analysis["scoring"]
    messages = analysis["messages"]
    final_depth = analysis["search_depth"]
    now = utc_now()
    status = initial_job_status(jd_text, scoring)
    cursor = conn.execute(
        """
        INSERT INTO job_postings (
            platform, source_url, title, company, city, salary_text,
            internship_days, internship_duration, jd_text, extracted_json,
            selected_resume_id, match_score, match_level, risk_level,
            recommendation, status, skip_reason, generated_message,
            generated_email, analysis_error, analysis_source, search_depth, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            platform,
            source_url,
            extracted.get("title") or "",
            extracted.get("company") or company,
            extracted.get("city") or "",
            extracted.get("salary_text") or "",
            extracted.get("internship_days") or "",
            extracted.get("internship_duration") or "",
            jd_text,
            dumps({"extracted": extracted, "scoring": scoring}),
            resume_id,
            scoring["score"] if jd_text else 0,
            scoring["level"] if jd_text else "",
            scoring["risk_level"] if jd_text else "",
            scoring["recommendation"] if jd_text else "",
            status,
            scoring["skip_reason"] if jd_text else "",
            messages["message"] if jd_text else "",
            messages["email"] if jd_text else "",
            analysis["analysis_error"],
            analysis["analysis_source"],
            final_depth,
            now,
            now,
        ),
    )
    job_id = cursor.lastrowid
    company_name = extracted.get("company") or company
    if jd_text and company_name:
        insert_company_research(conn, job_id, company_name, extracted.get("title") or "", extracted.get("city") or "", final_depth)
    return job_id, analysis


def comparable_source_url(url: str) -> set[str]:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return set()
    path = parsed.path.rstrip("/") or parsed.path or "/"
    exact = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/") or parsed.path, "", parsed.query, ""))
    without_query = urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))
    return {exact, without_query}


def same_source_url(left: str, right: str) -> bool:
    return bool(comparable_source_url(left) & comparable_source_url(right))


def find_existing_job_by_source_url(conn: Any, source_url: str) -> Any | None:
    for row in conn.execute("SELECT * FROM job_postings WHERE source_url != '' ORDER BY id DESC LIMIT 500").fetchall():
        if same_source_url(row["source_url"], source_url):
            return row
    return None


def link_candidates_to_job(conn: Any, source_url: str, job_id: int) -> int:
    now = utc_now()
    matched_ids = [
        int(row["id"])
        for row in conn.execute("SELECT id, source_url FROM job_candidates WHERE source_url != '' ORDER BY id DESC LIMIT 1000").fetchall()
        if same_source_url(row["source_url"], source_url)
    ]
    if not matched_ids:
        return 0
    placeholders = ",".join("?" for _ in matched_ids)
    conn.execute(
        f"UPDATE job_candidates SET job_id = ?, status = ?, error_message = '', updated_at = ? WHERE id IN ({placeholders})",
        (job_id, "已导入", now, *matched_ids),
    )
    return len(matched_ids)


def refresh_job_record(
    conn: Any,
    job_id: int,
    *,
    jd_text: str,
    resume_id: int | None,
    platform: str = "",
    source_url: str = "",
    title: str = "",
    company: str = "",
    city: str = "",
    salary_text: str = "",
    search_depth: str = "auto",
    generate_messages: bool = True,
) -> dict[str, Any]:
    existing = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not existing:
        raise ValueError("没有找到要刷新的岗位。")
    analysis = analyze_job_payload(
        jd_text,
        resume_id,
        title=title,
        company=company,
        city=city,
        salary_text=salary_text,
        search_depth=search_depth,
        generate_messages=generate_messages,
    )
    extracted = analysis["extracted"]
    scoring = analysis["scoring"]
    messages = analysis["messages"]
    status = existing["status"] or "待确认"
    if status in {"待分析", "待确认", "已归档"}:
        status = initial_job_status(jd_text, scoring)

    now = utc_now()
    conn.execute("DELETE FROM company_research WHERE job_id = ?", (job_id,))
    conn.execute(
        """
        UPDATE job_postings
        SET platform = ?, source_url = ?, title = ?, company = ?, city = ?, salary_text = ?,
            internship_days = ?, internship_duration = ?, jd_text = ?, extracted_json = ?,
            selected_resume_id = ?, match_score = ?, match_level = ?, risk_level = ?,
            recommendation = ?, status = ?, skip_reason = ?, generated_message = ?,
            generated_email = ?, analysis_error = ?, analysis_source = ?, search_depth = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            platform or existing["platform"],
            source_url or existing["source_url"],
            extracted.get("title") or "",
            extracted.get("company") or company,
            extracted.get("city") or "",
            extracted.get("salary_text") or "",
            extracted.get("internship_days") or "",
            extracted.get("internship_duration") or "",
            jd_text,
            dumps({"extracted": extracted, "scoring": scoring}),
            resume_id or existing["selected_resume_id"],
            scoring["score"] if jd_text else 0,
            scoring["level"] if jd_text else "",
            scoring["risk_level"] if jd_text else "",
            scoring["recommendation"] if jd_text else "",
            status,
            scoring["skip_reason"] if jd_text else "",
            messages["message"] if jd_text else "",
            messages["email"] if jd_text else "",
            analysis["analysis_error"],
            analysis["analysis_source"],
            analysis["search_depth"],
            now,
            job_id,
        ),
    )
    company_name = extracted.get("company") or company
    if jd_text and company_name:
        insert_company_research(conn, job_id, company_name, extracted.get("title") or "", extracted.get("city") or "", analysis["search_depth"])
    return analysis


def token_stats() -> dict[str, Any]:
    with connect() as conn:
        today = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total, COALESCE(SUM(estimated_cost), 0) AS cost
            FROM model_call_logs
            WHERE date(created_at) = date('now')
            """
        ).fetchone()
        all_time = conn.execute(
            """
            SELECT COALESCE(SUM(total_tokens), 0) AS total, COALESCE(SUM(estimated_cost), 0) AS cost
            FROM model_call_logs
            """
        ).fetchone()
    return {
        "today_total": int(today["total"] or 0),
        "today_cost": float(today["cost"] or 0),
        "all_total": int(all_time["total"] or 0),
        "all_cost": float(all_time["cost"] or 0),
    }


def save_search_result(result: Any) -> int:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO job_search_runs (
                platform, keyword, city, search_url, browser_channel, status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.platform,
                result.keyword,
                result.city,
                result.search_url,
                result.browser_channel,
                "完成" if result.candidates else "无结果",
                result.note,
                now,
            ),
        )
        run_id = cursor.lastrowid
        for candidate in result.candidates:
            conn.execute(
                """
                INSERT INTO job_candidates (
                    search_run_id, platform, title, company, city, source_url,
                    summary, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.platform,
                    candidate.title,
                    candidate.company,
                    candidate.city,
                    candidate.source_url,
                    candidate.summary,
                    "候选",
                    now,
                    now,
                ),
            )
    return int(run_id)


def save_search_failure(platform: str, keyword: str, city: str, browser_channel: str, error_message: str) -> int:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO job_search_runs (
                platform, keyword, city, search_url, browser_channel, status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                keyword,
                city,
                "",
                browser_channel or "msedge",
                "失败",
                error_message[:500],
                now,
            ),
        )
    return int(cursor.lastrowid)


def api_error(message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


def payload_text(payload: dict[str, Any], key: str, limit: int = 500) -> str:
    return str(payload.get(key) or "").strip()[:limit]


def payload_flag(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "y"}:
        return True
    if normalized in {"0", "false", "no", "off", "n"}:
        return False
    return default


def conversation_text_fingerprint(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def conversation_fingerprint_key(
    *,
    job_id: int | None,
    source_url: str,
    platform: str,
    page_title: str,
) -> str:
    if job_id:
        return f"job:{job_id}"
    return "page:{}|{}|{}".format(platform.strip().lower(), stable_conversation_url(source_url), page_title.strip().lower())


def stable_conversation_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return (url or "").strip()
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or parsed.path or "/"
    query = parse_qs(parsed.query, keep_blank_values=False)
    volatile_keys = {"time", "timestamp", "ts", "t", "_"}
    stable_query = {
        key: values
        for key, values in query.items()
        if key.lower() not in volatile_keys and not key.lower().startswith("_")
    }
    if host.endswith("liepin.com") and path == "/":
        stable_query = {}
    return urlunparse((parsed.scheme.lower(), host, path, "", urlencode(stable_query, doseq=True), ""))


def load_message_patrol_fingerprints() -> dict[str, Any]:
    saved = get_setting(MESSAGE_PATROL_FINGERPRINTS_KEY, {}) or {}
    return saved if isinstance(saved, dict) else {}


def save_message_patrol_fingerprint(key: str, fingerprint: str) -> None:
    fingerprints = load_message_patrol_fingerprints()
    fingerprints[key] = {"fingerprint": fingerprint, "updated_at": utc_now()}
    if len(fingerprints) > 500:
        ordered = sorted(
            fingerprints.items(),
            key=lambda item: str(item[1].get("updated_at") if isinstance(item[1], dict) else ""),
        )
        fingerprints = dict(ordered[-500:])
    set_setting(MESSAGE_PATROL_FINGERPRINTS_KEY, fingerprints)


def has_message_patrol_fingerprint(key: str, fingerprint: str) -> bool:
    item = load_message_patrol_fingerprints().get(key)
    if isinstance(item, dict):
        return item.get("fingerprint") == fingerprint
    return item == fingerprint


def load_ignored_message_fingerprints() -> dict[str, Any]:
    saved = get_setting(IGNORED_MESSAGE_FINGERPRINTS_KEY, {}) or {}
    return saved if isinstance(saved, dict) else {}


def ignored_fingerprint_items(item: Any) -> list[dict[str, Any]]:
    if isinstance(item, list):
        return [entry for entry in item if isinstance(entry, dict)]
    if isinstance(item, dict):
        return [item]
    if isinstance(item, str):
        return [{"fingerprint": item, "reason": "", "updated_at": ""}]
    return []


def save_ignored_message_fingerprint(key: str, fingerprint: str, reason: str = "") -> None:
    ignored = load_ignored_message_fingerprints()
    entries = [
        entry
        for entry in ignored_fingerprint_items(ignored.get(key))
        if str(entry.get("fingerprint") or "") != fingerprint
    ]
    entries.append({
        "fingerprint": fingerprint,
        "reason": reason[:200],
        "updated_at": utc_now(),
    })
    ignored[key] = entries[-20:]
    if len(ignored) > 500:
        ordered = sorted(
            ignored.items(),
            key=lambda item: str(
                max(
                    (str(entry.get("updated_at") or "") for entry in ignored_fingerprint_items(item[1])),
                    default="",
                )
            ),
        )
        ignored = dict(ordered[-500:])
    set_setting(IGNORED_MESSAGE_FINGERPRINTS_KEY, ignored)


def ignored_message_fingerprint(key: str, fingerprint: str) -> dict[str, Any] | None:
    for item in ignored_fingerprint_items(load_ignored_message_fingerprints().get(key)):
        if item.get("fingerprint") == fingerprint:
            return item
    return None


def should_skip_observation_for_pause(*, dry_run: bool, trigger_type: str) -> bool:
    return not (dry_run and trigger_type == "manual_browser")


def extension_page_text(payload: dict[str, Any]) -> str:
    title = payload_text(payload, "title", 300)
    url = payload_text(payload, "url", 1000)
    text = normalize_visible_text(str(payload.get("text") or ""))
    parts = [f"页面标题：{title}" if title else "", f"页面链接：{url}" if url else "", text]
    return "\n\n".join(part for part in parts if part).strip()[:EXTENSION_TEXT_LIMIT]


def extension_platform(url: str, fallback: str = "") -> str:
    inferred = infer_platform_from_url(url)
    if inferred != "岗位链接":
        return inferred
    return fallback or "浏览器扩展"


def extension_patrol_trigger(payload: dict[str, Any]) -> str:
    value = payload_text(payload, "patrol_trigger", 40) or payload_text(payload, "trigger_type", 40)
    if value in {"manual_extension", "manual", "scheduled", "executor", "manual_browser", "scheduled_executor"}:
        return value
    return "manual_extension"


def extension_patrol_scope(payload: dict[str, Any]) -> str:
    return payload_text(payload, "patrol_scope", 80) or "single_conversation"


def extension_keyword(url: str, title: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ["query", "keyword", "key", "kw", "q"]:
        values = query.get(key)
        if values and values[0].strip():
            return values[0].strip()[:80]
    return title.strip()[:80] or "扩展采集"


def extension_anchors(payload: dict[str, Any]) -> list[dict[str, str]]:
    anchors: list[dict[str, str]] = []
    for item in list(payload.get("links") or [])[:EXTENSION_LINK_LIMIT]:
        if not isinstance(item, dict):
            continue
        anchors.append(
            {
                "href": str(item.get("href") or item.get("url") or ""),
                "text": str(item.get("text") or ""),
                "title": str(item.get("title") or ""),
                "context": str(item.get("context") or ""),
            }
        )
    return anchors


def extension_cards(payload: dict[str, Any]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for item in list(payload.get("cards") or [])[:EXTENSION_LINK_LIMIT]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or item.get("context") or "")
        cards.append(
            {
                "href": str(item.get("href") or item.get("url") or ""),
                "text": str(item.get("title") or ""),
                "title": str(item.get("title") or ""),
                "context": text,
            }
        )
    return cards


def extension_candidate_sources(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [*extension_cards(payload), *extension_anchors(payload)]


def find_job_for_conversation(
    conn: Any,
    source_url: str,
    text: str,
    platform: str = "",
    page_title: str = "",
    text_scope: str = "",
) -> dict[str, Any] | None:
    existing = find_existing_job_by_source_url(conn, source_url)
    if existing and source_url_identifies_job(source_url) and platform_matches(platform, str(existing["platform"] or "")):
        return {key: existing[key] for key in existing.keys()}
    clean_text = text or ""
    rows = conn.execute("SELECT * FROM job_postings ORDER BY updated_at DESC, id DESC LIMIT 300").fetchall()
    for row in rows:
        company = str(row["company"] or "")
        if company and company in clean_text:
            return {key: row[key] for key in row.keys()}
    title_matches = [
        row
        for row in rows
        if is_distinctive_conversation_title(str(row["title"] or ""))
        and str(row["title"] or "") in clean_text
        and platform_matches(platform, str(row["platform"] or ""))
    ]
    if len(title_matches) == 1:
        row = title_matches[0]
        return {key: row[key] for key in row.keys()}
    return None


def source_url_identifies_job(url: str) -> bool:
    return bool(url) and not is_broad_conversation_source_url(url)


def is_broad_conversation_source_url(url: str, page_title: str = "") -> bool:
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "/").rstrip("/") or "/"
    lowered = f"{path}?{parsed.query}".lower()
    if not host:
        return True
    if "首页" in (page_title or "") or "home" in (page_title or "").lower():
        return True
    if path == "/":
        return True
    detail_tokens = ["job_detail", "/job/", "/intern/", "/position/"]
    if any(token in lowered for token in detail_tokens):
        return False
    broad_tokens = ["search", "jobs", "joblist", "job-list", "web/geek/jobs", "interns"]
    chat_tokens = ["/im/", "/chat", "/message", "/messages", "/conversation"]
    return any(token in lowered for token in broad_tokens) or any(token in lowered for token in chat_tokens)


def platform_matches(observed_platform: str, job_platform: str) -> bool:
    observed = (observed_platform or "").strip()
    saved = (job_platform or "").strip()
    if not observed or not saved:
        return True
    if "岗位链接" in {observed, saved}:
        return True
    return observed == saved


def is_distinctive_conversation_title(title: str) -> bool:
    compact = re.sub(r"\s+", "", title or "")
    if len(compact) < 5:
        return False
    too_generic = {"AI应用开发", "AI开发", "Agent开发", "后端开发", "软件开发", "Python开发"}
    if compact in too_generic:
        return False
    return bool(re.search(r"(实习|工程师|开发|后端|算法|agent|ai|大模型|llm|rag)", title or "", re.IGNORECASE))


def find_duplicate_conversation_capture(
    conn: Any,
    *,
    job_id: int | None,
    source_url: str,
    platform: str,
    page_title: str,
    conversation_text: str,
) -> dict[str, Any] | None:
    if job_id:
        row = conn.execute(
            """
            SELECT id, conversation_text, created_at
            FROM conversation_captures
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row and str(row["conversation_text"] or "") == conversation_text:
            return {key: row[key] for key in row.keys()}

    row = conn.execute(
        """
        SELECT id, conversation_text, created_at
        FROM conversation_captures
        WHERE source_url = ?
          AND platform = ?
          AND page_title = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (source_url, platform, page_title),
    ).fetchone()
    if row and str(row["conversation_text"] or "") == conversation_text:
        return {key: row[key] for key in row.keys()}
    return None


def try_llm_conversation_decision(text: str, job: dict[str, Any] | None, fallback: dict[str, Any]) -> dict[str, Any]:
    client = client_for_task("hr_reply_classify")
    if not client or not client.configured:
        return fallback
    prompt = (
        "你是求职 Agent 的 HR 对话分类器。只输出 JSON。"
        "字段：message_type, summary, action_required(boolean), reason, draft_message, risk_flags(array)。"
        "如果涉及面试/笔试/约时间，message_type=面试邀请，不要替候选人约时间。"
        "如果疑似 HR 群发、系统推荐、低相关岗位或无需回复，message_type=无需回复 且 draft_message 为空。"
        "如果涉及简历、联系方式、身份证、银行卡、押金、培训费、贷款、薪资谈判或无关内容，action_required=true 且 draft_message 为空。"
        "普通岗位相关问题可生成礼貌、学生真诚、不夸大的中文草稿。"
    )
    try:
        result = client.complete_json(
            [
                {"role": "system", "content": prompt},
                {"role": "user", "content": dumps({"job": job or {}, "conversation": text[:12000], "fallback": fallback})},
            ]
        )
    except Exception as exc:
        client.log_error(str(exc))
        return fallback

    merged = dict(fallback)
    allowed_types = {"岗位沟通", "面试邀请", "需要我处理", "无关内容", "无需回复"}
    result_type = str(result.get("message_type") or "").strip()
    if result_type and result_type not in allowed_types:
        return fallback
    for key in ["message_type", "summary", "reason", "draft_message"]:
        if isinstance(result.get(key), str):
            merged[key] = result[key].strip()
    if isinstance(result.get("action_required"), bool):
        merged["action_required"] = result["action_required"]
    if isinstance(result.get("risk_flags"), list):
        merged["risk_flags"] = [str(item).strip() for item in result["risk_flags"] if str(item).strip()]
    if merged.get("action_required") and merged.get("message_type") != "岗位沟通":
        merged["draft_message"] = ""
    return merged


def enforce_reply_gate(decision: dict[str, Any], job: dict[str, Any] | None, job_id: int | None) -> dict[str, Any]:
    message_type = str(decision.get("message_type") or "")
    if message_type == "面试邀请":
        return decision

    updated = dict(decision)
    reasons = [str(item).strip() for item in list(updated.get("reply_gate_reasons") or []) if str(item).strip()]
    flags = [str(item).strip() for item in list(updated.get("risk_flags") or []) if str(item).strip()]
    gate = str(updated.get("reply_gate") or "allow")
    job_recommendation = str((job or {}).get("recommendation") or "")
    job_match_level = str((job or {}).get("match_level") or "")

    if job and (job_recommendation == "跳过" or job_match_level == "低匹配"):
        gate = "skip"
        reasons.append("岗位此前被评为低匹配或建议跳过")
        flags.append("低匹配岗位默认不进入自动回复")

    if gate == "skip":
        updated.update(
            {
                "message_type": "无需回复",
                "action_required": False,
                "draft_message": "",
                "draft_type": "初筛跳过",
                "reason": "；".join(reasons) or "回复闸门判断无需回复。",
                "risk_flags": dedupe_texts(flags),
            }
        )
    elif gate == "manual" and message_type == "岗位沟通" and not job_id:
        flags.append("未匹配到岗位，禁止自动生成回复")
        updated.update(
            {
                "message_type": "需要我处理",
                "action_required": True,
                "draft_message": "",
                "draft_type": "初筛待确认",
                "reason": "；".join(reasons) or "未匹配到岗位，需人工确认是否值得回复。",
                "risk_flags": dedupe_texts(flags),
            }
        )
    else:
        updated["risk_flags"] = dedupe_texts(flags)
    updated["reply_gate"] = gate
    updated["reply_gate_reasons"] = dedupe_texts(reasons)
    return updated


def classify_conversation_for_patrol_preview(text: str, job: dict[str, Any] | None) -> dict[str, Any]:
    from .services.conversation import classify_conversation as rule_classify_conversation

    return rule_classify_conversation(text, job)


def dedupe_texts(values: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def evaluate_draft_send_gate(conn: Any, draft: Any, message: str) -> dict[str, Any]:
    reasons: list[str] = []
    draft_type = str(draft["draft_type"] or "")
    old_status = str(draft["status"] or "")
    job_id = int(draft["job_id"]) if draft["job_id"] else None
    capture_id = int(draft["capture_id"]) if draft["capture_id"] else None

    if old_status != "待确认":
        reasons.append(f"草稿当前状态为「{old_status or '未设置'}」，只能发送待确认草稿")
    if not message.strip():
        reasons.append("草稿内容为空")
    if not job_id:
        reasons.append("未匹配到岗位")

    blocked_draft_types = {"初筛跳过", "初筛待确认", "自主询问暂停", "面试邀请", "需要我处理", "无关内容"}
    if draft_type in blocked_draft_types:
        reasons.append(f"草稿类型为「{draft_type}」，必须人工处理")

    capture_type = ""
    if capture_id:
        capture = conn.execute(
            "SELECT message_type, action_required FROM conversation_captures WHERE id = ?",
            (capture_id,),
        ).fetchone()
        if capture:
            capture_type = str(capture["message_type"] or "")
            if capture_type != "岗位沟通":
                reasons.append(f"对话分类为「{capture_type or '未分类'}」，不能进入发送流程")
            if capture["action_required"]:
                reasons.append("对话采集标记为需要人工处理")

    job_status = ""
    job_recommendation = ""
    job_match_level = ""
    if job_id:
        job = conn.execute(
            "SELECT status, recommendation, match_level FROM job_postings WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job:
            job_status = str(job["status"] or "")
            job_recommendation = str(job["recommendation"] or "")
            job_match_level = str(job["match_level"] or "")
            if job_status in {"面试邀请", "待面试"}:
                reasons.append(f"岗位状态为「{job_status}」，后续沟通需人工确认")
            if job_recommendation == "跳过" or job_match_level == "低匹配":
                reasons.append("岗位此前被评为低匹配或建议跳过")

    return {
        "allowed": not reasons,
        "reasons": dedupe_texts(reasons),
        "draft_id": int(draft["id"]),
        "draft_status": old_status,
        "draft_type": draft_type,
        "message_length": len(message.strip()),
        "job_id": job_id,
        "job_status": job_status,
        "job_recommendation": job_recommendation,
        "job_match_level": job_match_level,
        "capture_id": capture_id,
        "capture_message_type": capture_type,
    }


def build_communication_execution_plan(
    conn: Any,
    *,
    trigger_type: str = "manual",
    limit: int = COMMUNICATION_EXECUTOR_PLAN_LIMIT,
) -> dict[str, Any]:
    policy = communication_policy()
    if policy["mode"] == "off":
        return {
            "ok": True,
            "dry_run": True,
            "trigger_type": trigger_type,
            "status": "已关闭",
            "note": "沟通模式为关闭，自动回复执行器未扫描候选草稿。",
            "policy_mode": policy["mode"],
            "candidate_count": 0,
            "allowed_count": 0,
            "blocked_count": 0,
            "plans": [],
        }

    rows = conn.execute(
        """
        SELECT
            d.*,
            j.title AS job_title,
            j.company AS company,
            j.source_url AS job_source_url,
            j.status AS job_status,
            j.recommendation AS job_recommendation,
            j.match_level AS job_match_level,
            c.message_type AS capture_message_type,
            c.source_url AS capture_source_url
        FROM message_drafts d
        LEFT JOIN job_postings j ON j.id = d.job_id
        LEFT JOIN conversation_captures c ON c.id = d.capture_id
        WHERE d.status = '待确认'
        ORDER BY d.created_at ASC, d.id ASC
        LIMIT ?
        """,
        (max(1, min(int(limit or COMMUNICATION_EXECUTOR_PLAN_LIMIT), 100)),),
    ).fetchall()

    plans: list[dict[str, Any]] = []
    for row in rows:
        message = str(row["message"] or "")
        gate = evaluate_draft_send_gate(conn, row, message)
        plans.append(
            {
                "draft_id": int(row["id"]),
                "job_id": int(row["job_id"]) if row["job_id"] else None,
                "platform": str(row["platform"] or ""),
                "company": str(row["company"] or ""),
                "job_title": str(row["job_title"] or ""),
                "source_url": str(row["capture_source_url"] or row["job_source_url"] or ""),
                "draft_type": str(row["draft_type"] or ""),
                "communication_mode": str(row["communication_mode"] or policy["mode"]),
                "followup_index": int(row["followup_index"] or 0),
                "followup_limit": int(row["followup_limit"] or 0),
                "message_length": len(message.strip()),
                "decision": "计划发送" if gate["allowed"] else "拦截",
                "gate_allowed": bool(gate["allowed"]),
                "gate_reasons": gate["reasons"],
                "job_status": str(row["job_status"] or ""),
                "job_recommendation": str(row["job_recommendation"] or ""),
                "job_match_level": str(row["job_match_level"] or ""),
                "capture_message_type": str(row["capture_message_type"] or ""),
            }
        )

    allowed_count = sum(1 for item in plans if item["gate_allowed"])
    blocked_count = len(plans) - allowed_count
    if not plans:
        status = "无候选"
        note = "没有待确认草稿可进入自动回复演练。"
    else:
        status = "演练完成"
        note = f"扫描 {len(plans)} 条待确认草稿：计划发送 {allowed_count} 条，拦截 {blocked_count} 条。"

    return {
        "ok": True,
        "dry_run": True,
        "trigger_type": trigger_type,
        "status": status,
        "note": note,
        "policy_mode": policy["mode"],
        "candidate_count": len(plans),
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
        "plans": plans,
    }


def run_communication_executor_dry_run(trigger_type: str = "manual") -> dict[str, Any]:
    with connect() as conn:
        plan = build_communication_execution_plan(conn, trigger_type=trigger_type)
        log_agent_action(
            conn,
            action_type="communication_executor_dry_run",
            status=str(plan["status"]),
            summary=str(plan["note"]),
            decision={
                "dry_run": True,
                "trigger_type": trigger_type,
                "policy_mode": plan["policy_mode"],
                "candidate_count": plan["candidate_count"],
                "allowed_count": plan["allowed_count"],
                "blocked_count": plan["blocked_count"],
                "plans": plan["plans"],
                "message_text_saved": False,
            },
        )
    return plan


def run_communication_browser_dry_run(trigger_type: str = "manual") -> dict[str, Any]:
    with connect() as conn:
        execution_plan = build_communication_execution_plan(conn, trigger_type=trigger_type)
        browser_plan = build_browser_send_adapter_plan(execution_plan)
        log_agent_action(
            conn,
            action_type="communication_browser_dry_run",
            status=str(browser_plan["status"]),
            summary=str(browser_plan["note"]),
            decision={
                "dry_run": True,
                "trigger_type": trigger_type,
                "policy_mode": browser_plan["policy_mode"],
                "candidate_count": browser_plan["candidate_count"],
                "allowed_count": browser_plan["allowed_count"],
                "blocked_count": browser_plan["blocked_count"],
                "browser_ready_count": browser_plan["browser_ready_count"],
                "browser_manual_count": browser_plan["browser_manual_count"],
                "browser_skipped_count": browser_plan["browser_skipped_count"],
                "browser_plans": browser_plan["browser_plans"],
                "message_text_saved": False,
                "browser_clicked": False,
                "message_filled": False,
            },
        )
    return browser_plan


def run_communication_browser_probe_dry_run(trigger_type: str = "manual_browser") -> dict[str, Any]:
    with connect() as conn:
        execution_plan = build_communication_execution_plan(conn, trigger_type=trigger_type)
        browser_plan = build_browser_send_adapter_plan(execution_plan)
    try:
        probe_plan = probe_browser_send_adapter_plan(browser_plan)
    except ValueError as exc:
        probe_plan = {
            **browser_plan,
            "status": "浏览器未连接",
            "note": str(exc)[:500],
            "browser_probe_dry_run": True,
            "browser_connected": False,
            "probe_results": [],
            "message_text_saved": False,
        }
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="communication_browser_probe",
            status=str(probe_plan["status"]),
            summary=str(probe_plan["note"]),
            decision={
                "dry_run": True,
                "trigger_type": trigger_type,
                "policy_mode": probe_plan.get("policy_mode", ""),
                "candidate_count": probe_plan.get("candidate_count", 0),
                "allowed_count": probe_plan.get("allowed_count", 0),
                "blocked_count": probe_plan.get("blocked_count", 0),
                "browser_connected": bool(probe_plan.get("browser_connected")),
                "page_count": int(probe_plan.get("page_count") or 0),
                "probe_ready_count": int(probe_plan.get("probe_ready_count") or 0),
                "probe_partial_count": int(probe_plan.get("probe_partial_count") or 0),
                "probe_not_found_count": int(probe_plan.get("probe_not_found_count") or 0),
                "probe_skipped_count": int(probe_plan.get("probe_skipped_count") or 0),
                "probe_results": probe_plan.get("probe_results", []),
                "message_text_saved": False,
                "browser_clicked": False,
                "message_filled": False,
            },
        )
    return probe_plan


def communication_executor_page_status(conn: Any) -> dict[str, Any]:
    preview = build_communication_execution_plan(conn, trigger_type="page_preview")
    latest_rows = conn.execute(
        """
        SELECT id, action_type, status, summary, decision_json, created_at
        FROM agent_action_logs
        WHERE action_type IN (
            'communication_executor_dry_run',
            'communication_browser_dry_run',
            'communication_browser_probe'
        )
        ORDER BY id DESC
        LIMIT 3
        """
    ).fetchall()
    latest = []
    for row in latest_rows:
        decision = loads(row["decision_json"], {}) or {}
        latest.append(
            {
                "id": row["id"],
                "action_type": row["action_type"],
                "status": row["status"],
                "summary": row["summary"],
                "created_at": row["created_at"],
                "candidate_count": int(decision.get("candidate_count") or 0),
                "allowed_count": int(decision.get("allowed_count") or 0),
                "blocked_count": int(decision.get("blocked_count") or 0),
                "browser_ready_count": int(decision.get("browser_ready_count") or 0),
                "browser_skipped_count": int(decision.get("browser_skipped_count") or 0),
                "page_count": int(decision.get("page_count") or 0),
                "probe_ready_count": int(decision.get("probe_ready_count") or 0),
                "probe_partial_count": int(decision.get("probe_partial_count") or 0),
                "probe_not_found_count": int(decision.get("probe_not_found_count") or 0),
                "probe_skipped_count": int(decision.get("probe_skipped_count") or 0),
            }
        )
    return {
        "preview": preview,
        "latest": latest,
        "pending_count": int(preview.get("candidate_count") or 0),
        "allowed_count": int(preview.get("allowed_count") or 0),
        "blocked_count": int(preview.get("blocked_count") or 0),
    }


def find_existing_demo_draft(conn: Any) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT d.id, d.job_id, d.platform
        FROM message_drafts d
        WHERE d.status = '待确认'
          AND d.draft_type = '岗位沟通'
          AND d.reason LIKE '%本地演练%'
        ORDER BY d.id DESC
        LIMIT 1
        """
    ).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def create_demo_communication_draft(conn: Any) -> dict[str, Any]:
    existing = find_existing_demo_draft(conn)
    if existing:
        job_id = int(existing["job_id"]) if existing.get("job_id") else None
        log_agent_action(
            conn,
            action_type="demo_draft_created",
            status="已存在",
            summary="已有本地演练草稿，可直接执行自动回复 dry-run。",
            platform=str(existing.get("platform") or ""),
            job_id=job_id,
            draft_id=int(existing["id"]),
            decision={
                "local_demo": True,
                "existing": True,
                "real_platform_data": False,
            },
        )
        return {"created": False, "draft_id": int(existing["id"]), "job_id": job_id}

    now = utc_now()
    default_resume = conn.execute("SELECT id FROM resume_versions WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
    resume_id = int(default_resume["id"]) if default_resume else None
    jd_text = (
        "本地演练岗位，不来自真实招聘平台。\n"
        "岗位名称：AI 应用开发实习生\n"
        "公司名称：本地演练智能科技有限公司\n"
        "工作内容：参与 Python/FastAPI/RAG/Agent 应用开发，整理需求并完成接口与工具调用演示。\n"
        "要求：了解 Python，愿意学习大模型应用开发。"
    )
    cursor = conn.execute(
        """
        INSERT INTO job_postings (
            platform, source_url, title, company, city, salary_text, jd_text,
            selected_resume_id, match_score, match_level, risk_level, recommendation,
            status, analysis_source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "Boss 直聘",
            "https://www.zhipin.com/job_detail/local-demo.html",
            "AI 应用开发实习生（本地演练）",
            "本地演练智能科技有限公司",
            "远程/本地演练",
            "演练数据",
            jd_text,
            resume_id,
            88,
            "高匹配",
            "低风险",
            "可投递",
            "演练",
            "local_demo",
            now,
            now,
        ),
    )
    job_id = int(cursor.lastrowid)
    conversation_text = (
        "HR：您好，这里是本地演练智能科技有限公司。\n"
        "HR：我们在招 AI 应用开发实习生，可以了解工作内容和实习安排。"
    )
    capture_cursor = conn.execute(
        """
        INSERT INTO conversation_captures (
            job_id, platform, source_url, page_title, raw_visible_text,
            conversation_text, ignored_lines_json, message_type, summary,
            action_required, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            "Boss 直聘",
            "https://www.zhipin.com/job_detail/local-demo.html",
            "本地演练 HR 对话",
            conversation_text,
            conversation_text,
            "[]",
            "岗位沟通",
            "本地演练 HR 询问是否了解 AI 应用开发实习安排。",
            0,
            now,
        ),
    )
    capture_id = int(capture_cursor.lastrowid)
    message = "您好，感谢回复。我对这个 AI 应用开发实习岗位比较感兴趣，想了解主要工作内容、技术栈和实习安排。"
    draft_cursor = conn.execute(
        """
        INSERT INTO message_drafts (
            capture_id, job_id, platform, draft_type, status,
            communication_mode, followup_index, followup_limit,
            reason, message, risk_flags_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            capture_id,
            job_id,
            "Boss 直聘",
            "岗位沟通",
            "待确认",
            communication_policy()["mode"],
            0,
            0,
            "本地演练草稿：用于验证自动回复 dry-run 链路，不来自真实 HR 对话。",
            message,
            "[]",
            now,
            now,
        ),
    )
    draft_id = int(draft_cursor.lastrowid)
    log_agent_action(
        conn,
        action_type="demo_draft_created",
        status="已创建",
        summary="已创建本地演练草稿，用于验证自动回复 dry-run 链路。",
        platform="Boss 直聘",
        job_id=job_id,
        capture_id=capture_id,
        draft_id=draft_id,
        decision={
            "local_demo": True,
            "message_length": len(message),
            "message_text_saved": True,
            "real_platform_data": False,
        },
    )
    return {"created": True, "draft_id": draft_id, "job_id": job_id}


def apply_communication_policy(
    conn: Any,
    decision: dict[str, Any],
    policy: dict[str, Any],
    job_id: int | None,
) -> dict[str, Any]:
    mode = str(policy.get("mode") or "draft")
    if (
        mode == "autonomous"
        and not job_id
        and decision.get("message_type") == "需要我处理"
        and decision.get("draft_type") == "初筛待确认"
    ):
        updated = dict(decision)
        reason = str(updated.get("reason") or "").strip()
        flags = list(updated.get("risk_flags") or [])
        flags.append("未匹配到岗位，暂停自主询问并等待用户确认")
        updated.update(
            {
                "communication_mode": mode,
                "followup_limit": int(policy.get("max_auto_followups") or 0),
                "followup_index": 0,
                "draft_type": "自主询问暂停",
                "risk_flags": dedupe_texts(flags),
                "reason": "；".join(item for item in [reason, "未匹配到岗位，无法可靠记录自主询问轮次。"] if item),
            }
        )
        return updated
    if decision.get("message_type") != "岗位沟通" or not str(decision.get("draft_message") or "").strip():
        return decision

    updated = dict(decision)
    reason = str(updated.get("reason") or "").strip()
    updated["communication_mode"] = mode
    if mode == "autonomous":
        max_followups = int(policy.get("max_auto_followups") or 0)
        updated["followup_limit"] = max_followups
        if not job_id:
            flags = list(updated.get("risk_flags") or [])
            flags.append("未匹配到岗位，暂停自主询问并等待用户确认")
            updated.update(
                {
                    "action_required": True,
                    "draft_message": "",
                    "draft_type": "自主询问暂停",
                    "followup_index": 0,
                    "risk_flags": flags,
                    "reason": "；".join(item for item in [reason, "未匹配到岗位，无法可靠记录自主询问轮次。"] if item),
                }
            )
            return updated

        sent_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM message_drafts
                WHERE job_id = ?
                  AND status = '已发送'
                  AND draft_type IN ('岗位沟通', '自主询问候选')
                """,
                (job_id,),
            ).fetchone()["count"]
        )
        if sent_count >= max_followups:
            flags = list(updated.get("risk_flags") or [])
            flags.append(f"自主询问已达到 {max_followups} 轮上限，暂停并等待用户确认")
            updated.update(
                {
                    "action_required": True,
                    "draft_message": "",
                    "draft_type": "自主询问暂停",
                    "followup_index": sent_count,
                    "risk_flags": flags,
                    "reason": "；".join(item for item in [reason, f"自主询问已达到 {max_followups} 轮上限。"] if item),
                }
            )
            return updated
        updated["draft_type"] = "自主询问候选"
        updated["followup_index"] = sent_count + 1
        updated["reason"] = "；".join(
            item
            for item in [
                reason,
                f"当前为自主询问模式，已发送 {sent_count}/{max_followups} 轮；本阶段仍需人工确认发送。",
            ]
            if item
        )
        return updated

    updated["draft_type"] = "岗位沟通"
    updated["followup_index"] = 0
    updated["followup_limit"] = 0
    updated["reason"] = "；".join(item for item in [reason, "当前为草稿模式，需人工确认后发送。"] if item)
    return updated


def create_conversation_from_extension(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    policy = communication_policy()
    if policy["mode"] == "off":
        return {
            "ok": True,
            "capture_type": "conversation",
            "skipped": True,
            "message_type": "沟通模式已关闭",
            "reason": "沟通模式为关闭，不保存、不分析、不生成草稿。",
            "redirect_url": "/communications",
        }

    patrol_trigger = extension_patrol_trigger(payload)
    patrol_scope = extension_patrol_scope(payload)
    control = automation_control()
    if control["paused"]:
        raw_source_url = payload_text(payload, "url", 1000)
        page_title = payload_text(payload, "title", 300)
        platform = extension_platform(raw_source_url, payload_text(payload, "platform", 80))
        with connect() as conn:
            patrol_run_id = insert_message_patrol_run(
                conn,
                trigger_type=patrol_trigger,
                platform=platform,
                scope=patrol_scope,
                status="已暂停",
                checked_count=1,
                skipped_count=1,
                note="自动化已暂停，跳过本次对话巡检。",
                source_url=raw_source_url,
                page_title=page_title,
            )
            log_agent_action(
                conn,
                action_type="automation_paused",
                status="已暂停",
                summary="自动化已暂停，跳过本次对话采集。",
                platform=platform,
                decision={
                    "patrol_run_id": patrol_run_id,
                    "pause_reason": control["pause_reason"],
                    "updated_at": control["updated_at"],
                },
            )
        return {
            "ok": True,
            "capture_type": "conversation",
            "skipped": True,
            "message_type": "自动化已暂停",
            "reason": control["pause_reason"] or "自动化已暂停，不保存、不分析、不生成草稿。",
            "patrol_run_id": patrol_run_id,
            "redirect_url": "/communications",
        }

    raw_url = payload_text(payload, "url", 1000)
    if not raw_url:
        return api_error("缺少当前对话页面 URL。")
    try:
        url = ensure_public_http_url(raw_url)
    except ValueError as exc:
        return api_error(str(exc))

    title = payload_text(payload, "title", 300)
    platform = extension_platform(url, payload_text(payload, "platform", 80))
    prepared_text = prepare_conversation_text(str(payload.get("text") or ""))
    conversation_text = str(prepared_text["clean_text"])
    if len(conversation_text) < 20:
        return api_error("当前页面可见对话文本太短，无法分析。")

    with connect() as conn:
        job = find_job_for_conversation(
            conn,
            url,
            conversation_text,
            platform=platform,
            page_title=title,
            text_scope=str(payload.get("text_scope") or ""),
        )
        job_id = int(job["id"]) if job else None
        fingerprint_key = conversation_fingerprint_key(job_id=job_id, source_url=url, platform=platform, page_title=title)
        fingerprint = conversation_text_fingerprint(conversation_text)
        ignored = ignored_message_fingerprint(fingerprint_key, fingerprint)
        if ignored:
            patrol_run_id = insert_message_patrol_run(
                conn,
                trigger_type=patrol_trigger,
                platform=platform,
                scope=patrol_scope,
                status="已忽略",
                checked_count=1,
                skipped_count=1,
                note="这条对话已被手动忽略，未保存聊天全文、未调用模型。",
                source_url=url,
                page_title=title,
                fingerprint_key=fingerprint_key,
                fingerprint=fingerprint,
                job_id=job_id,
            )
            log_agent_action(
                conn,
                action_type="message_patrol_observation",
                status="已忽略",
                summary="这条对话已被手动忽略，跳过采集和草稿生成。",
                platform=platform,
                job_id=job_id,
                decision={
                    "patrol_run_id": patrol_run_id,
                    "fingerprint_key": fingerprint_key,
                    "ignored": True,
                    "ignore_reason": str(ignored.get("reason") or ""),
                    "model_called": False,
                    "text_saved": False,
                },
            )
            return {
                "ok": True,
                "capture_type": "conversation",
                "skipped": True,
                "message_type": "已忽略",
                "reason": "这条对话已被手动忽略。",
                "patrol_run_id": patrol_run_id,
                "redirect_url": "/communications",
            }

        duplicate = find_duplicate_conversation_capture(
            conn,
            job_id=job_id,
            source_url=url,
            platform=platform,
            page_title=title,
            conversation_text=conversation_text,
        )
        if duplicate:
            patrol_run_id = insert_message_patrol_run(
                conn,
                trigger_type=patrol_trigger,
                platform=platform,
                scope=patrol_scope,
                status="无新内容",
                checked_count=1,
                skipped_count=1,
                note="清洗后的对话内容与上一条采集一致，未调用模型。",
                source_url=url,
                page_title=title,
                fingerprint_key=fingerprint_key,
                fingerprint=fingerprint,
                job_id=job_id,
                capture_id=int(duplicate["id"]),
            )
            log_agent_action(
                conn,
                action_type="conversation_diff_check",
                status="无新内容",
                summary="清洗后的对话内容与上一条采集一致，已跳过分析。",
                platform=platform,
                job_id=job_id,
                capture_id=int(duplicate["id"]),
                decision={
                    "patrol_run_id": patrol_run_id,
                    "matched_capture_id": int(duplicate["id"]),
                    "text_length": len(conversation_text),
                    "matched_by": "job_id" if job_id else "source_url",
                },
            )
            return {
                "ok": True,
                "capture_type": "conversation",
                "skipped": True,
                "message_type": "无新内容",
                "reason": "当前对话没有新增内容，已跳过分析和草稿生成。",
                "existing_capture_id": int(duplicate["id"]),
                "patrol_run_id": patrol_run_id,
                "redirect_url": "/communications",
            }

        decision = classify_conversation(conversation_text, job)
        decision = try_llm_conversation_decision(conversation_text, job, decision)
        decision = enforce_reply_gate(decision, job, job_id)
        now = utc_now()
        decision = apply_communication_policy(conn, decision, policy, job_id)
        cursor = conn.execute(
            """
            INSERT INTO conversation_captures (
                job_id, platform, source_url, page_title, raw_visible_text,
                conversation_text, ignored_lines_json, message_type, summary,
                action_required, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                platform,
                url,
                title,
                str(prepared_text["raw_text"]),
                conversation_text,
                dumps(prepared_text["ignored_lines"]),
                str(decision.get("message_type") or ""),
                str(decision.get("summary") or ""),
                1 if decision.get("action_required") else 0,
                now,
            ),
        )
        capture_id = int(cursor.lastrowid)
        draft_id = None
        draft_message = str(decision.get("draft_message") or "").strip()
        risk_flags = list(decision.get("risk_flags") or [])
        draft_status = "待确认" if draft_message else "需要我处理"
        if draft_message or decision.get("action_required"):
            draft_cursor = conn.execute(
                """
                INSERT INTO message_drafts (
                    capture_id, job_id, platform, draft_type, status,
                    communication_mode, followup_index, followup_limit,
                    reason, message, risk_flags_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    job_id,
                    platform,
                    str(decision.get("draft_type") or decision.get("message_type") or "岗位沟通"),
                    draft_status,
                    str(decision.get("communication_mode") or policy["mode"]),
                    int(decision.get("followup_index") or 0),
                    int(decision.get("followup_limit") or 0),
                    str(decision.get("reason") or ""),
                    draft_message,
                    dumps(risk_flags),
                    now,
                    now,
                ),
            )
            draft_id = int(draft_cursor.lastrowid)
        if job_id and decision.get("message_type") == "面试邀请":
            conn.execute("UPDATE job_postings SET status = ?, updated_at = ? WHERE id = ?", ("面试邀请", now, job_id))
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "面试邀请识别", str(decision.get("summary") or "HR 对话中提到面试或笔试。"), now),
            )
        if decision.get("message_type") == "面试邀请":
            action_status = "面试邀请"
        elif decision.get("message_type") == "无需回复":
            action_status = "无需回复"
        elif draft_id and draft_status == "待确认":
            action_status = str(decision.get("draft_type") or "草稿待确认")
        elif draft_id and draft_status == "需要我处理":
            action_status = "需要我处理"
        else:
            action_status = "已采集"
        patrol_status = "无需回复" if decision.get("message_type") == "无需回复" else "已处理"
        patrol_new_count = 0 if decision.get("message_type") == "无需回复" else 1
        patrol_skipped_count = 1 if decision.get("message_type") == "无需回复" else 0
        patrol_note = (
            "回复闸门判断无需回复，已保存采集依据但未生成草稿。"
            if decision.get("message_type") == "无需回复"
            else "发现新对话内容，已进入分类和草稿流程。"
        )
        patrol_run_id = insert_message_patrol_run(
            conn,
            trigger_type=patrol_trigger,
            platform=platform,
            scope=patrol_scope,
            status=patrol_status,
            checked_count=1,
            new_count=patrol_new_count,
            skipped_count=patrol_skipped_count,
            note=patrol_note,
            source_url=url,
            page_title=title,
            fingerprint_key=fingerprint_key,
            fingerprint=fingerprint,
            job_id=job_id,
            capture_id=capture_id,
        )
        log_agent_action(
            conn,
            action_type="conversation_capture",
            status=action_status,
            summary=str(decision.get("summary") or "")[:500],
            platform=platform,
            job_id=job_id,
            capture_id=capture_id,
            draft_id=draft_id,
            decision={
                "patrol_run_id": patrol_run_id,
                "message_type": decision.get("message_type"),
                "draft_type": decision.get("draft_type") or decision.get("message_type"),
                "draft_status": draft_status if draft_id else "",
                "communication_mode": policy["mode"],
                "followup_index": int(decision.get("followup_index") or 0),
                "followup_limit": int(decision.get("followup_limit") or 0),
                "action_required": bool(decision.get("action_required")),
                "risk_flags": risk_flags,
                "reply_gate": decision.get("reply_gate"),
                "reply_gate_reasons": decision.get("reply_gate_reasons"),
            },
        )

    response_payload = {
        "ok": True,
        "capture_type": "conversation",
        "capture_id": capture_id,
        "draft_id": draft_id,
        "message_type": decision.get("message_type"),
        "communication_mode": policy["mode"],
        "communication_mode_label": policy["mode_label"],
        "patrol_run_id": patrol_run_id,
        "redirect_url": "/communications",
    }
    if decision.get("message_type") == "无需回复":
        response_payload["skipped"] = True
    return response_payload


def normalize_message_patrol_observation_request(payload: Any) -> tuple[dict[str, Any] | None, str]:
    if isinstance(payload, list):
        observations = payload
        return (
            {
                "dry_run": True,
                "executor": "external",
                "trigger_type": "executor",
                "scope": "scheduled_patrol",
                "observations": observations[:MESSAGE_PATROL_OBSERVATION_LIMIT],
                "truncated": len(observations) > MESSAGE_PATROL_OBSERVATION_LIMIT,
            },
            "",
        )
    if not isinstance(payload, dict):
        return None, "请求体必须是 JSON 对象或 observation 数组。"

    raw_observations = payload.get("observations")
    if raw_observations is None and any(key in payload for key in ("url", "source_url", "text", "visible_text")):
        raw_observations = [payload]
    if not isinstance(raw_observations, list):
        return None, "observations 必须是数组，或直接提交单条 observation 对象。"
    if not raw_observations:
        return None, "至少需要提供 1 条 observation。"

    trigger_type = payload_text(payload, "trigger_type", 40) or payload_text(payload, "patrol_trigger", 40) or "executor"
    if trigger_type not in {"manual_extension", "manual", "scheduled", "executor", "manual_browser", "scheduled_executor"}:
        trigger_type = "executor"

    return (
        {
            "dry_run": payload_flag(payload.get("dry_run"), True),
            "executor": payload_text(payload, "executor", 80) or "external",
            "trigger_type": trigger_type,
            "scope": payload_text(payload, "scope", 80) or payload_text(payload, "patrol_scope", 80) or "scheduled_patrol",
            "observations": raw_observations[:MESSAGE_PATROL_OBSERVATION_LIMIT],
            "truncated": len(raw_observations) > MESSAGE_PATROL_OBSERVATION_LIMIT,
        },
        "",
    )


def json_response_payload(response: JSONResponse) -> dict[str, Any]:
    try:
        return loads(response.body.decode("utf-8"), {})
    except Exception:
        return {"ok": False, "error": "响应解析失败。"}


def observation_error_result(index: int, message: str, status: str = "错误") -> dict[str, Any]:
    return {
        "index": index,
        "ok": False,
        "status": status,
        "reason": message,
        "checked_count": 0,
        "new_count": 0,
        "skipped_count": 0,
        "error_count": 1,
    }


def write_observation_patrol_run(
    *,
    status: str,
    note: str,
    trigger_type: str,
    scope: str,
    platform: str,
    source_url: str,
    page_title: str,
    job_id: int | None = None,
    capture_id: int | None = None,
    fingerprint_key: str = "",
    fingerprint: str = "",
    checked_count: int = 1,
    new_count: int = 0,
    skipped_count: int = 0,
    error_count: int = 0,
    decision: dict[str, Any] | None = None,
) -> int:
    with connect() as conn:
        patrol_run_id = insert_message_patrol_run(
            conn,
            trigger_type=trigger_type,
            platform=platform,
            scope=scope,
            status=status,
            checked_count=checked_count,
            new_count=new_count,
            skipped_count=skipped_count,
            error_count=error_count,
            note=note,
            source_url=source_url,
            page_title=page_title,
            fingerprint_key=fingerprint_key,
            fingerprint=fingerprint,
            job_id=job_id,
            capture_id=capture_id,
        )
        log_agent_action(
            conn,
            action_type="message_patrol_observation",
            status=status,
            summary=note,
            platform=platform,
            job_id=job_id,
            capture_id=capture_id,
            decision={
                "patrol_run_id": patrol_run_id,
                "trigger_type": trigger_type,
                "scope": scope,
                "model_called": False,
                "text_saved": False,
                **(decision or {}),
            },
        )
    return patrol_run_id


def process_message_patrol_observation(
    observation: Any,
    *,
    index: int,
    dry_run: bool,
    executor: str,
    trigger_type: str,
    scope: str,
) -> dict[str, Any]:
    if not isinstance(observation, dict):
        return observation_error_result(index, "observation 必须是 JSON 对象。")

    raw_url = str(observation.get("url") or observation.get("source_url") or "").strip()[:1000]
    if not raw_url:
        return observation_error_result(index, "observation 缺少 url 或 source_url。")
    try:
        url = ensure_public_http_url(raw_url)
    except ValueError as exc:
        return observation_error_result(index, str(exc))

    title = str(observation.get("title") or observation.get("page_title") or "").strip()[:300]
    platform = extension_platform(url, str(observation.get("platform") or "").strip()[:80])
    base_result = {
        "index": index,
        "platform": platform,
        "source_url": url,
        "page_title": title,
        "checked_count": 1,
        "new_count": 0,
        "skipped_count": 0,
        "error_count": 0,
    }

    policy = communication_policy()
    if policy["mode"] == "off":
        return {
            **base_result,
            "ok": True,
            "skipped": True,
            "status": "沟通模式已关闭",
            "reason": "沟通模式为关闭，不保存、不分析、不生成草稿。",
            "skipped_count": 1,
        }

    control = automation_control()
    if control["paused"] and should_skip_observation_for_pause(dry_run=dry_run, trigger_type=trigger_type):
        patrol_run_id = write_observation_patrol_run(
            status="已暂停",
            note="自动化已暂停，跳过本次巡检观察。",
            trigger_type=trigger_type,
            scope=scope,
            platform=platform,
            source_url=url,
            page_title=title,
            skipped_count=1,
            decision={
                "executor": executor,
                "dry_run": dry_run,
                "pause_reason": control["pause_reason"],
                "updated_at": control["updated_at"],
            },
        )
        return {
            **base_result,
            "ok": True,
            "skipped": True,
            "status": "已暂停",
            "reason": control["pause_reason"] or "自动化已暂停。",
            "patrol_run_id": patrol_run_id,
            "skipped_count": 1,
        }

    raw_text = str(
        observation.get("text")
        or observation.get("visible_text")
        or observation.get("conversation_text")
        or ""
    )[:EXTENSION_TEXT_LIMIT]
    text_scope = str(observation.get("text_scope") or "")
    prepared_text = prepare_conversation_text(raw_text)
    conversation_text = str(prepared_text["clean_text"])
    if len(conversation_text) < 20:
        patrol_run_id = write_observation_patrol_run(
            status="文本过短",
            note="当前 observation 清洗后的可见对话文本太短，未调用模型。",
            trigger_type=trigger_type,
            scope=scope,
            platform=platform,
            source_url=url,
            page_title=title,
            skipped_count=1,
            decision={
                "executor": executor,
                "dry_run": dry_run,
                "text_length": len(conversation_text),
                "ignored_line_count": len(prepared_text["ignored_lines"]),
            },
        )
        return {
            **base_result,
            "ok": True,
            "skipped": True,
            "status": "文本过短",
            "reason": "当前页面可见对话文本太短，已跳过。",
            "patrol_run_id": patrol_run_id,
            "text_length": len(conversation_text),
            "skipped_count": 1,
        }

    if dry_run:
        with connect() as conn:
            job = find_job_for_conversation(
                conn,
                url,
                conversation_text,
                platform=platform,
                page_title=title,
                text_scope=text_scope,
            )
            job_id = int(job["id"]) if job else None
            duplicate = find_duplicate_conversation_capture(
                conn,
                job_id=job_id,
                source_url=url,
                platform=platform,
                page_title=title,
                conversation_text=conversation_text,
            )
        fingerprint_key = conversation_fingerprint_key(job_id=job_id, source_url=url, platform=platform, page_title=title)
        fingerprint = conversation_text_fingerprint(conversation_text)
        ignored = ignored_message_fingerprint(fingerprint_key, fingerprint)
        if ignored:
            note = "这条对话已被手动忽略；dry-run 未保存聊天全文、未调用模型。"
            patrol_run_id = write_observation_patrol_run(
                status="已忽略",
                note=note,
                trigger_type=trigger_type,
                scope=scope,
                platform=platform,
                source_url=url,
                page_title=title,
                job_id=job_id,
                fingerprint_key=fingerprint_key,
                fingerprint=fingerprint,
                skipped_count=1,
                decision={
                    "executor": executor,
                    "dry_run": True,
                    "ignored": True,
                    "ignore_reason": str(ignored.get("reason") or ""),
                    "fingerprint_key": fingerprint_key,
                    "text_length": len(conversation_text),
                    "ignored_line_count": len(prepared_text["ignored_lines"]),
                    "text_scope": text_scope,
                },
            )
            return {
                **base_result,
                "ok": True,
                "skipped": True,
                "status": "已忽略",
                "reason": note,
                "job_id": job_id,
                "patrol_run_id": patrol_run_id,
                "text_length": len(conversation_text),
                "skipped_count": 1,
            }
        duplicate_fingerprint = has_message_patrol_fingerprint(fingerprint_key, fingerprint)
        preview_decision: dict[str, Any] = {}
        if duplicate or duplicate_fingerprint:
            status = "无新内容"
            note = "清洗后的对话内容与上一条记录一致；dry-run 未保存聊天全文、未调用模型。"
            new_count = 0
            skipped_count = 1
            capture_id = int(duplicate["id"]) if duplicate else None
        else:
            preview_decision = enforce_reply_gate(classify_conversation_for_patrol_preview(conversation_text, job), job, job_id)
            if preview_decision.get("message_type") == "无需回复":
                status = "无需回复"
                note = "回复闸门判断无需回复；dry-run 未保存聊天全文、未调用模型。"
                new_count = 0
                skipped_count = 1
                capture_id = None
            else:
                status = "观察完成"
                note = "发现可能的新对话内容；dry-run 未保存聊天全文、未调用模型。"
                new_count = 1
                skipped_count = 0
                capture_id = None
        save_message_patrol_fingerprint(fingerprint_key, fingerprint)
        patrol_run_id = write_observation_patrol_run(
            status=status,
            note=note,
            trigger_type=trigger_type,
            scope=scope,
            platform=platform,
            source_url=url,
            page_title=title,
            job_id=job_id,
            capture_id=capture_id,
            fingerprint_key=fingerprint_key,
            fingerprint=fingerprint,
            new_count=new_count,
            skipped_count=skipped_count,
            decision={
                "executor": executor,
                "dry_run": True,
                "matched_capture_id": capture_id,
                "matched_by": "job_id" if job_id else "page_fingerprint",
                "duplicate_fingerprint": duplicate_fingerprint,
                "fingerprint_key": fingerprint_key,
                "text_length": len(conversation_text),
                "ignored_line_count": len(prepared_text["ignored_lines"]),
                "text_scope": text_scope,
                "reply_gate": preview_decision.get("reply_gate", ""),
                "reply_gate_reasons": preview_decision.get("reply_gate_reasons", []),
            },
        )
        return {
            **base_result,
            "ok": True,
            "skipped": bool(skipped_count),
            "status": status,
            "reason": note,
            "job_id": job_id,
            "existing_capture_id": capture_id,
            "patrol_run_id": patrol_run_id,
            "text_length": len(conversation_text),
            "new_count": new_count,
            "skipped_count": skipped_count,
        }

    capture_payload = {
        "capture_type": "conversation",
        "url": url,
        "title": title,
        "platform": platform,
        "text": raw_text,
        "text_scope": text_scope,
        "patrol_trigger": trigger_type,
        "patrol_scope": scope,
    }
    capture_result = create_conversation_from_extension(capture_payload)
    if isinstance(capture_result, JSONResponse):
        response_payload = json_response_payload(capture_result)
        if capture_result.status_code >= 400:
            return observation_error_result(index, str(response_payload.get("error") or "对话处理失败。"))
        capture_result = response_payload

    if not isinstance(capture_result, dict):
        return observation_error_result(index, "对话处理结果格式异常。")

    skipped = bool(capture_result.get("skipped"))
    status = str(capture_result.get("message_type") or ("已跳过" if skipped else "已处理"))
    return {
        **base_result,
        **capture_result,
        "index": index,
        "platform": platform,
        "source_url": url,
        "page_title": title,
        "status": status,
        "new_count": 0 if skipped else 1,
        "skipped_count": 1 if skipped else 0,
        "error_count": 0,
    }


def summarize_observation_results(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "checked_count": sum(int(item.get("checked_count") or 0) for item in results),
        "new_count": sum(int(item.get("new_count") or 0) for item in results),
        "skipped_count": sum(int(item.get("skipped_count") or 0) for item in results),
        "error_count": sum(int(item.get("error_count") or 0) for item in results),
    }


def create_job_from_extension(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    raw_url = payload_text(payload, "url", 1000)
    if not raw_url:
        return api_error("缺少当前页面 URL。")
    try:
        url = ensure_public_http_url(raw_url)
    except ValueError as exc:
        return api_error(str(exc))

    jd_text = extension_page_text({**payload, "url": url})
    if len(jd_text) < 40:
        return api_error("当前页面文本太短，无法作为岗位详情导入。")

    title = payload_text(payload, "title", 300)
    platform = extension_platform(url, payload_text(payload, "platform", 80))
    with connect() as conn:
        default_resume = conn.execute("SELECT id FROM resume_versions WHERE is_default = 1 ORDER BY id LIMIT 1").fetchone()
        resume_id = int(default_resume["id"]) if default_resume else None
        existing_job = find_existing_job_by_source_url(conn, url)
        if existing_job:
            job_id = int(existing_job["id"])
            refresh_job_record(
                conn,
                job_id,
                jd_text=jd_text,
                resume_id=resume_id,
                platform=platform,
                source_url=url,
                title=title,
                search_depth="auto",
                generate_messages=False,
            )
            event_type = "浏览器扩展刷新"
            event_content = f"从浏览器扩展采集当前岗位详情并刷新已有岗位：{url}"
        else:
            job_id, _analysis = create_job_record(
                conn,
                jd_text=jd_text,
                resume_id=resume_id,
                platform=platform,
                source_url=url,
                title=title,
                search_depth="auto",
                generate_messages=False,
            )
            event_type = "浏览器扩展采集"
            event_content = f"从浏览器扩展采集当前岗位页面：{url}"
        linked_count = link_candidates_to_job(conn, url, job_id)
        conn.execute(
            "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, event_content, utc_now()),
        )

    return {
        "ok": True,
        "capture_type": "job",
        "job_id": job_id,
        "updated": bool(existing_job),
        "linked_candidate_count": linked_count,
        "redirect_url": f"/jobs/{job_id}",
    }


def create_search_from_extension(payload: dict[str, Any]) -> dict[str, Any] | JSONResponse:
    raw_url = payload_text(payload, "url", 1000)
    if not raw_url:
        return api_error("缺少当前页面 URL。")
    try:
        url = ensure_public_http_url(raw_url)
    except ValueError as exc:
        return api_error(str(exc))

    title = payload_text(payload, "title", 300)
    platform = extension_platform(url, payload_text(payload, "platform", 80))
    keyword = payload_text(payload, "keyword", 80) or extension_keyword(url, title)
    city = payload_text(payload, "city", 80)
    sources = extension_candidate_sources(payload)
    candidates = extract_candidates_from_anchors(sources, platform, city, url, limit=30)
    note = f"浏览器扩展基于当前页可见文本、{len(extension_anchors(payload))} 个链接和 {len(extension_cards(payload))} 个候选卡片采集搜索结果页。"
    if not candidates:
        note += " 没有识别到候选岗位，可尝试停留在岗位列表区域后重试。"
    result = SearchResult(
        platform=platform,
        keyword=keyword,
        city=city,
        search_url=url,
        browser_channel="extension",
        candidates=candidates,
        note=note,
    )
    run_id = save_search_result(result)
    return {
        "ok": True,
        "capture_type": "search",
        "search_run_id": run_id,
        "candidate_count": len(candidates),
        "source_count": len(sources),
        "redirect_url": f"/searches/{run_id}",
    }


def candidate_snapshot_jd(candidate: dict[str, Any]) -> str:
    title = str(candidate.get("title") or "").strip()
    company = str(candidate.get("company") or "").strip()
    city = str(candidate.get("city") or "").strip()
    source_url = str(candidate.get("source_url") or "").strip()
    summary = normalize_visible_text(str(candidate.get("summary") or ""))
    parts = [
        "以下内容来自搜索结果页可见文本，详情页暂未抓取成功，需要后续补充完整 JD。",
        f"岗位名称：{title}" if title else "",
        f"公司名称：{company}" if company else "",
        f"城市：{city}" if city else "",
        f"岗位链接：{source_url}" if source_url else "",
        f"搜索结果摘要：\n{summary}" if summary else "",
    ]
    return "\n\n".join(part for part in parts if part).strip()[:EXTENSION_TEXT_LIMIT]


def fetch_browser_channel(value: str) -> str:
    return "msedge" if value == "extension" else value or "msedge"


def normalize_extension_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        for key in ("result", "payload", "data"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                return nested
        return payload
    if isinstance(payload, list):
        for item in payload:
            normalized = normalize_extension_payload(item)
            if normalized:
                return normalized
    return None


@app.post("/api/message-patrol/observations")
async def message_patrol_observations(request: Request) -> Any:
    try:
        raw_payload = await request.json()
    except Exception:
        return api_error("请求不是有效 JSON。")

    parsed, error_message = normalize_message_patrol_observation_request(raw_payload)
    if not parsed:
        return api_error(error_message)

    results = [
        process_message_patrol_observation(
            observation,
            index=index,
            dry_run=bool(parsed["dry_run"]),
            executor=str(parsed["executor"]),
            trigger_type=str(parsed["trigger_type"]),
            scope=str(parsed["scope"]),
        )
        for index, observation in enumerate(parsed["observations"])
    ]
    counts = summarize_observation_results(results)
    return {
        "ok": counts["error_count"] == 0,
        "dry_run": bool(parsed["dry_run"]),
        "executor": parsed["executor"],
        "trigger_type": parsed["trigger_type"],
        "scope": parsed["scope"],
        "truncated": bool(parsed["truncated"]),
        **counts,
        "results": results,
        "redirect_url": "/communications",
    }


@app.post("/api/extension/capture")
async def extension_capture(request: Request) -> Any:
    try:
        raw_payload = await request.json()
    except Exception:
        return api_error("请求不是有效 JSON。")
    payload = normalize_extension_payload(raw_payload)
    if payload is None:
        return api_error("请求体必须是 JSON 对象，或 Chrome 脚本返回对象数组。")

    capture_type = payload_text(payload, "capture_type", 30) or payload_text(payload, "type", 30)
    if capture_type == "job":
        return create_job_from_extension(payload)
    if capture_type == "search":
        return create_search_from_extension(payload)
    if capture_type == "conversation":
        return create_conversation_from_extension(payload)
    return api_error("capture_type 必须是 job、search 或 conversation。")


@app.get("/")
def dashboard(request: Request) -> Any:
    with connect() as conn:
        counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM job_postings GROUP BY status").fetchall()
        }
        recent_jobs = [
            parse_json_fields({key: row[key] for key in row.keys()})
            for row in conn.execute("SELECT * FROM job_postings ORDER BY created_at DESC LIMIT 6").fetchall()
        ]
        resumes = conn.execute("SELECT COUNT(*) AS count FROM resume_versions").fetchone()["count"]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"counts": counts, "recent_jobs": recent_jobs, "resumes": resumes, "token_stats": token_stats()},
    )


@app.get("/resumes")
def resumes_page(request: Request) -> Any:
    with connect() as conn:
        profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        resumes = conn.execute("SELECT * FROM resume_versions ORDER BY id").fetchall()
    profile_dict = {key: profile[key] for key in profile.keys()} if profile else {}
    projects = loads(profile_dict.get("projects_json"), []) if profile_dict else []
    return templates.TemplateResponse(
        request,
        "resumes.html",
        {
            "profile": profile_dict,
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "projects": projects if isinstance(projects, list) else [],
            "project_lines": format_project_lines(projects if isinstance(projects, list) else []),
            "loads": loads,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


def parse_project_lines(value: str) -> list[dict[str, Any]]:
    projects: list[dict[str, Any]] = []
    for line in (value or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        parts = [part.strip() for part in raw.split("|")]
        name = parts[0] if parts else ""
        url = parts[1] if len(parts) > 1 else ""
        highlights_raw = parts[2] if len(parts) > 2 else ""
        highlights = [item.strip() for item in re.split(r"[;；]", highlights_raw) if item.strip()]
        if not name and url:
            name = url.rstrip("/").split("/")[-1]
        if not name:
            continue
        project: dict[str, Any] = {"name": name, "url": url, "highlights": highlights}
        if repo_key(url):
            project["source"] = "github"
        projects.append(project)
    return projects[:20]


def format_project_lines(projects: list[dict[str, Any]]) -> str:
    lines = []
    for project in projects:
        name = str(project.get("name") or "").strip()
        url = str(project.get("url") or "").strip()
        highlights = project.get("highlights") or []
        if not isinstance(highlights, list):
            highlights = []
        highlight_text = "；".join(str(item).strip() for item in highlights if str(item).strip())
        if name or url or highlight_text:
            lines.append(" | ".join([name, url, highlight_text]).rstrip(" |"))
    return "\n".join(lines)


@app.post("/resumes/profile")
async def update_profile(request: Request) -> RedirectResponse:
    form = await request.form()
    now = utc_now()
    with connect() as conn:
        profile = conn.execute("SELECT id FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        values = (
            str(form.get("name") or ""),
            str(form.get("education") or ""),
            str(form.get("github_url") or ""),
            str(form.get("demo_url") or ""),
            dumps([item.strip() for item in str(form.get("target_roles") or "").splitlines() if item.strip()]),
            dumps([item.strip() for item in str(form.get("skills") or "").splitlines() if item.strip()]),
            dumps(parse_project_lines(str(form.get("projects") or ""))),
            now,
        )
        if profile:
            conn.execute(
                """
                UPDATE candidate_profile
                SET name = ?, education = ?, github_url = ?, demo_url = ?,
                    target_roles = ?, skills_json = ?, projects_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, profile["id"]),
            )
    return redirect_with_notice("/resumes", "候选人画像已保存。", "success")


def refresh_github_projects_for_profile(conn: Any, profile_id: int) -> dict[str, Any]:
    profile = conn.execute("SELECT * FROM candidate_profile WHERE id = ?", (profile_id,)).fetchone()
    if not profile:
        return {"refreshed_count": 0, "error_count": 1, "errors": ["没有找到候选人画像。"]}
    existing_projects = loads(profile["projects_json"], []) or []
    if not isinstance(existing_projects, list):
        existing_projects = []
    urls = github_repo_urls_from_projects(existing_projects)
    for candidate in [str(profile["demo_url"] or ""), str(profile["github_url"] or "")]:
        if repo_key(candidate):
            urls.append(normalize_repo_url(candidate))
    urls = dedupe_urls(urls)
    if not urls:
        log_agent_action(
            conn,
            action_type="github_project_refresh",
            status="无仓库",
            summary="没有找到可刷新的具体 GitHub 仓库链接。",
            decision={"model_called": False, "repo_count": 0},
        )
        return {
            "refreshed_count": 0,
            "error_count": 0,
            "errors": [],
            "repo_count": 0,
            "message": "请先在项目事实库或作品链接里填写具体 GitHub 仓库链接。",
        }

    refreshed: list[dict[str, Any]] = []
    errors: list[str] = []
    for url in urls[:8]:
        try:
            snapshot = fetch_github_repo_snapshot(url)
            refreshed.append(project_from_snapshot(snapshot))
        except GitHubProjectError as exc:
            errors.append(f"{url}: {str(exc)[:160]}")
    merged = merge_project_facts(existing_projects, refreshed)
    now = utc_now()
    conn.execute(
        "UPDATE candidate_profile SET projects_json = ?, updated_at = ? WHERE id = ?",
        (dumps(merged), now, profile_id),
    )
    status = "已刷新" if refreshed else "刷新失败"
    summary = f"GitHub 项目刷新：成功 {len(refreshed)} 个，失败 {len(errors)} 个。"
    log_agent_action(
        conn,
        action_type="github_project_refresh",
        status=status,
        summary=summary,
        decision={
            "model_called": False,
            "repo_count": len(urls),
            "refreshed_count": len(refreshed),
            "error_count": len(errors),
            "errors": errors[:5],
        },
    )
    return {
        "refreshed_count": len(refreshed),
        "error_count": len(errors),
        "errors": errors,
        "repo_count": len(urls),
        "message": summary,
    }


@app.post("/resumes/github-refresh")
async def refresh_github_projects(request: Request) -> RedirectResponse:
    with connect() as conn:
        profile = conn.execute("SELECT id FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        if not profile:
            return redirect_with_notice("/resumes", "没有找到候选人画像。", "error")
        result = refresh_github_projects_for_profile(conn, int(profile["id"]))
    if result.get("refreshed_count"):
        notice_type = "warning" if result.get("error_count") else "success"
    else:
        notice_type = "error" if result.get("error_count") else "info"
    return redirect_with_notice("/resumes", str(result.get("message") or "GitHub 项目刷新完成。"), notice_type)


@app.post("/resumes/{resume_id}")
async def update_resume(resume_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    file_path = str(form.get("file_path") or "").strip()
    parsed_text = str(form.get("parsed_text") or "")
    if file_path and not parsed_text:
        parsed_text = read_resume_text(file_path)
    with connect() as conn:
        conn.execute(
            """
            UPDATE resume_versions
            SET name = ?, target_role = ?, file_path = ?, parsed_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                str(form.get("name") or ""),
                str(form.get("target_role") or ""),
                file_path,
                parsed_text,
                utc_now(),
                resume_id,
            ),
        )
    return redirect("/resumes")


@app.get("/jobs")
def jobs_page(request: Request, status: str = "") -> Any:
    params: tuple[Any, ...] = ()
    query = "SELECT * FROM job_postings"
    if status:
        query += " WHERE status = ?"
        params = (status,)
    query += " ORDER BY created_at DESC"
    with connect() as conn:
        jobs = [parse_json_fields({key: row[key] for key in row.keys()}) for row in conn.execute(query, params).fetchall()]
        recommendation_counts = {
            row["recommendation"] or "未分析": row["count"]
            for row in conn.execute(
                """
                SELECT recommendation, COUNT(*) AS count
                FROM job_postings
                GROUP BY recommendation
                """
            ).fetchall()
        }
        status_counts = {
            row["status"]: row["count"]
            for row in conn.execute("SELECT status, COUNT(*) AS count FROM job_postings GROUP BY status").fetchall()
        }
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": jobs,
            "status_filter": status,
            "recommendation_counts": recommendation_counts,
            "status_counts": status_counts,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.get("/applications")
def application_preparations_page(request: Request) -> Any:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT p.*, j.title AS job_title, j.company AS company, j.city AS city,
                   j.platform AS platform, j.match_score AS match_score,
                   j.risk_level AS risk_level, j.status AS job_status,
                   r.name AS resume_name
            FROM application_preparations p
            JOIN job_postings j ON j.id = p.job_id
            LEFT JOIN resume_versions r ON r.id = p.resume_id
            ORDER BY CASE p.status WHEN '待确认' THEN 0 WHEN '已确认' THEN 1 ELSE 2 END,
                     p.updated_at DESC, p.id DESC
            """
        ).fetchall()
        resumes = conn.execute("SELECT id, name, target_role FROM resume_versions ORDER BY is_default DESC, id").fetchall()
        status_counts = {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM application_preparations GROUP BY status"
            ).fetchall()
        }
    return templates.TemplateResponse(
        request,
        "application_preparations.html",
        {
            "preparations": [{key: row[key] for key in row.keys()} for row in rows],
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "status_counts": status_counts,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/applications/refresh")
async def refresh_application_preparations(request: Request) -> RedirectResponse:
    with connect() as conn:
        jobs = conn.execute(
            """
            SELECT id FROM job_postings
            WHERE recommendation = '必投' AND risk_level = '低' AND status IN ('待确认', '待投递')
            ORDER BY match_score DESC, id DESC
            """
        ).fetchall()
        results = [
            ensure_application_preparation_for_job(conn, int(row["id"]), trigger_type="batch_refresh")
            for row in jobs
        ]
    created_count = sum(1 for item in results if item.get("created"))
    existing_count = sum(1 for item in results if item.get("preparation_id") and not item.get("created"))
    return redirect_with_notice(
        "/applications",
        f"已检查 {len(results)} 条必投低风险岗位：新增 {created_count} 条投递准备，已有 {existing_count} 条。",
        "success" if created_count else "info",
    )


@app.post("/jobs/{job_id}/application-preparation")
async def create_application_preparation(job_id: int, request: Request) -> RedirectResponse:
    with connect() as conn:
        result = ensure_application_preparation_for_job(conn, job_id, trigger_type="job_detail")
    if result.get("created"):
        return redirect_with_notice(f"/jobs/{job_id}", "已加入投递准备，请核对简历版本后再确认。", "success")
    if result.get("preparation_id"):
        return redirect_with_notice(f"/jobs/{job_id}", "该岗位已有投递准备。", "info")
    return redirect_with_notice(f"/jobs/{job_id}", str(result.get("reason") or "暂时不能生成投递准备。"), "error")


@app.post("/applications/{preparation_id}")
async def update_application_preparation(preparation_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    action = str(form.get("action") or "save").strip()
    if action not in {"save", "confirm", "skip"}:
        return redirect_with_notice("/applications", "投递准备操作无效。", "error")
    resume_id_raw = str(form.get("resume_id") or "").strip()
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    user_note = str(form.get("user_note") or "").strip()[:1500]
    return_to_job = str(form.get("return_to") or "") == "job"
    now = utc_now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM application_preparations WHERE id = ?", (preparation_id,)).fetchone()
        if not row:
            return redirect_with_notice("/applications", "没有找到这条投递准备。", "error")
        job_id = int(row["job_id"])
        target_path = f"/jobs/{job_id}" if return_to_job else "/applications"
        if resume_id:
            valid_resume = conn.execute("SELECT id FROM resume_versions WHERE id = ?", (resume_id,)).fetchone()
            if not valid_resume:
                return redirect_with_notice(target_path, "选择的简历版本不存在。", "error")
        if action == "confirm" and not resume_id:
            return redirect_with_notice(target_path, "确认待投递前，请先选择一个简历版本。", "error")
        if action == "skip" and row["status"] != "待确认":
            return redirect_with_notice(target_path, "只有待确认的投递准备可以跳过。", "error")

        status = {"save": row["status"], "confirm": "已确认", "skip": "已跳过"}[action]
        resume_reason = row["resume_reason"] or ""
        if resume_id != row["resume_id"]:
            resume_reason = "由用户在投递准备中手动选择。"
        conn.execute(
            """
            UPDATE application_preparations
            SET resume_id = ?, resume_reason = ?, user_note = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (resume_id, resume_reason, user_note, status, now, preparation_id),
        )
        event_type = {"save": "投递准备更新", "confirm": "投递准备确认", "skip": "投递准备跳过"}[action]
        event_content = {"save": "已更新投递准备。", "confirm": "已确认进入待投递；尚未在招聘平台执行投递。", "skip": "已跳过本次投递准备。"}[action]
        if action == "confirm":
            job = conn.execute("SELECT status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
            if job and job["status"] in {"待确认", "待投递"}:
                conn.execute("UPDATE job_postings SET status = ?, updated_at = ? WHERE id = ?", ("待投递", now, job_id))
        conn.execute(
            "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
            (job_id, event_type, event_content, now),
        )
        log_agent_action(
            conn,
            action_type="application_preparation",
            status=status,
            summary=event_content,
            job_id=job_id,
            decision={
                "preparation_id": preparation_id,
                "action": action,
                "resume_id": resume_id,
                "model_called": False,
            },
        )
    message = {"save": "投递准备已保存。", "confirm": "已进入待投递，仍需你在平台上手动执行。", "skip": "已跳过本次投递准备。"}[action]
    return redirect_with_notice(target_path, message, "success")


@app.post("/applications/{preparation_id}/open-browser")
async def open_application_browser(preparation_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/applications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/applications"
    with connect() as conn:
        item = application_browser_item(conn, preparation_id)
    if not item:
        return redirect_with_notice(return_to, "没有找到这条投递准备。", "error")
    plan = build_application_browser_plan(item)
    if plan.get("browser_action") == "blocked":
        return redirect_with_notice(return_to, str(plan.get("reason") or "当前不能打开投递页面。"), "error")
    try:
        target_url = await run_in_threadpool(open_message_patrol_browser, str(item.get("source_url") or ""))
    except ValueError as exc:
        return redirect_with_notice(return_to, f"打开受控 Edge 失败：{str(exc)[:180]}", "error")
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="application_browser_open",
            status="已打开",
            summary="已在受控 Edge 打开岗位页，尚未执行任何填写或投递。",
            job_id=int(item["job_id"]),
            decision={
                "preparation_id": preparation_id,
                "source_url_host": (urlparse(target_url).hostname or "").lower(),
                "browser_filled": False,
                "browser_clicked": False,
                "resume_uploaded": False,
                "model_called": False,
            },
        )
    return redirect_with_notice(return_to, "已在受控 Edge 打开岗位页。登录并确认页面后，可执行只读演练。", "success")


@app.post("/applications/{preparation_id}/browser-probe")
async def probe_application_browser(preparation_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/applications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/applications"
    result = await run_in_threadpool(run_application_browser_probe, preparation_id)
    status = str(result.get("status") or "")
    notice_type = "success" if status == "探测完成" else "error" if status in {"浏览器未连接", "未找到投递准备"} else "info"
    return redirect_with_notice(return_to, f"投递页面只读演练：{result.get('note') or status}", notice_type)


@app.get("/communications")
def communications_page(request: Request) -> Any:
    with connect() as conn:
        drafts = conn.execute(
            """
            SELECT d.*, j.title AS job_title, j.company AS company, j.status AS job_status
            FROM message_drafts d
            LEFT JOIN job_postings j ON j.id = d.job_id
            ORDER BY d.created_at DESC
            LIMIT 100
            """
        ).fetchall()
        captures = conn.execute(
            """
            SELECT c.*, j.title AS job_title, j.company AS company
            FROM conversation_captures c
            LEFT JOIN job_postings j ON j.id = c.job_id
            ORDER BY c.created_at DESC
            LIMIT 50
            """
        ).fetchall()
        draft_counts = conn.execute("SELECT status, COUNT(*) AS count FROM message_drafts GROUP BY status").fetchall()
        draft_type_counts = conn.execute("SELECT draft_type, COUNT(*) AS count FROM message_drafts GROUP BY draft_type").fetchall()
        feedback_counts = conn.execute(
            """
            SELECT feedback_status AS status, COUNT(*) AS count
            FROM conversation_captures
            WHERE feedback_status != ''
            GROUP BY feedback_status
            """
        ).fetchall()
        action_logs = conn.execute(
            """
            SELECT l.*, j.title AS job_title, j.company AS company
            FROM agent_action_logs l
            LEFT JOIN job_postings j ON j.id = l.job_id
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT 80
            """
        ).fetchall()
        patrol_runs = conn.execute(
            """
            SELECT p.*, j.title AS job_title, j.company AS company
            FROM message_patrol_runs p
            LEFT JOIN job_postings j ON j.id = p.job_id
            ORDER BY p.created_at DESC, p.id DESC
            LIMIT 40
            """
        ).fetchall()
        executor_status = communication_executor_page_status(conn)
    return templates.TemplateResponse(
        request,
        "communications.html",
        {
            "drafts": [{key: row[key] for key in row.keys()} for row in drafts],
            "captures": [{key: row[key] for key in row.keys()} for row in captures],
            "draft_counts": {row["status"]: row["count"] for row in draft_counts},
            "draft_type_counts": {row["draft_type"]: row["count"] for row in draft_type_counts},
            "feedback_counts": {row["status"]: row["count"] for row in feedback_counts},
            "message_types": ["岗位沟通", "面试邀请", "需要我处理", "无关内容", "无需回复"],
            "communication_policy": communication_policy(),
            "automation_control": automation_control(),
            "message_patrol_policy": message_patrol_policy(),
            "executor_status": executor_status,
            "communication_mode_label": communication_mode_label,
            "action_logs": [{key: row[key] for key in row.keys()} for row in action_logs],
            "patrol_runs": [{key: row[key] for key in row.keys()} for row in patrol_runs],
            "action_type_label": action_type_label,
            "loads": loads,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/message-drafts/{draft_id}")
async def update_message_draft(draft_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    status = str(form.get("status") or "待确认").strip()
    message = str(form.get("message") or "").strip()
    allowed = {"待确认", "已发送", "已驳回", "需要我处理"}
    if status not in allowed:
        return redirect_with_notice("/communications", "草稿状态无效。", "error")
    with connect() as conn:
        row = conn.execute("SELECT * FROM message_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            return redirect_with_notice("/communications", "没有找到草稿。", "error")
        old_status = str(row["status"] or "")
        now = utc_now()
        send_gate: dict[str, Any] | None = None
        if status == "已发送":
            send_gate = evaluate_draft_send_gate(conn, row, message)
            if not send_gate["allowed"]:
                conn.execute(
                    "UPDATE message_drafts SET message = ?, updated_at = ? WHERE id = ?",
                    (message, now, draft_id),
                )
                summary = "发送闸门拦截：" + "；".join(send_gate["reasons"])
                log_agent_action(
                    conn,
                    action_type="draft_send_gate",
                    status="已拦截",
                    summary=summary[:500],
                    platform=str(row["platform"] or ""),
                    job_id=int(row["job_id"]) if row["job_id"] else None,
                    capture_id=int(row["capture_id"]) if row["capture_id"] else None,
                    draft_id=draft_id,
                    decision=send_gate,
                )
                return redirect_with_notice("/communications", summary[:180], "error")
        conn.execute(
            "UPDATE message_drafts SET status = ?, message = ?, updated_at = ? WHERE id = ?",
            (status, message, now, draft_id),
        )
        if row["job_id"] and status == "已发送":
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (row["job_id"], "沟通草稿已发送", message[:500], now),
            )
        log_agent_action(
            conn,
            action_type="draft_status_update",
            status=status,
            summary=f"草稿状态：{old_status or '-'} -> {status}",
            platform=str(row["platform"] or ""),
            job_id=int(row["job_id"]) if row["job_id"] else None,
            capture_id=int(row["capture_id"]) if row["capture_id"] else None,
            draft_id=draft_id,
            decision={
                "old_status": old_status,
                "new_status": status,
                "draft_type": row["draft_type"],
                "communication_mode": row["communication_mode"],
                "followup_index": row["followup_index"],
                "followup_limit": row["followup_limit"],
                "message_length": len(message),
                "send_gate": send_gate or {"checked": False},
            },
        )
    return redirect_with_notice("/communications", "草稿已更新。", "success")


@app.post("/communication-executor/demo-draft")
async def communication_demo_draft_route(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    with connect() as conn:
        result = create_demo_communication_draft(conn)
    if result["created"]:
        message = "已创建本地演练草稿，可直接执行 dry-run。"
    else:
        message = "已有本地演练草稿，可直接执行 dry-run。"
    return redirect_with_notice(return_to, message, "success")


@app.post("/communication-executor/dry-run")
async def communication_executor_dry_run_route(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    plan = await run_in_threadpool(run_communication_executor_dry_run, "manual")
    notice_type = "success" if plan["status"] in {"演练完成", "无候选"} else "info"
    return redirect_with_notice(return_to, f"自动回复 dry-run：{plan['note']}", notice_type)


@app.post("/communication-executor/browser-dry-run")
async def communication_browser_dry_run_route(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    plan = await run_in_threadpool(run_communication_browser_dry_run, "manual_browser")
    notice_type = "success" if plan["status"] in {"映射完成", "无候选"} else "info"
    return redirect_with_notice(return_to, f"浏览器发送 dry-run：{plan['note']}", notice_type)


@app.post("/communication-executor/browser-probe-dry-run")
async def communication_browser_probe_dry_run_route(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    plan = await run_in_threadpool(run_communication_browser_probe_dry_run, "manual_browser")
    notice_type = "success" if plan["status"] in {"探测完成", "无候选"} else "info"
    if plan["status"] == "浏览器未连接":
        notice_type = "error"
    return redirect_with_notice(return_to, f"浏览器页面探测 dry-run：{plan['note']}", notice_type)


@app.post("/conversation-captures/{capture_id}/feedback")
async def update_conversation_feedback(capture_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    feedback_status = str(form.get("feedback_status") or "").strip()
    expected_message_type = str(form.get("expected_message_type") or "").strip()
    feedback_note = str(form.get("feedback_note") or "").strip()[:1000]
    allowed_statuses = {"", "正确", "误判", "待观察"}
    allowed_types = {"", "岗位沟通", "面试邀请", "需要我处理", "无关内容", "无需回复"}
    if feedback_status not in allowed_statuses:
        return redirect_with_notice("/communications", "反馈状态无效。", "error")
    if expected_message_type not in allowed_types:
        return redirect_with_notice("/communications", "期望分类无效。", "error")

    now = utc_now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM conversation_captures WHERE id = ?", (capture_id,)).fetchone()
        if not row:
            return redirect_with_notice("/communications", "没有找到这条对话采集。", "error")
        conn.execute(
            """
            UPDATE conversation_captures
            SET feedback_status = ?, expected_message_type = ?, feedback_note = ?, feedback_updated_at = ?
            WHERE id = ?
            """,
            (feedback_status, expected_message_type, feedback_note, now, capture_id),
        )
        if row["job_id"] and feedback_status:
            event_content = f"分类反馈：{feedback_status}"
            if expected_message_type:
                event_content += f"；期望分类：{expected_message_type}"
            if feedback_note:
                event_content += f"；备注：{feedback_note}"
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (row["job_id"], "对话分类反馈", event_content[:500], now),
            )
        if feedback_status:
            log_agent_action(
                conn,
                action_type="conversation_feedback",
                status=feedback_status,
                summary=f"对话分类反馈：{feedback_status}",
                platform=str(row["platform"] or ""),
                job_id=int(row["job_id"]) if row["job_id"] else None,
                capture_id=capture_id,
                decision={
                    "message_type": row["message_type"],
                    "expected_message_type": expected_message_type,
                    "feedback_note_length": len(feedback_note),
                },
            )
    return redirect_with_notice("/communications", "对话采集反馈已保存。", "success")


@app.post("/message-patrol/runs/{run_id}/ignore")
async def ignore_message_patrol_run(run_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    reason = str(form.get("reason") or "用户从巡检记录手动忽略相同消息。").strip()[:200]

    with connect() as conn:
        row = conn.execute("SELECT * FROM message_patrol_runs WHERE id = ?", (run_id,)).fetchone()
        run = {key: row[key] for key in row.keys()} if row else None
    if not run:
        return redirect_with_notice(return_to, "没有找到这条巡检记录。", "error")

    fingerprint_key = str(run.get("fingerprint_key") or "")
    fingerprint = str(run.get("fingerprint") or "")
    if not fingerprint_key or not fingerprint:
        return redirect_with_notice(return_to, "这条巡检记录没有可忽略的消息指纹，请重新巡检后再试。", "error")

    save_ignored_message_fingerprint(fingerprint_key, fingerprint, reason)
    now = utc_now()
    note = "这条消息已加入忽略列表；后续相同消息会直接标记为已忽略。"
    with connect() as conn:
        conn.execute(
            """
            UPDATE message_patrol_runs
            SET status = ?, new_count = 0, skipped_count = CASE WHEN skipped_count = 0 THEN 1 ELSE skipped_count END,
                note = ?, finished_at = ?
            WHERE id = ?
            """,
            ("已忽略", note, now, run_id),
        )
        log_agent_action(
            conn,
            action_type="message_patrol_ignore",
            status="已忽略",
            summary=note,
            platform=str(run.get("platform") or ""),
            job_id=int(run["job_id"]) if run.get("job_id") else None,
            capture_id=int(run["capture_id"]) if run.get("capture_id") else None,
            decision={
                "patrol_run_id": run_id,
                "fingerprint_key": fingerprint_key,
                "reason": reason,
                "text_saved": False,
            },
        )
    return redirect_with_notice(return_to, "已忽略相同消息。", "success")


@app.get("/jobs/new")
def new_job_page(request: Request) -> Any:
    with connect() as conn:
        resumes = conn.execute("SELECT * FROM resume_versions ORDER BY is_default DESC, id").fetchall()
    jd_client = client_for_task("jd_extract")
    return templates.TemplateResponse(
        request,
        "job_form.html",
        {
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "model_configured": bool(jd_client and jd_client.configured),
            "bulk_import_limit": BULK_IMPORT_LIMIT,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.get("/searches")
def searches_page(request: Request) -> Any:
    search_form = default_search_form()
    with connect() as conn:
        runs = conn.execute(
            """
            SELECT r.*,
                   COUNT(c.id) AS candidate_count,
                   SUM(CASE WHEN c.status = '已导入' THEN 1 ELSE 0 END) AS imported_count
            FROM job_search_runs r
            LEFT JOIN job_candidates c ON c.search_run_id = r.id
            GROUP BY r.id
            ORDER BY r.created_at DESC
            LIMIT 30
            """
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "searches.html",
        {
            "runs": [{key: row[key] for key in row.keys()} for row in runs],
            "search_form": search_form,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
            "browser_channel_label": browser_channel_label,
        },
    )


@app.post("/searches")
async def create_search_run(request: Request) -> RedirectResponse:
    form = await request.form()
    platform = str(form.get("platform") or "Boss 直聘").strip()
    keyword = str(form.get("keyword") or "").strip()
    city = str(form.get("city") or "").strip()
    browser_channel = str(form.get("browser_channel") or "msedge").strip()
    if not keyword:
        return redirect_with_notice("/searches", "请填写搜索关键词。", "error")
    save_last_manual_search(platform, keyword, city, browser_channel)

    try:
        result = await run_in_threadpool(search_jobs_with_browser, platform, keyword, city, browser_channel=browser_channel)
    except Exception as exc:
        run_id = save_search_failure(platform, keyword, city, browser_channel, f"自动采集失败：{str(exc)}")
        return redirect_with_notice(f"/searches/{run_id}", f"搜索采集失败：{str(exc)[:160]}", "error")

    run_id = save_search_result(result)
    return redirect_with_notice(f"/searches/{run_id}", f"已采集 {len(result.candidates)} 个候选岗位。", "success")


@app.post("/searches/open-manual")
async def open_manual_search(request: Request) -> RedirectResponse:
    form = await request.form()
    platform = str(form.get("platform") or "Boss 直聘").strip()
    keyword = str(form.get("keyword") or "").strip()
    city = str(form.get("city") or "").strip()
    if not keyword:
        return redirect_with_notice("/searches", "请填写搜索关键词。", "error")
    save_last_manual_search(platform, keyword, city, "msedge")
    try:
        search_url = await run_in_threadpool(open_manual_search_in_edge, platform, keyword, city)
    except Exception as exc:
        run_id = save_search_failure(platform, keyword, city, "msedge", f"打开 Edge 失败：{str(exc)}")
        return redirect_with_notice(f"/searches/{run_id}", f"打开 Edge 失败：{str(exc)[:160]}", "error")
    save_last_manual_search(platform, keyword, city, "msedge", search_url)
    return redirect_with_notice("/searches", f"已打开 Edge 搜索页：{search_url}。完成登录或筛选后，点击“采集当前 Edge 页面”。", "success")


@app.post("/searches/capture-current")
async def capture_current_search(request: Request) -> RedirectResponse:
    form = await request.form()
    saved = default_search_form()
    platform = search_form_value(form, "platform", saved["platform"]) or "Boss 直聘"
    keyword = search_form_value(form, "keyword", saved["keyword"])
    city = search_form_value(form, "city", saved["city"])
    browser_channel = search_form_value(form, "browser_channel", saved["browser_channel"]) or "msedge"
    if not keyword:
        return redirect_with_notice("/searches", "请填写搜索关键词。", "error")
    save_last_manual_search(platform, keyword, city, browser_channel, saved.get("search_url", ""))
    try:
        result = await run_in_threadpool(capture_current_search_page, platform, keyword, city, browser_channel=browser_channel)
    except Exception as exc:
        run_id = save_search_failure(platform, keyword, city, browser_channel, f"当前页面采集失败：{str(exc)}")
        return redirect_with_notice(f"/searches/{run_id}", f"当前页面采集失败：{str(exc)[:160]}", "error")
    run_id = save_search_result(result)
    return redirect_with_notice(f"/searches/{run_id}", f"已从当前 Edge 页面采集 {len(result.candidates)} 个候选岗位。", "success")


@app.get("/searches/{run_id}")
def search_detail(run_id: int, request: Request) -> Any:
    with connect() as conn:
        run = conn.execute("SELECT * FROM job_search_runs WHERE id = ?", (run_id,)).fetchone()
        candidates = conn.execute(
            "SELECT * FROM job_candidates WHERE search_run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        resumes = conn.execute("SELECT * FROM resume_versions ORDER BY is_default DESC, id").fetchall()
    if not run:
        return redirect("/searches")
    return templates.TemplateResponse(
        request,
        "search_detail.html",
        {
            "run": {key: run[key] for key in run.keys()},
            "candidates": [{key: row[key] for key in row.keys()} for row in candidates],
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "browser_channel_label": browser_channel_label,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/candidates/{candidate_id}/import")
async def import_candidate(candidate_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    fetch_mode = str(form.get("fetch_mode") or "auto")
    browser_channel = str(form.get("browser_channel") or "msedge")
    with connect() as conn:
        row = conn.execute(
            """
            SELECT c.*, r.browser_channel AS run_browser_channel
            FROM job_candidates c
            LEFT JOIN job_search_runs r ON r.id = c.search_run_id
            WHERE c.id = ?
            """,
            (candidate_id,),
        ).fetchone()
    if not row:
        return redirect_with_notice("/searches", "没有找到候选岗位。", "error")

    candidate = {key: row[key] for key in row.keys()}
    run_id = int(candidate["search_run_id"])
    channel = fetch_browser_channel(browser_channel or candidate.get("run_browser_channel") or "msedge")
    try:
        fetched = await run_in_threadpool(fetch_job_from_url, candidate["source_url"], fetch_mode=fetch_mode, browser_channel=channel)
        with connect() as conn:
            existing_job = find_existing_job_by_source_url(conn, fetched.final_url)
            if existing_job:
                job_id = int(existing_job["id"])
                refresh_job_record(
                    conn,
                    job_id,
                    jd_text=fetched.text,
                    resume_id=resume_id,
                    platform=candidate.get("platform") or infer_platform_from_url(fetched.final_url),
                    source_url=fetched.final_url,
                    title=fetched.title or candidate.get("title") or "",
                    company=candidate.get("company") or "",
                    city=candidate.get("city") or "",
                    search_depth=requested_depth,
                    generate_messages=candidate.get("run_browser_channel") != "extension",
                )
                event_type = "搜索候选刷新"
                event_content = f"从搜索候选 {candidate.get('source_url')} 刷新已有岗位详情。"
            else:
                job_id, _analysis = create_job_record(
                    conn,
                    jd_text=fetched.text,
                    resume_id=resume_id,
                    platform=candidate.get("platform") or infer_platform_from_url(fetched.final_url),
                    source_url=fetched.final_url,
                    title=fetched.title or candidate.get("title") or "",
                    company=candidate.get("company") or "",
                    city=candidate.get("city") or "",
                    search_depth=requested_depth,
                    generate_messages=candidate.get("run_browser_channel") != "extension",
                )
                event_type = "搜索候选导入"
                event_content = f"从搜索候选 {candidate.get('source_url')} 导入岗位详情。"
            now = utc_now()
            conn.execute(
                "UPDATE job_candidates SET job_id = ?, status = ?, error_message = '', updated_at = ? WHERE id = ?",
                (job_id, "已导入", now, candidate_id),
            )
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, event_type, event_content, now),
            )
        return redirect_with_notice(f"/jobs/{job_id}", f"已从候选岗位导入并通过{fetch_mode_label(fetched.fetch_mode)}完成分析。", "success")
    except Exception as exc:
        fallback_text = candidate_snapshot_jd(candidate)
        if len(fallback_text) >= 40:
            fetch_error = str(exc)[:300]
            with connect() as conn:
                existing_job = find_existing_job_by_source_url(conn, candidate.get("source_url") or "")
                if existing_job:
                    job_id = int(existing_job["id"])
                    refresh_job_record(
                        conn,
                        job_id,
                        jd_text=fallback_text,
                        resume_id=resume_id,
                        platform=candidate.get("platform") or infer_platform_from_url(candidate.get("source_url") or ""),
                        source_url=candidate.get("source_url") or "",
                        title=candidate.get("title") or "",
                        company=candidate.get("company") or "",
                        city=candidate.get("city") or "",
                        salary_text=extract_salary(candidate.get("summary") or ""),
                        search_depth=requested_depth,
                        generate_messages=False,
                    )
                else:
                    job_id, _analysis = create_job_record(
                        conn,
                        jd_text=fallback_text,
                        resume_id=resume_id,
                        platform=candidate.get("platform") or infer_platform_from_url(candidate.get("source_url") or ""),
                        source_url=candidate.get("source_url") or "",
                        title=candidate.get("title") or "",
                        company=candidate.get("company") or "",
                        city=candidate.get("city") or "",
                        salary_text=extract_salary(candidate.get("summary") or ""),
                        search_depth=requested_depth,
                        generate_messages=False,
                    )
                now = utc_now()
                conn.execute(
                    "UPDATE job_candidates SET job_id = ?, status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                    (job_id, "已导入", f"详情页抓取失败，已使用搜索结果摘要：{fetch_error}", now, candidate_id),
                )
                conn.execute(
                    "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                    (job_id, "搜索候选摘要导入", f"详情页抓取失败，已用搜索结果摘要创建岗位。原始错误：{fetch_error}", now),
                )
            return redirect_with_notice(f"/jobs/{job_id}", "详情页抓取失败，已先用搜索结果摘要创建待补充岗位。建议打开岗位页后再用扩展采集当前岗位刷新详情。", "warning")

        with connect() as conn:
            conn.execute(
                "UPDATE job_candidates SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                ("导入失败", str(exc)[:500], utc_now(), candidate_id),
            )
        return redirect_with_notice(f"/searches/{run_id}", f"候选岗位导入失败：{str(exc)[:160]}", "error")


@app.post("/jobs/analyze")
async def analyze_job(request: Request) -> RedirectResponse:
    form = await request.form()
    jd_text = str(form.get("jd_text") or "").strip()
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    with connect() as conn:
        job_id, _analysis = create_job_record(
            conn,
            jd_text=jd_text,
            resume_id=resume_id,
            platform=str(form.get("platform") or ""),
            source_url=str(form.get("source_url") or ""),
            title=str(form.get("title") or ""),
            company=str(form.get("company") or ""),
            city=str(form.get("city") or ""),
            salary_text=str(form.get("salary_text") or ""),
            search_depth=requested_depth,
        )
    return redirect(f"/jobs/{job_id}")


@app.post("/jobs/bulk-analyze")
async def bulk_analyze_jobs(request: Request) -> RedirectResponse:
    form = await request.form()
    jd_items = split_batch_jds(str(form.get("batch_jd_text") or ""))
    if not jd_items:
        return redirect_with_notice("/jobs/new", "没有找到可导入的 JD。", "error")

    truncated = len(jd_items) > BULK_IMPORT_LIMIT
    jd_items = jd_items[:BULK_IMPORT_LIMIT]
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    platform = str(form.get("platform") or "").strip()
    created: list[dict[str, Any]] = []

    with connect() as conn:
        for jd_text in jd_items:
            job_id, analysis = create_job_record(
                conn,
                jd_text=jd_text,
                resume_id=resume_id,
                platform=platform,
                search_depth=requested_depth,
            )
            created.append(
                {
                    "id": job_id,
                    "recommendation": analysis["scoring"].get("recommendation") or "未分析",
                }
            )

    counts = {label: sum(1 for item in created if item["recommendation"] == label) for label in ["必投", "可冲", "跳过"]}
    message = f"已导入 {len(created)} 条岗位：必投 {counts['必投']}，可冲 {counts['可冲']}，跳过 {counts['跳过']}。"
    if truncated:
        message += f" 单次最多处理 {BULK_IMPORT_LIMIT} 条，其余内容未导入。"
    return redirect_with_notice("/jobs", message, "success")


@app.post("/jobs/import-url")
async def import_job_url(request: Request) -> RedirectResponse:
    form = await request.form()
    source_url = str(form.get("source_url") or "").strip()
    resume_id_raw = str(form.get("selected_resume_id") or "")
    resume_id = int(resume_id_raw) if resume_id_raw.isdigit() else None
    requested_depth = str(form.get("search_depth") or "auto")
    fetch_mode = str(form.get("fetch_mode") or "auto")
    browser_channel = str(form.get("browser_channel") or "msedge")
    platform = str(form.get("platform") or "").strip()
    try:
        fetched = await run_in_threadpool(fetch_job_from_url, source_url, fetch_mode=fetch_mode, browser_channel=browser_channel)
    except Exception as exc:
        return redirect_with_notice("/jobs/new", f"链接导入失败：{str(exc)[:160]}", "error")

    with connect() as conn:
        job_id, _analysis = create_job_record(
            conn,
            jd_text=fetched.text,
            resume_id=resume_id,
            platform=platform or infer_platform_from_url(fetched.final_url),
            source_url=fetched.final_url,
            title=fetched.title,
            search_depth=requested_depth,
        )
        conn.execute(
            "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
            (
                job_id,
                "链接导入",
                f"使用{fetch_mode_label(fetched.fetch_mode)}从 {fetched.final_url} 抓取页面文本并生成岗位记录。{fetched.note}",
                utc_now(),
            ),
        )
    return redirect_with_notice(f"/jobs/{job_id}", f"已通过{fetch_mode_label(fetched.fetch_mode)}导入并完成分析。", "success")


def ensure_interview_preparation_for_job(
    conn: Any,
    job_id: int,
    *,
    trigger_type: str,
    source_text: str = "",
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT id FROM interview_preparations WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if existing:
        interview_id = int(existing["id"])
        log_agent_action(
            conn,
            action_type="interview_prep_auto_create",
            status="已存在",
            summary="该岗位已有面试准备记录，未重复创建。",
            job_id=job_id,
            decision={
                "trigger_type": trigger_type,
                "interview_id": interview_id,
                "created": False,
                "model_called": False,
            },
        )
        return {"created": False, "interview_id": interview_id}

    row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {"created": False, "interview_id": None, "error": "job_not_found"}

    job = parse_json_fields({key: row[key] for key in row.keys()})
    title = " - ".join(item for item in [job.get("company"), job.get("title")] if item) or f"岗位 {job_id}"
    prep_source = (source_text or "").strip()
    if not prep_source:
        prep_source = f"{title} 已标记为待面试，自动生成本地面试准备材料。"
    review = build_interview_review(job, prep_source)
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO interview_preparations (
            job_id, source_text, prep_plan_json, question_bank_json,
            review_markdown, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, prep_source, dumps(review["plan"]), dumps(review["questions"]), review["markdown"], now, now),
    )
    interview_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
        (job_id, "面试准备自动生成", f"{title} 已生成本地面试准备记录。", now),
    )
    log_agent_action(
        conn,
        action_type="interview_prep_auto_create",
        status="已创建",
        summary="岗位进入待面试阶段，已生成本地面试准备记录。",
        job_id=job_id,
        decision={
            "trigger_type": trigger_type,
            "interview_id": interview_id,
            "created": True,
            "model_called": False,
        },
    )
    return {"created": True, "interview_id": interview_id}


def interview_feedback_context(conn: Any, job_id: int | None, limit: int = 8) -> str:
    if not job_id:
        return ""
    rows = conn.execute(
        """
        SELECT feedback_type, question, issue_summary, improvement_plan
        FROM interview_feedback
        WHERE job_id = ? AND status = '待练习'
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (job_id, max(1, min(int(limit or 8), 20))),
    ).fetchall()
    if not rows:
        return ""
    lines = ["历史待练习薄弱点："]
    for row in rows:
        parts = [
            str(row["feedback_type"] or "面试问题"),
            str(row["question"] or "").strip(),
            str(row["issue_summary"] or "").strip(),
            str(row["improvement_plan"] or "").strip(),
        ]
        lines.append(" - " + "；".join(item for item in parts if item))
    return "\n".join(lines)


def practice_question_key(question: str) -> str:
    return re.sub(r"\s+", "", question or "").lower()


def interview_practice_questions(conn: Any, review: dict[str, Any]) -> list[dict[str, Any]]:
    """Combine unresolved feedback with the generated question bank, keeping weak points first."""
    review_id = int(review["id"])
    job_id = int(review["job_id"]) if review.get("job_id") else None
    if job_id:
        feedback_rows = conn.execute(
            """
            SELECT id, feedback_type, question, issue_summary
            FROM interview_feedback
            WHERE job_id = ? AND status = '待练习'
            ORDER BY updated_at DESC, id DESC
            LIMIT 20
            """,
            (job_id,),
        ).fetchall()
    else:
        feedback_rows = conn.execute(
            """
            SELECT id, feedback_type, question, issue_summary
            FROM interview_feedback
            WHERE interview_preparation_id = ? AND status = '待练习'
            ORDER BY updated_at DESC, id DESC
            LIMIT 20
            """,
            (review_id,),
        ).fetchall()

    questions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in feedback_rows:
        question = str(row["question"] or row["issue_summary"] or "").strip()
        key = practice_question_key(question)
        if not key or key in seen:
            continue
        seen.add(key)
        questions.append(
            {
                "question": question[:500],
                "source": "待练习薄弱点",
                "feedback_id": int(row["id"]),
                "feedback_type": str(row["feedback_type"] or "面试问题"),
            }
        )

    for item in loads(review.get("question_bank_json"), []):
        question = str(item or "").strip()
        key = practice_question_key(question)
        if not key or key in seen:
            continue
        seen.add(key)
        questions.append(
            {
                "question": question[:500],
                "source": "面试题库",
                "feedback_id": None,
                "feedback_type": "",
            }
        )
    return questions[:24]


def application_preparation_eligibility(job: dict[str, Any]) -> tuple[bool, str]:
    if job.get("recommendation") != "必投":
        return False, "该岗位当前不是“必投”建议，暂不自动进入投递准备。"
    if job.get("risk_level") != "低":
        return False, "该岗位风险不是“低”，需要人工确认后再决定是否投递。"
    if job.get("status") not in {"待确认", "待投递"}:
        return False, f"岗位当前状态为“{job.get('status') or '未设置'}”，不在待投递阶段。"
    return True, "岗位为必投且风险低，可进入投递准备。"


def resume_recommendation_for_job(conn: Any, job: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    rows = conn.execute("SELECT * FROM resume_versions ORDER BY is_default DESC, id").fetchall()
    resumes = [{key: row[key] for key in row.keys()} for row in rows]
    if not resumes:
        return None, "当前没有可用简历版本，请先在候选人画像页补充简历。"

    selected_resume_id = int(job["selected_resume_id"]) if job.get("selected_resume_id") else None
    for resume in resumes:
        if resume["id"] == selected_resume_id:
            return resume, "该版本是分析此 JD 时已选用的简历，优先沿用。"

    job_text = " ".join(str(job.get(key) or "") for key in ("title", "jd_text")).lower()
    focus_rules = [
        ("agent", ["agent", "智能体", "工具调用", "function calling"]),
        ("backend", ["后端", "backend", "fastapi", "接口", "api", "服务端"]),
        ("application", ["ai 应用", "大模型应用", "rag", "检索", "llm"]),
    ]
    job_focus = {name for name, keywords in focus_rules if any(keyword in job_text for keyword in keywords)}

    def score(resume: dict[str, Any]) -> int:
        resume_text = f"{resume.get('name') or ''} {resume.get('target_role') or ''}".lower()
        value = 4 if resume.get("is_default") else 0
        for name, keywords in focus_rules:
            if name in job_focus and any(keyword in resume_text for keyword in keywords):
                value += 40 if name == "agent" else 20
        return value

    recommended = max(resumes, key=score)
    if job_focus:
        focus_label = "、".join(
            {"agent": "Agent", "backend": "AI 后端", "application": "AI 应用"}[item]
            for item in ["agent", "backend", "application"]
            if item in job_focus
        )
        return recommended, f"根据岗位中的 {focus_label} 关键词，推荐目标方向最接近的版本。"
    return recommended, "岗位方向信号不足，暂推荐默认简历版本；请在确认前人工核对。"


def application_recommendation_reason(job: dict[str, Any]) -> str:
    scoring = (job.get("extracted") or {}).get("scoring") or {}
    matched_skills = [str(item) for item in scoring.get("matched_skills") or [] if str(item).strip()]
    missing_skills = [str(item) for item in scoring.get("missing_skills") or [] if str(item).strip()]
    parts = [f"匹配分 {job.get('match_score') or 0}，当前分析建议为“必投”，风险为“低”。"]
    if matched_skills:
        parts.append("已匹配：" + "、".join(matched_skills[:6]) + "。")
    if missing_skills:
        parts.append("投递前仍需确认：" + "、".join(missing_skills[:4]) + "。")
    return "".join(parts)


def ensure_application_preparation_for_job(conn: Any, job_id: int, *, trigger_type: str) -> dict[str, Any]:
    existing = conn.execute("SELECT id, status FROM application_preparations WHERE job_id = ?", (job_id,)).fetchone()
    if existing:
        return {"created": False, "preparation_id": int(existing["id"]), "status": existing["status"]}

    row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {"created": False, "preparation_id": None, "reason": "岗位不存在。"}
    job = parse_json_fields({key: row[key] for key in row.keys()})
    eligible, reason = application_preparation_eligibility(job)
    if not eligible:
        return {"created": False, "preparation_id": None, "reason": reason}

    resume, resume_reason = resume_recommendation_for_job(conn, job)
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO application_preparations (
            job_id, resume_id, recommendation_reason, resume_reason,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            int(resume["id"]) if resume else None,
            application_recommendation_reason(job),
            resume_reason,
            "待确认",
            now,
            now,
        ),
    )
    preparation_id = int(cursor.lastrowid)
    label = " - ".join(item for item in [job.get("company"), job.get("title")] if item) or f"岗位 {job_id}"
    conn.execute(
        "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
        (job_id, "投递准备生成", f"{label} 已进入本地投递准备，仍需人工确认。", now),
    )
    log_agent_action(
        conn,
        action_type="application_preparation",
        status="待确认",
        summary=f"已生成投递准备：{label}",
        job_id=job_id,
        decision={
            "trigger_type": trigger_type,
            "preparation_id": preparation_id,
            "resume_id": int(resume["id"]) if resume else None,
            "model_called": False,
        },
    )
    return {"created": True, "preparation_id": preparation_id, "resume_name": resume.get("name") if resume else ""}


def application_browser_item(conn: Any, preparation_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT p.id AS preparation_id, p.status AS preparation_status, p.resume_id,
               j.id AS job_id, j.platform, j.source_url, j.title AS job_title,
               j.company, j.status AS job_status, r.name AS resume_name
        FROM application_preparations p
        JOIN job_postings j ON j.id = p.job_id
        LEFT JOIN resume_versions r ON r.id = p.resume_id
        WHERE p.id = ?
        """,
        (preparation_id,),
    ).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def run_application_browser_probe(preparation_id: int) -> dict[str, Any]:
    with connect() as conn:
        item = application_browser_item(conn, preparation_id)
    if not item:
        return {"status": "未找到投递准备", "note": "没有找到这条投递准备。", "preparation_id": preparation_id}

    plan = build_application_browser_plan(item)
    try:
        result = probe_application_browser_plan(plan)
    except ValueError as exc:
        result = {
            **plan,
            "status": "浏览器未连接",
            "note": str(exc)[:500],
            "browser_probe_dry_run": True,
            "browser_connected": False,
            "probe_result": None,
        }
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="application_browser_probe",
            status=str(result.get("status") or "未知"),
            summary=str(result.get("note") or "投递页面只读演练完成。"),
            job_id=int(item["job_id"]),
            decision={
                "preparation_id": preparation_id,
                "browser_action": result.get("browser_action"),
                "browser_connected": bool(result.get("browser_connected")),
                "probe_status": (result.get("probe_result") or {}).get("probe_status"),
                "browser_filled": False,
                "browser_clicked": False,
                "resume_uploaded": False,
                "model_called": False,
            },
        )
    return result


@app.post("/jobs/bulk-status")
async def bulk_update_jobs(request: Request) -> RedirectResponse:
    form = await request.form()
    job_ids = [int(value) for value in form.getlist("job_ids") if str(value).isdigit()]
    status = str(form.get("status") or "").strip()
    allowed_statuses = {"待确认", "待投递", "已投递", "已沟通", "面试邀请", "待面试", "面试准备中", "已归档"}
    if not job_ids:
        return redirect_with_notice("/jobs", "请先选择岗位。", "error")
    if status not in allowed_statuses:
        return redirect_with_notice("/jobs", "状态无效，未更新。", "error")

    now = utc_now()
    placeholders = ",".join("?" for _ in job_ids)
    with connect() as conn:
        rows = conn.execute(
            f"SELECT id, title, company FROM job_postings WHERE id IN ({placeholders})",
            tuple(job_ids),
        ).fetchall()
        if not rows:
            return redirect_with_notice("/jobs", "没有找到可更新的岗位。", "error")
        valid_ids = [int(row["id"]) for row in rows]
        valid_placeholders = ",".join("?" for _ in valid_ids)
        conn.execute(
            f"UPDATE job_postings SET status = ?, updated_at = ? WHERE id IN ({valid_placeholders})",
            (status, now, *valid_ids),
        )
        for row in rows:
            title = " - ".join(item for item in [row["company"], row["title"]] if item) or f"岗位 {row['id']}"
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], "批量状态更新", f"{title} 已标记为 {status}。", now),
            )
        prep_results = []
        if status in INTERVIEW_PREP_TRIGGER_STATUSES:
            prep_results = [
                ensure_interview_preparation_for_job(conn, int(row["id"]), trigger_type="bulk_status_update")
                for row in rows
            ]
    message = f"已更新 {len(valid_ids)} 条岗位为「{status}」。"
    if status in INTERVIEW_PREP_TRIGGER_STATUSES:
        created_count = sum(1 for item in prep_results if item.get("created"))
        existing_count = sum(1 for item in prep_results if item.get("interview_id") and not item.get("created"))
        message += f" 已创建 {created_count} 条面试准备，已有 {existing_count} 条。"
    return redirect_with_notice("/jobs", message, "success")


@app.get("/jobs/{job_id}")
def job_detail(job_id: int, request: Request) -> Any:
    with connect() as conn:
        job = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        research = conn.execute("SELECT * FROM company_research WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
        events = conn.execute("SELECT * FROM application_events WHERE job_id = ? ORDER BY id DESC", (job_id,)).fetchall()
        interviews = conn.execute(
            "SELECT id, created_at FROM interview_preparations WHERE job_id = ? ORDER BY created_at DESC, id DESC",
            (job_id,),
        ).fetchall()
        preparation = conn.execute(
            """
            SELECT p.*, r.name AS resume_name
            FROM application_preparations p
            LEFT JOIN resume_versions r ON r.id = p.resume_id
            WHERE p.job_id = ?
            """,
            (job_id,),
        ).fetchone()
        resumes = conn.execute("SELECT id, name, target_role FROM resume_versions ORDER BY is_default DESC, id").fetchall()
    if not job:
        return redirect("/jobs")
    job_data = parse_json_fields({key: job[key] for key in job.keys()})
    application_eligible, application_block_reason = application_preparation_eligibility(job_data)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job_data,
            "research": [{key: row[key] for key in row.keys()} for row in research],
            "events": [{key: row[key] for key in row.keys()} for row in events],
            "interviews": [{key: row[key] for key in row.keys()} for row in interviews],
            "application_preparation": {key: preparation[key] for key in preparation.keys()} if preparation else None,
            "application_eligible": application_eligible,
            "application_block_reason": application_block_reason,
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "analysis_source_label": analysis_source_label,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/jobs/{job_id}/status")
async def update_job_status(job_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    now = utc_now()
    status = str(form.get("status") or "待确认")
    note = str(form.get("note") or "").strip()
    skip_reason = str(form.get("skip_reason") or "").strip()
    allowed_statuses = {
        "待确认",
        "待投递",
        "已投递",
        "已沟通",
        "面试邀请",
        "待笔试",
        "待面试",
        "面试准备中",
        "已面试",
        "已拒绝",
        "已通过",
        "已归档",
    }
    if status not in allowed_statuses:
        return redirect_with_notice(f"/jobs/{job_id}", "状态无效，未更新。", "error")
    with connect() as conn:
        row = conn.execute("SELECT id FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return redirect_with_notice("/jobs", "没有找到这个岗位。", "error")
        conn.execute(
            "UPDATE job_postings SET status = ?, skip_reason = ?, updated_at = ? WHERE id = ?",
            (status, skip_reason, now, job_id),
        )
        if note:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "状态更新", note, now),
            )
        prep_result = None
        if status in INTERVIEW_PREP_TRIGGER_STATUSES:
            prep_result = ensure_interview_preparation_for_job(
                conn,
                job_id,
                trigger_type="status_update",
                source_text=note,
            )
    if prep_result:
        if prep_result.get("created"):
            return redirect_with_notice(f"/jobs/{job_id}", "状态已保存，并已创建本地面试准备。", "success")
        if prep_result.get("interview_id"):
            return redirect_with_notice(f"/jobs/{job_id}", "状态已保存，该岗位已有面试准备。", "info")
    return redirect_with_notice(f"/jobs/{job_id}", "状态已保存。", "success")


@app.post("/jobs/{job_id}/reanalyze")
async def reanalyze_job(job_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return redirect("/jobs")

    job = {key: row[key] for key in row.keys()}
    resume_id = job.get("selected_resume_id")
    requested_depth = str(form.get("search_depth") or job.get("search_depth") or "auto")
    analysis = analyze_job_payload(
        job.get("jd_text") or "",
        int(resume_id) if resume_id else None,
        title=str(form.get("title") or job.get("title") or ""),
        company=str(form.get("company") or job.get("company") or ""),
        city=str(form.get("city") or job.get("city") or ""),
        salary_text=str(form.get("salary_text") or job.get("salary_text") or ""),
        search_depth=requested_depth,
    )
    extracted = analysis["extracted"]
    scoring = analysis["scoring"]
    messages = analysis["messages"]
    status = job.get("status") or "待确认"
    if status in {"待分析", "待确认", "已归档"}:
        status = "待确认" if scoring["recommendation"] in {"必投", "可冲"} else "已归档"

    now = utc_now()
    with connect() as conn:
        conn.execute("DELETE FROM company_research WHERE job_id = ?", (job_id,))
        conn.execute(
            """
            UPDATE job_postings
            SET title = ?, company = ?, city = ?, salary_text = ?,
                internship_days = ?, internship_duration = ?, extracted_json = ?,
                match_score = ?, match_level = ?, risk_level = ?, recommendation = ?,
                status = ?, skip_reason = ?, generated_message = ?, generated_email = ?,
                analysis_error = ?, analysis_source = ?, search_depth = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                extracted.get("title") or "",
                extracted.get("company") or "",
                extracted.get("city") or "",
                extracted.get("salary_text") or "",
                extracted.get("internship_days") or "",
                extracted.get("internship_duration") or "",
                dumps({"extracted": extracted, "scoring": scoring}),
                scoring["score"],
                scoring["level"],
                scoring["risk_level"],
                scoring["recommendation"],
                status,
                scoring["skip_reason"],
                messages["message"],
                messages["email"],
                analysis["analysis_error"],
                analysis["analysis_source"],
                analysis["search_depth"],
                now,
                job_id,
            ),
        )
        conn.execute(
            "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
            (job_id, "重新分析", f"使用{analysis_source_label(analysis['analysis_source'])}刷新岗位分析。", now),
        )
        insert_company_research(
            conn,
            job_id,
            extracted.get("company") or "",
            extracted.get("title") or "",
            extracted.get("city") or "",
            analysis["search_depth"],
        )
    return redirect_with_notice(f"/jobs/{job_id}", "已重新分析岗位。", "success")


@app.get("/interviews")
def interviews_page(request: Request) -> Any:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT i.*, j.title AS job_title, j.company AS company
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            ORDER BY i.created_at DESC
            """
        ).fetchall()
        jobs = conn.execute("SELECT id, title, company FROM job_postings ORDER BY created_at DESC").fetchall()
        feedback_rows = conn.execute(
            """
            SELECT f.*, j.title AS job_title, j.company AS company
            FROM interview_feedback f
            LEFT JOIN job_postings j ON j.id = f.job_id
            ORDER BY f.created_at DESC, f.id DESC
            LIMIT 12
            """
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "interviews.html",
        {
            "interviews": [{key: row[key] for key in row.keys()} for row in rows],
            "jobs": [{key: row[key] for key in row.keys()} for row in jobs],
            "feedback_rows": [{key: row[key] for key in row.keys()} for row in feedback_rows],
        },
    )


@app.post("/interviews")
async def create_interview_review(request: Request) -> RedirectResponse:
    form = await request.form()
    job_id_raw = str(form.get("job_id") or "")
    job_id = int(job_id_raw) if job_id_raw.isdigit() else None
    source_text = str(form.get("source_text") or "")
    job: dict[str, Any] | None = None
    feedback_context = ""
    if job_id:
        with connect() as conn:
            row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
            if row:
                job = parse_json_fields({key: row[key] for key in row.keys()})
            feedback_context = interview_feedback_context(conn, job_id)

    review_source_text = "\n\n".join(item for item in [source_text.strip(), feedback_context] if item)
    review = build_interview_review(job, review_source_text)
    client = client_for_task("interview_review")
    if client and client.configured and source_text:
        try:
            llm_markdown = client.complete_text(
                [
                    {"role": "system", "content": "请生成中文面试复盘 Markdown，聚焦没答好问题、补强建议和下一轮模拟题。"},
                    {"role": "user", "content": dumps({"job": job or {}, "transcript": review_source_text[:12000]})},
                ]
            )
            if llm_markdown.strip():
                review["markdown"] = llm_markdown.strip()
        except Exception as exc:
            client.log_error(str(exc))

    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO interview_preparations (
                job_id, source_text, prep_plan_json, question_bank_json,
                review_markdown, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, review_source_text, dumps(review["plan"]), dumps(review["questions"]), review["markdown"], now, now),
        )
        review_id = cursor.lastrowid
    return redirect(f"/interviews/{review_id}")


@app.get("/interviews/{review_id}")
def interview_detail(review_id: int, request: Request) -> Any:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT i.*, j.title AS job_title, j.company AS company
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            WHERE i.id = ?
            """,
            (review_id,),
        ).fetchone()
        feedback_rows = conn.execute(
            """
            SELECT *
            FROM interview_feedback
            WHERE interview_preparation_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (review_id,),
        ).fetchall()
        recordings = conn.execute(
            "SELECT * FROM interview_recordings WHERE interview_preparation_id = ? ORDER BY created_at DESC, id DESC",
            (review_id,),
        ).fetchall()
    if not row:
        return redirect("/interviews")
    review = {key: row[key] for key in row.keys()}
    review["plan"] = loads(review.get("prep_plan_json"), {})
    review["questions"] = loads(review.get("question_bank_json"), [])
    job_feedback: list[dict[str, Any]] = []
    if review.get("job_id"):
        with connect() as conn:
            job_rows = conn.execute(
                """
                SELECT *
                FROM interview_feedback
                WHERE job_id = ? AND interview_preparation_id != ?
                ORDER BY created_at DESC, id DESC
                LIMIT 20
                """,
                (review["job_id"], review_id),
            ).fetchall()
            job_feedback = [{key: item[key] for key in item.keys()} for item in job_rows]
    return templates.TemplateResponse(
        request,
        "interview_detail.html",
        {
            "review": review,
            "feedback_rows": [{key: item[key] for key in item.keys()} for item in feedback_rows],
            "job_feedback": job_feedback,
            "feedback_statuses": INTERVIEW_FEEDBACK_STATUSES,
            "recordings": [{key: item[key] for key in item.keys()} for item in recordings],
            "transcription_models": TRANSCRIPTION_MODELS,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.get("/interviews/{review_id}/practice")
def interview_practice_page(review_id: int, request: Request) -> Any:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT i.*, j.title AS job_title, j.company AS company
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            WHERE i.id = ?
            """,
            (review_id,),
        ).fetchone()
        if not row:
            return redirect("/interviews")
        review = {key: row[key] for key in row.keys()}
        questions = interview_practice_questions(conn, review)
        attempts = conn.execute(
            """
            SELECT *
            FROM interview_practice_attempts
            WHERE interview_preparation_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 12
            """,
            (review_id,),
        ).fetchall()

    raw_index = request.query_params.get("question", "0")
    try:
        question_index = int(raw_index)
    except (TypeError, ValueError):
        question_index = 0
    question_index = max(0, min(question_index, max(len(questions) - 1, 0)))
    current_question = questions[question_index] if questions else None
    outcome_counts = {"答得不错": 0, "没答好": 0, "跳过": 0}
    for attempt in attempts:
        outcome = str(attempt["outcome"] or "")
        if outcome in outcome_counts:
            outcome_counts[outcome] += 1
    return templates.TemplateResponse(
        request,
        "interview_practice.html",
        {
            "review": review,
            "questions": questions,
            "current_question": current_question,
            "question_index": question_index,
            "attempts": [{key: item[key] for key in item.keys()} for item in attempts],
            "outcome_counts": outcome_counts,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/interviews/{review_id}/practice")
async def save_interview_practice_attempt(review_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    try:
        question_index = int(str(form.get("question_index") or "0"))
    except (TypeError, ValueError):
        question_index = 0
    outcome = str(form.get("outcome") or "").strip()
    if outcome not in {"答得不错", "没答好", "跳过"}:
        return redirect_with_notice(f"/interviews/{review_id}/practice", "请选择本题结果。", "error")

    answer_text = str(form.get("answer_text") or "").strip()[:4000]
    now = utc_now()
    with connect() as conn:
        review_row = conn.execute("SELECT * FROM interview_preparations WHERE id = ?", (review_id,)).fetchone()
        if not review_row:
            return redirect_with_notice("/interviews", "没有找到这条面试准备记录。", "error")
        review = {key: review_row[key] for key in review_row.keys()}
        questions = interview_practice_questions(conn, review)
        if not questions:
            return redirect_with_notice(f"/interviews/{review_id}", "题库为空，请先补充面试准备内容。", "error")
        question_index = max(0, min(question_index, len(questions) - 1))
        current_question = questions[question_index]
        question = str(current_question["question"])
        job_id = int(review["job_id"]) if review.get("job_id") else None
        feedback_id = current_question.get("feedback_id")

        if outcome == "没答好":
            if feedback_id:
                conn.execute(
                    """
                    UPDATE interview_feedback
                    SET user_answer_summary = CASE WHEN ? != '' THEN ? ELSE user_answer_summary END,
                        status = '待练习', updated_at = ?
                    WHERE id = ?
                    """,
                    (answer_text, answer_text, now, feedback_id),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO interview_feedback (
                        job_id, interview_preparation_id, feedback_type, question,
                        user_answer_summary, issue_summary, improvement_plan,
                        status, source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        review_id,
                        "模拟面试",
                        question,
                        answer_text,
                        "模拟面试中标记为没答好，待补充具体卡住点。",
                        "复盘回答结构，补充可验证的项目例子后再次练习。",
                        "待练习",
                        "practice",
                        now,
                        now,
                    ),
                )
                feedback_id = int(cursor.lastrowid)
        elif outcome == "答得不错" and feedback_id:
            conn.execute(
                "UPDATE interview_feedback SET status = ?, updated_at = ? WHERE id = ?",
                ("已补强", now, feedback_id),
            )

        cursor = conn.execute(
            """
            INSERT INTO interview_practice_attempts (
                job_id, interview_preparation_id, interview_feedback_id,
                question, answer_text, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                review_id,
                feedback_id,
                question,
                "" if outcome == "跳过" else answer_text,
                outcome,
                now,
            ),
        )
        attempt_id = int(cursor.lastrowid)
        if job_id:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "模拟面试", f"{outcome}：{question[:360]}", now),
            )
        log_agent_action(
            conn,
            action_type="interview_practice",
            status=outcome,
            summary=f"模拟面试{outcome}：{question[:120]}",
            job_id=job_id,
            decision={
                "review_id": review_id,
                "attempt_id": attempt_id,
                "feedback_id": feedback_id,
                "question_source": current_question["source"],
                "model_called": False,
            },
        )
    next_index = min(question_index + 1, len(questions) - 1)
    message = "已保存本次练习。"
    if outcome == "没答好":
        message += " 已加入待练习薄弱点。"
    elif outcome == "答得不错" and feedback_id:
        message += " 对应薄弱点已标记为已补强。"
    return redirect_with_notice(f"/interviews/{review_id}/practice?question={next_index}", message, "success")


@app.post("/interviews/{review_id}/recordings")
async def upload_interview_recording(review_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    recording = form.get("recording")
    if not recording or not getattr(recording, "filename", "") or not hasattr(recording, "read"):
        return redirect_with_notice(f"/interviews/{review_id}", "请选择一个录音文件。", "error")
    suffix = Path(recording.filename).suffix.lower()
    if suffix not in ALLOWED_RECORDING_EXTENSIONS:
        return redirect_with_notice(f"/interviews/{review_id}", "仅支持 mp3、m4a、wav、mp4、aac、flac、ogg 格式。", "error")
    try:
        recording.file.seek(0, 2)
        size = recording.file.tell()
        recording.file.seek(0)
    except Exception:
        size = 0
    if size > 250 * 1024 * 1024:
        return redirect_with_notice(f"/interviews/{review_id}", "录音文件不能超过 250MB。", "error")

    with connect() as conn:
        review = conn.execute("SELECT id, job_id FROM interview_preparations WHERE id = ?", (review_id,)).fetchone()
    if not review:
        return redirect_with_notice("/interviews", "没有找到这条面试准备记录。", "error")

    original_name = Path(recording.filename).name[:180]
    stored_path = recordings_dir() / f"{uuid.uuid4().hex}{suffix}"
    try:
        with stored_path.open("wb") as output:
            while chunk := await recording.read(1024 * 1024):
                output.write(chunk)
    finally:
        await recording.close()

    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO interview_recordings (
                interview_preparation_id, file_name, file_path, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (review_id, original_name, str(stored_path), "待转写", now, now),
        )
        recording_id = int(cursor.lastrowid)
        job_id = int(review["job_id"]) if review["job_id"] else None
        if job_id:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "面试录音上传", f"已保存本地录音：{original_name}", now),
            )
        log_agent_action(
            conn,
            action_type="interview_recording",
            status="待转写",
            summary=f"已保存本地面试录音：{original_name}",
            job_id=job_id,
            decision={"recording_id": recording_id, "stored_locally": True, "model_called": False},
        )
    return redirect_with_notice(f"/interviews/{review_id}", "录音已保存到本地，可以开始转写。", "success")


@app.post("/interview-recordings/{recording_id}/transcribe")
async def transcribe_interview_recording(recording_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    model_size = str(form.get("model_size") or "base").strip()
    if model_size not in TRANSCRIPTION_MODELS:
        return redirect_with_notice("/interviews", "转写模型无效。", "error")
    with connect() as conn:
        recording = conn.execute("SELECT * FROM interview_recordings WHERE id = ?", (recording_id,)).fetchone()
        if not recording:
            return redirect_with_notice("/interviews", "没有找到这条录音。", "error")
        review_id = int(recording["interview_preparation_id"])
        if recording["status"] == "已转写":
            return redirect_with_notice(f"/interviews/{review_id}", "这条录音已经转写完成。", "info")
        conn.execute(
            "UPDATE interview_recordings SET status = ?, model_size = ?, error_message = '', updated_at = ? WHERE id = ?",
            ("转写中", model_size, utc_now(), recording_id),
        )

    try:
        transcription = await run_in_threadpool(transcribe_recording, str(recording["file_path"]), model_size)
    except ValueError as exc:
        error_message = str(exc)[:1000]
        with connect() as conn:
            conn.execute(
                "UPDATE interview_recordings SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                ("转写失败", error_message, utc_now(), recording_id),
            )
            log_agent_action(
                conn,
                action_type="interview_recording",
                status="转写失败",
                summary=error_message[:200],
                decision={"recording_id": recording_id, "model_size": model_size, "model_called": False},
            )
        return redirect_with_notice(f"/interviews/{review_id}", error_message, "error")

    transcript = str(transcription["transcript"])
    language = str(transcription.get("language") or "zh")
    now = utc_now()
    with connect() as conn:
        review_row = conn.execute(
            """
            SELECT i.*, j.title AS job_title, j.company AS company
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            WHERE i.id = ?
            """,
            (review_id,),
        ).fetchone()
        if not review_row:
            return redirect_with_notice("/interviews", "没有找到关联的面试准备。", "error")
        review = {key: review_row[key] for key in review_row.keys()}
        job = parse_json_fields(review) if review.get("job_id") else None
        source_text = str(review.get("source_text") or "").strip()
        recording_section = f"【录音转写：{recording['file_name']}】\n{transcript}"
        updated_source = "\n\n".join(item for item in [source_text, recording_section] if item)
        refreshed = build_interview_review(job, updated_source)
        conn.execute(
            """
            UPDATE interview_recordings
            SET status = ?, model_size = ?, language = ?, transcript = ?, error_message = '', updated_at = ?
            WHERE id = ?
            """,
            ("已转写", model_size, language, transcript, now, recording_id),
        )
        conn.execute(
            """
            UPDATE interview_preparations
            SET source_text = ?, prep_plan_json = ?, question_bank_json = ?, review_markdown = ?, updated_at = ?
            WHERE id = ?
            """,
            (updated_source, dumps(refreshed["plan"]), dumps(refreshed["questions"]), refreshed["markdown"], now, review_id),
        )
        job_id = int(review["job_id"]) if review["job_id"] else None
        if job_id:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "面试录音转写", f"已用本地 {model_size} 模型转写：{recording['file_name']}", now),
            )
        log_agent_action(
            conn,
            action_type="interview_recording",
            status="已转写",
            summary=f"本地转写完成：{recording['file_name']}",
            job_id=job_id,
            decision={
                "recording_id": recording_id,
                "model_size": model_size,
                "language": language,
                "transcript_length": len(transcript),
                "stored_locally": True,
                "local_asr_called": True,
                "llm_called": False,
            },
        )
    return redirect_with_notice(f"/interviews/{review_id}", "本地转写完成，已刷新面试准备和题库。", "success")


@app.post("/interview-recordings/{recording_id}/delete")
async def delete_interview_recording(recording_id: int) -> RedirectResponse:
    with connect() as conn:
        recording = conn.execute("SELECT * FROM interview_recordings WHERE id = ?", (recording_id,)).fetchone()
        if not recording:
            return redirect_with_notice("/interviews", "没有找到这条录音。", "error")
        review_id = int(recording["interview_preparation_id"])
        review_row = conn.execute(
            """
            SELECT i.*, j.title AS job_title, j.company AS company
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            WHERE i.id = ?
            """,
            (review_id,),
        ).fetchone()
        if not review_row:
            return redirect_with_notice("/interviews", "没有找到关联的面试准备。", "error")
        review = {key: review_row[key] for key in review_row.keys()}
        transcript = str(recording["transcript"] or "").strip()
        source_text = str(review.get("source_text") or "")
        if transcript:
            section = f"【录音转写：{recording['file_name']}】\n{transcript}"
            source_text = source_text.replace(section, "", 1)
            source_text = re.sub(r"\n{3,}", "\n\n", source_text).strip()
            job = parse_json_fields(review) if review.get("job_id") else None
            refreshed = build_interview_review(job, source_text)
            conn.execute(
                """
                UPDATE interview_preparations
                SET source_text = ?, prep_plan_json = ?, question_bank_json = ?, review_markdown = ?, updated_at = ?
                WHERE id = ?
                """,
                (source_text, dumps(refreshed["plan"]), dumps(refreshed["questions"]), refreshed["markdown"], utc_now(), review_id),
            )
        conn.execute("DELETE FROM interview_recordings WHERE id = ?", (recording_id,))
        job_id = int(review["job_id"]) if review["job_id"] else None
        log_agent_action(
            conn,
            action_type="interview_recording",
            status="已删除",
            summary=f"已删除本地面试录音和转写：{recording['file_name']}",
            job_id=job_id,
            decision={
                "recording_id": recording_id,
                "removed_transcript": bool(transcript),
                "stored_locally": True,
                "llm_called": False,
            },
        )

    stored_path = Path(str(recording["file_path"] or "")).resolve()
    base_dir = recordings_dir()
    if base_dir in stored_path.parents:
        try:
            stored_path.unlink(missing_ok=True)
        except OSError:
            pass
    return redirect_with_notice(f"/interviews/{review_id}", "已删除本地录音及其转写内容。", "success")


@app.post("/interviews/{review_id}/feedback")
async def create_interview_feedback(review_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    feedback_type = str(form.get("feedback_type") or "技术问题").strip()[:80]
    question = str(form.get("question") or "").strip()[:500]
    user_answer_summary = str(form.get("user_answer_summary") or "").strip()[:1000]
    issue_summary = str(form.get("issue_summary") or "").strip()[:1000]
    improvement_plan = str(form.get("improvement_plan") or "").strip()[:1000]
    if not question and not issue_summary:
        return redirect_with_notice(f"/interviews/{review_id}", "请至少填写问题或卡住点。", "error")

    now = utc_now()
    with connect() as conn:
        review = conn.execute("SELECT id, job_id FROM interview_preparations WHERE id = ?", (review_id,)).fetchone()
        if not review:
            return redirect_with_notice("/interviews", "没有找到这条面试准备记录。", "error")
        job_id = int(review["job_id"]) if review["job_id"] else None
        cursor = conn.execute(
            """
            INSERT INTO interview_feedback (
                job_id, interview_preparation_id, feedback_type, question,
                user_answer_summary, issue_summary, improvement_plan,
                status, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                review_id,
                feedback_type,
                question,
                user_answer_summary,
                issue_summary,
                improvement_plan,
                "待练习",
                "manual",
                now,
                now,
            ),
        )
        feedback_id = int(cursor.lastrowid)
        if job_id:
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, "面试反馈记录", (question or issue_summary)[:500], now),
            )
        log_agent_action(
            conn,
            action_type="interview_feedback_update",
            status="已创建",
            summary=f"新增面试薄弱点：{(question or issue_summary)[:120]}",
            job_id=job_id,
            decision={
                "review_id": review_id,
                "feedback_id": feedback_id,
                "feedback_type": feedback_type,
                "status": "待练习",
                "model_called": False,
            },
        )
    return redirect_with_notice(f"/interviews/{review_id}", "已记录面试薄弱点。", "success")


@app.post("/interview-feedback/{feedback_id}")
async def update_interview_feedback(feedback_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    status = str(form.get("status") or "").strip()
    if status not in INTERVIEW_FEEDBACK_STATUSES:
        return redirect_with_notice("/interviews", "反馈状态无效。", "error")
    now = utc_now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM interview_feedback WHERE id = ?", (feedback_id,)).fetchone()
        if not row:
            return redirect_with_notice("/interviews", "没有找到这条面试反馈。", "error")
        old_status = str(row["status"] or "")
        conn.execute(
            "UPDATE interview_feedback SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, feedback_id),
        )
        log_agent_action(
            conn,
            action_type="interview_feedback_update",
            status=status,
            summary=f"面试反馈状态：{old_status or '-'} -> {status}",
            job_id=int(row["job_id"]) if row["job_id"] else None,
            decision={
                "review_id": int(row["interview_preparation_id"]) if row["interview_preparation_id"] else None,
                "feedback_id": feedback_id,
                "old_status": old_status,
                "new_status": status,
                "model_called": False,
            },
        )
    review_id = int(row["interview_preparation_id"]) if row["interview_preparation_id"] else None
    return redirect_with_notice(f"/interviews/{review_id}" if review_id else "/interviews", "面试反馈状态已更新。", "success")


@app.get("/interviews/{review_id}/download")
def download_interview_review(review_id: int) -> Response:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT i.review_markdown, COALESCE(j.title, 'interview-review') AS title
            FROM interview_preparations i
            LEFT JOIN job_postings j ON j.id = i.job_id
            WHERE i.id = ?
            """,
            (review_id,),
        ).fetchone()
    if not row:
        return Response("Not found", status_code=404)
    filename = safe_filename(row["title"] or "interview-review") + ".md"
    return Response(
        row["review_markdown"] or "",
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def safe_filename(value: str) -> str:
    keep = []
    for char in value:
        if char.isalnum() or char in {"-", "_"}:
            keep.append(char)
        elif char.isspace():
            keep.append("-")
    return "".join(keep).strip("-")[:80] or "interview-review"


@app.get("/settings")
def settings_page(request: Request) -> Any:
    with connect() as conn:
        profile_rows = conn.execute("SELECT * FROM model_profiles ORDER BY is_default DESC, id").fetchall()
        routes = conn.execute(
            """
            SELECT r.*, p.name AS profile_name
            FROM model_routes r
            LEFT JOIN model_profiles p ON p.id = r.profile_id
            ORDER BY r.id
            """
        ).fetchall()
        logs = conn.execute("SELECT * FROM model_call_logs ORDER BY created_at DESC LIMIT 20").fetchall()
    profiles = []
    env_usage: dict[str, list[str]] = {}
    for row in profile_rows:
        key_name = row["api_key_env"] or suggest_api_key_env(row["name"], row["base_url"])
        env_usage.setdefault(key_name, []).append(row["name"])
    for row in profile_rows:
        profile = {key: row[key] for key in row.keys()}
        key_name = profile.get("api_key_env") or suggest_api_key_env(profile.get("name") or "", profile.get("base_url") or "")
        profile["api_key_env"] = key_name
        profile["api_key_mask"] = mask_secret(os.environ.get(key_name, ""))
        profile["api_key_env_shared_with"] = [name for name in env_usage.get(key_name, []) if name != profile.get("name")]
        profile["api_key_env_suggestion"] = suggest_api_key_env(profile.get("name") or "", profile.get("base_url") or "")
        profiles.append(profile)
    env_path = ROOT_DIR / ".env"
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "profiles": profiles,
            "routes": [{key: row[key] for key in row.keys()} for row in routes],
            "logs": [{key: row[key] for key in row.keys()} for row in logs],
            "task_label": task_label,
            "token_stats": token_stats(),
            "env_exists": env_path.exists(),
            "env_example": (ROOT_DIR / ".env.example").read_text(encoding="utf-8"),
            "blacklist_companies": "\n".join(get_setting("blacklist_companies", []) or []),
            "blacklist_keywords": "\n".join(get_setting("blacklist_keywords", []) or []),
            "communication_policy": communication_policy(),
            "automation_control": automation_control(),
            "message_patrol_policy": message_patrol_policy(),
            "communication_modes": COMMUNICATION_MODES,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
            "default_api_key_env": "OPENAI_COMPATIBLE_API_KEY",
        },
    )


@app.post("/settings/model-profiles")
async def upsert_model_profile(request: Request) -> RedirectResponse:
    form = await request.form()
    profile_id = str(form.get("profile_id") or "")
    action = str(form.get("action") or "save")
    name = str(form.get("name") or "未命名配置")
    base_url = str(form.get("base_url") or "")
    api_key_env = str(form.get("api_key_env") or "").strip() or suggest_api_key_env(name, base_url)
    api_key_value = str(form.get("api_key") or "").strip()
    if api_key_value and not looks_masked(api_key_value):
        set_env_value(api_key_env, api_key_value)

    values = (
        name,
        base_url,
        api_key_env,
        str(form.get("model") or ""),
        float(form.get("temperature") or 0.2),
        float(form.get("input_cost_per_million") or 0),
        float(form.get("output_cost_per_million") or 0),
        1 if form.get("is_default") == "on" else 0,
        utc_now(),
    )
    with connect() as conn:
        if values[7]:
            conn.execute("UPDATE model_profiles SET is_default = 0")
        if profile_id.isdigit():
            conn.execute(
                """
                UPDATE model_profiles
                SET name = ?, base_url = ?, api_key_env = ?, model = ?, temperature = ?,
                    input_cost_per_million = ?, output_cost_per_million = ?,
                    is_default = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, int(profile_id)),
            )
        else:
            conn.execute(
                """
                INSERT INTO model_profiles (
                    name, base_url, api_key_env, model, temperature,
                    input_cost_per_million, output_cost_per_million, is_default,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, values[-1]),
            )
        shared_rows = conn.execute(
            "SELECT name FROM model_profiles WHERE api_key_env = ? AND name != ? ORDER BY id",
            (api_key_env, name),
        ).fetchall()
    shared_names = [row["name"] for row in shared_rows]
    save_message = f"模型配置已保存。API Key 变量名：{api_key_env}"
    if shared_names:
        save_message += f"；该变量也被 {', '.join(shared_names)} 使用，会共用同一个 Key。"

    if action == "test":
        profile = {
            "name": values[0],
            "base_url": values[1],
            "api_key_env": values[2],
            "model": values[3],
            "temperature": values[4],
            "input_cost_per_million": values[5],
            "output_cost_per_million": values[6],
        }
        client = OpenAICompatibleClient(profile, "settings_test")
        try:
            client.complete_text(
                [
                    {"role": "system", "content": "你是模型连通性测试助手。"},
                    {"role": "user", "content": "请只回复 OK。"},
                ]
            )
            return redirect_with_notice("/settings", f"模型连接成功：{values[0]} / {values[3]}", "success")
        except Exception as exc:
            client.log_error(str(exc))
            return redirect_with_notice("/settings", f"模型连接失败：{str(exc)[:160]}", "error")

    return redirect_with_notice("/settings", save_message, "success")


@app.post("/settings/communication")
async def update_communication_policy(request: Request) -> RedirectResponse:
    form = await request.form()
    old_policy = communication_policy()
    mode = str(form.get("mode") or "draft").strip()
    if mode not in dict(COMMUNICATION_MODES):
        return redirect_with_notice("/settings", "沟通模式无效，未保存。", "error")
    try:
        max_followups = int(str(form.get("max_auto_followups") or "2"))
    except ValueError:
        max_followups = 2
    max_followups = max(0, min(max_followups, 10))
    set_setting(
        COMMUNICATION_POLICY_KEY,
        {
            "mode": mode,
            "max_auto_followups": max_followups,
        },
    )
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="communication_policy_update",
            status=mode,
            summary=f"沟通模式：{old_policy['mode_label']} -> {communication_mode_label(mode)}",
            decision={
                "old_mode": old_policy["mode"],
                "new_mode": mode,
                "old_max_auto_followups": old_policy["max_auto_followups"],
                "new_max_auto_followups": max_followups,
            },
        )
    return redirect_with_notice(
        "/settings",
        f"沟通模式已保存：{communication_mode_label(mode)}，自主询问上限 {max_followups} 轮。",
        "success",
    )


@app.post("/settings/automation-control")
async def update_automation_control(request: Request) -> RedirectResponse:
    form = await request.form()
    action = str(form.get("action") or "").strip()
    if action not in {"pause", "resume"}:
        return redirect_with_notice("/settings", "自动化控制动作无效。", "error")

    old_control = automation_control()
    paused = action == "pause"
    reason = str(form.get("pause_reason") or "").strip()[:200]
    now = utc_now()
    new_control = {
        "paused": paused,
        "pause_reason": reason if paused else "",
        "updated_at": now,
    }
    set_setting(AUTOMATION_CONTROL_KEY, new_control)
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="automation_control_update",
            status="已暂停" if paused else "运行中",
            summary="自动化控制：{} -> {}".format(old_control["status_label"], "已暂停" if paused else "运行中"),
            decision={
                "old_paused": old_control["paused"],
                "new_paused": paused,
                "pause_reason": new_control["pause_reason"],
            },
        )

    return_to = str(form.get("return_to") or "/settings")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/settings"
    return redirect_with_notice(return_to, "自动化已暂停。" if paused else "自动化已恢复。", "success")


@app.post("/settings/message-patrol")
async def update_message_patrol_policy(request: Request) -> RedirectResponse:
    form = await request.form()
    old_policy = message_patrol_policy()
    enabled = form.get("enabled") == "on"
    interval_seconds = clamp_int(form.get("interval_seconds"), old_policy["interval_seconds"], 30, 3600)
    cooldown_seconds = clamp_int(form.get("cooldown_seconds"), old_policy["cooldown_seconds"], 0, 3600)
    now = utc_now()
    next_tick_at = old_policy["next_tick_at"]
    if enabled and not old_policy["enabled"]:
        next_tick_at = (datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)).isoformat(timespec="seconds")
    if not enabled:
        next_tick_at = ""

    new_policy = {
        "enabled": enabled,
        "interval_seconds": interval_seconds,
        "cooldown_seconds": cooldown_seconds,
        "last_tick_at": old_policy["last_tick_at"],
        "next_tick_at": next_tick_at,
        "last_status": old_policy["last_status"],
        "updated_at": now,
    }
    save_message_patrol_policy(new_policy)
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="message_patrol_policy_update",
            status="已开启" if enabled else "已关闭",
            summary=f"定时巡检：{old_policy['status_label']} -> {'已开启' if enabled else '已关闭'}",
            decision={
                "old_enabled": old_policy["enabled"],
                "new_enabled": enabled,
                "interval_seconds": interval_seconds,
                "cooldown_seconds": cooldown_seconds,
                "next_tick_at": next_tick_at,
            },
        )

    return_to = str(form.get("return_to") or "/settings")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/settings"
    return redirect_with_notice(return_to, "定时巡检设置已保存。", "success")


@app.post("/message-patrol/tick")
async def trigger_message_patrol_tick(request: Request) -> RedirectResponse:
    form = await request.form()
    result = await run_in_threadpool(run_message_patrol_tick, "manual", force=False)
    if result is None:
        message = "定时巡检未到下次执行时间。"
        notice_type = "info"
    else:
        message = f"巡检 tick：{result['status']}。{result['note']}"
        notice_type = "success" if result["status"] in {"待接入", "未启用"} else "info"

    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    return redirect_with_notice(return_to, message, notice_type)


@app.post("/message-patrol/open-browser")
async def open_message_patrol_browser_route(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    try:
        target_url = await run_in_threadpool(open_message_patrol_browser, str(form.get("start_url") or ""))
    except Exception as exc:
        return redirect_with_notice(return_to, f"打开 Edge 巡检窗口失败：{str(exc)[:180]}", "error")
    if target_url == "about:blank":
        return redirect_with_notice(return_to, "已打开 Edge 巡检窗口，请在该窗口登录招聘平台并打开 HR 对话页。", "success")
    return redirect_with_notice(return_to, f"已打开 Edge 巡检窗口：{target_url}", "success")


@app.post("/message-patrol/browser-dry-run")
async def trigger_message_patrol_browser_dry_run(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"

    policy = communication_policy()
    if policy["mode"] == "off":
        return redirect_with_notice(return_to, "沟通模式为关闭，未读取浏览器页面。", "info")

    result = await run_in_threadpool(
        run_browser_message_patrol_executor,
        trigger_type="manual_browser",
        scope="manual_browser_patrol",
        dry_run=True,
    )
    notice_type = "success" if result["status"] in {"观察完成", "无新内容", "无需回复", "已忽略", "已跳过", "未发现聊天页"} else "info"
    if result.get("error_count"):
        notice_type = "error"
    return redirect_with_notice(return_to, f"浏览器 dry-run 巡检：{result['status']}。{result['note']}", notice_type)


@app.post("/settings/blacklists")
async def update_blacklists(request: Request) -> RedirectResponse:
    form = await request.form()
    companies = [item.strip() for item in str(form.get("blacklist_companies") or "").splitlines() if item.strip()]
    keywords = [item.strip() for item in str(form.get("blacklist_keywords") or "").splitlines() if item.strip()]
    set_setting("blacklist_companies", companies)
    set_setting("blacklist_keywords", keywords)
    return redirect("/settings")


@app.post("/settings/routes")
async def update_routes(request: Request) -> RedirectResponse:
    form = await request.form()
    now = utc_now()
    with connect() as conn:
        for task_type, _label in TASK_TYPES:
            raw = str(form.get(task_type) or "")
            profile_id = int(raw) if raw.isdigit() else None
            conn.execute(
                "UPDATE model_routes SET profile_id = ?, updated_at = ? WHERE task_type = ?",
                (profile_id, now, task_type),
            )
    return redirect("/settings")
