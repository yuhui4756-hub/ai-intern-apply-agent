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
    daily_salary_bounds,
    extract_salary,
    generate_message,
    looks_like_salary_text,
    rule_extract_jd,
    score_job,
    strip_platform_safety_notice,
)
from .services.browser_patrol import (
    capture_browser_patrol_observations,
    open_message_patrol_browser,
    scan_controlled_edge_unread_conversations,
)
from .services.application_browser import (
    build_application_browser_plan,
    fill_application_note_in_controlled_edge,
    probe_application_browser_plan,
    upload_application_resume_in_controlled_edge,
)
from .services.communication_browser import (
    build_browser_send_adapter_plan,
    calibrate_controlled_edge_chat_pages,
    fill_message_in_controlled_edge,
    is_pc_message_automation_platform,
    probe_browser_send_adapter_plan,
    send_message_in_controlled_edge,
)
from .services.conversation import classify_conversation, prepare_conversation_text
from .services.control_layer import (
    CITY_NAMES,
    CONTROL_STATUS_UPDATE_TARGETS,
    ROLE_NAMES,
    control_memory_contains_sensitive_text,
    explicit_control_job_ids,
    normalize_model_control_intent,
    parse_control_job_list_filters,
    parse_control_intent,
    redact_control_text,
)
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
from .services.interview_export import render_interview_review_pdf
from .services.job_fetcher import FetchResult, ensure_public_http_url, fetch_job_from_url, normalize_visible_text, validate_fetched_text
from .services.job_searcher import (
    SearchResult,
    capture_current_search_page,
    close_controlled_edge_target,
    controlled_job_snapshot_expression,
    controlled_edge_status,
    create_controlled_edge_target,
    evaluate_cdp_expression,
    extract_candidates_from_anchors,
    fetch_job_from_controlled_edge,
    is_recruitment_interstitial_url,
    open_manual_search_in_edge,
    search_jobs_in_controlled_edge,
    search_jobs_with_browser,
    wait_for_cdp_document_ready,
)
from .services.llm import OpenAICompatibleClient, client_for_task
from .services.research import search_company
from .services.resume import read_resume_text
from .services.transcription import ALLOWED_RECORDING_EXTENSIONS, TRANSCRIPTION_MODELS, transcribe_recording
from .services.visual_page import capture_controlled_edge_visual_page, capture_visual_page_target


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
AUTONOMOUS_DAILY_SEND_LIMIT = 5
AUTONOMOUS_PLATFORM_DAILY_SEND_LIMIT = 3
AUTONOMOUS_MESSAGE_BLOCKING_TEXT = ("简历", "附件", "电话", "微信", "邮箱", "身份证", "银行卡", "押金", "培训费", "贷款", "报价")
JOB_DISCOVERY_PLATFORMS = ("Boss 直聘", "猎聘", "实习僧")
JOB_DISCOVERY_SEARCH_PAGE_LIMIT = 3
JOB_DISCOVERY_CANDIDATE_LIMIT = 18
JOB_DISCOVERY_IMPORT_LIMIT = 6
DEFAULT_DISCOVERY_ROLES = ("AI 应用开发实习", "Agent 开发实习", "AI 后端实习")
DEFAULT_DISCOVERY_CITIES = ("北京", "上海", "广州", "深圳", "杭州", "重庆", "成都")
JOB_DISCOVERY_STRONG_ROLE_SIGNALS = (
    "ai",
    "agent",
    "智能体",
    "大模型",
    "llm",
    "rag",
    "aigc",
    "人工智能",
    "机器学习",
    "深度学习",
    "自然语言",
    "nlp",
)
JOB_DISCOVERY_SECONDARY_ROLE_SIGNALS = ("模型", "算法")
JOB_DISCOVERY_SENIOR_EXPERIENCE_RE = re.compile(r"(?<!\d)(?:[2-9]|1\d)\s*(?:[-~～至到]\s*(?:[2-9]|1\d))?\s*年(?:经验|工作经验)?")
JOB_DISCOVERY_INTERNSHIP_SIGNAL_RE = re.compile(r"实习|在校|校招|应届|学生")
JOB_DISCOVERY_NON_ENGINEERING_SIGNALS = (
    "法务",
    "供应链",
    "物流",
    "采购",
    "贸易",
    "并购",
    "m&a",
    "财务",
    "会计",
    "人事",
    "行政",
    "销售",
    "客服",
    "市场",
    "运营",
    "产品经理",
    "设计",
)
INTERVIEW_PREP_TRIGGER_STATUSES = {"待面试", "面试准备中"}
INTERVIEW_FEEDBACK_STATUSES = ["待练习", "已补强", "已归档"]
CANDIDATE_FEEDBACK_STATUSES = ["", "正确", "误判", "待观察"]
CANDIDATE_EXPECTED_SCREENINGS = ["", "自动读取 JD", "人工复核", "跳过"]
APPLICATION_PREPARATION_STATUSES = ["待确认", "已确认", "已跳过"]
APPLICATION_ELIGIBLE_RECOMMENDATIONS = {"必投", "可投递"}
APPLICATION_LOW_RISK_LEVELS = {"低", "低风险"}
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
        "communication_browser_calibration": "聊天页结构校准",
        "unread_conversation_scan": "未读会话只读扫描",
        "communication_browser_fill": "浏览器填入草稿",
        "communication_autonomous_send": "自主沟通发送",
        "communication_autonomous_executor": "自主沟通执行",
        "workflow_control": "求职流程控制",
        "job_discovery": "岗位发现",
        "job_candidate_feedback": "候选校准",
        "job_rescore": "本地重新评分",
        "job_match_review": "岗位深度复核",
        "demo_draft_created": "演练草稿",
        "interview_prep_auto_create": "面试准备",
        "interview_prep_enhance": "智能面试准备",
        "interview_feedback_update": "面试反馈",
        "interview_practice": "模拟面试",
        "interview_recording": "面试录音",
        "application_preparation": "投递准备",
        "communication_preparation": "沟通准备",
        "application_browser_open": "投递页面打开",
        "application_browser_probe": "投递页面演练",
        "application_browser_fill": "投递附言填入",
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
        "daily_send_limit": AUTONOMOUS_DAILY_SEND_LIMIT,
        "platform_daily_send_limit": AUTONOMOUS_PLATFORM_DAILY_SEND_LIMIT,
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


def start_autonomous_communication_workflow(start_url: str = "") -> dict[str, Any]:
    """Open the controlled browser, then explicitly enable the bounded messaging workflow."""
    target_url = open_message_patrol_browser(start_url)
    old_policy = communication_policy()
    old_control = automation_control()
    old_patrol = message_patrol_policy()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")
    interval_seconds = int(old_patrol["interval_seconds"])
    next_tick_at = (now_dt + timedelta(seconds=min(30, interval_seconds))).isoformat(timespec="seconds")

    set_setting(
        COMMUNICATION_POLICY_KEY,
        {
            "mode": "autonomous",
            "max_auto_followups": old_policy["max_auto_followups"],
        },
    )
    set_setting(
        AUTOMATION_CONTROL_KEY,
        {
            "paused": False,
            "pause_reason": "",
            "updated_at": now,
        },
    )
    save_message_patrol_policy(
        {
            **old_patrol,
            "enabled": True,
            "next_tick_at": next_tick_at,
            "updated_at": now,
        }
    )
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="workflow_control",
            status="已启动",
            summary="已打开受控 Edge，并启用自主询问和定时巡检。",
            decision={
                "target_url": target_url,
                "old_mode": old_policy["mode"],
                "new_mode": "autonomous",
                "old_paused": old_control["paused"],
                "new_paused": False,
                "old_patrol_enabled": old_patrol["enabled"],
                "new_patrol_enabled": True,
                "interval_seconds": interval_seconds,
                "next_tick_at": next_tick_at,
                "daily_send_limit": AUTONOMOUS_DAILY_SEND_LIMIT,
                "platform_daily_send_limit": AUTONOMOUS_PLATFORM_DAILY_SEND_LIMIT,
                "message_text_saved": False,
            },
        )
    return {
        "target_url": target_url,
        "interval_seconds": interval_seconds,
        "next_tick_at": next_tick_at,
    }


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
        autonomous = communication_policy()["mode"] == "autonomous"
        executor_trigger = "scheduled_executor" if trigger_type == "scheduler" else "manual_browser"
        result = run_browser_message_patrol_executor(trigger_type=executor_trigger, dry_run=not autonomous)
        if autonomous and result.get("new_count"):
            autonomous_result = run_autonomous_communication_executor(executor_trigger)
            result = {
                **result,
                "autonomous_result": autonomous_result,
                "note": f"{result['note']} 自主沟通：{autonomous_result['note']}",
            }
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


def get_matching_evidence(resume_id: int | None) -> tuple[str, str]:
    _resume, resume_text = get_resume_text(resume_id)
    if resume_text.strip():
        return resume_text, "简历正文"
    with connect() as conn:
        profile = conn.execute("SELECT skills_json FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
    skills = loads(profile["skills_json"], []) if profile else []
    explicit_skills = [str(skill).strip() for skill in skills if str(skill).strip()]
    if explicit_skills:
        return "\n".join(explicit_skills), "候选人画像技能"
    return "", "无可用简历或画像技能"


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
    matching_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resume_text, matching_evidence = get_matching_evidence(resume_id)
    analysis_text = strip_platform_safety_notice(jd_text)
    fallback_extract = rule_extract_jd(
        analysis_text,
        fallback_title=title,
        fallback_company=company,
        fallback_city=city,
        fallback_salary=salary_text,
    )
    llm_extract, analysis_error = try_llm_jd_extract(analysis_text)
    extracted = clean_extracted({**fallback_extract, **(llm_extract or {})})
    extracted["required_skills"] = list(dict.fromkeys((llm_extract or {}).get("required_skills") or fallback_extract["required_skills"]))
    grounded_llm_risks = [
        signal
        for signal in clean_extracted(llm_extract or {}).get("risk_signals", [])
        if signal in analysis_text
    ]
    extracted["risk_signals"] = list(dict.fromkeys(fallback_extract["risk_signals"] + grounded_llm_risks))
    extracted["caution_signals"] = list(dict.fromkeys((llm_extract or {}).get("caution_signals") or fallback_extract["caution_signals"]))
    if fallback_extract.get("salary_text"):
        extracted["salary_text"] = fallback_extract["salary_text"]
    elif extracted.get("salary_text") and not looks_like_salary_text(extracted["salary_text"]):
        extracted["salary_text"] = ""
    apply_blacklists(extracted, analysis_text)

    if matching_preferences is None:
        with connect() as conn:
            profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        profile_data = {key: profile[key] for key in profile.keys()} if profile else {}
        matching_preferences = discovery_filters_from_profile(profile_data)
    scoring = score_job(
        extracted,
        analysis_text,
        resume_text,
        matching_preferences,
        matching_evidence=matching_evidence,
    )
    messages = try_llm_message(extracted, scoring, generate_message(extracted, scoring)) if generate_messages and analysis_text else {"message": "", "email": ""}
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


def rescore_saved_job(conn: Any, job_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not row:
        raise ValueError("没有找到要重新评分的岗位。")
    job = {key: row[key] for key in row.keys()}
    stored = loads(job.get("extracted_json"), {})
    stored_extracted = stored.get("extracted") if isinstance(stored, dict) else {}
    analysis_text = strip_platform_safety_notice(str(job.get("jd_text") or ""))
    fallback_extract = rule_extract_jd(
        analysis_text,
        fallback_title=str(job.get("title") or ""),
        fallback_company=str(job.get("company") or ""),
        fallback_city=str(job.get("city") or ""),
        fallback_salary=str(job.get("salary_text") or ""),
    )
    extracted = clean_extracted(stored_extracted or fallback_extract)
    if not extracted.get("required_skills"):
        extracted["required_skills"] = fallback_extract["required_skills"]
    apply_blacklists(extracted, analysis_text)
    resume_id = job.get("selected_resume_id")
    resume_text, matching_evidence = get_matching_evidence(int(resume_id) if resume_id else None)
    profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
    profile_data = {key: profile[key] for key in profile.keys()} if profile else {}
    preferences = discovery_filters_from_profile(profile_data)
    scoring = score_job(
        extracted,
        analysis_text,
        resume_text,
        preferences,
        matching_evidence=matching_evidence,
    )
    now = utc_now()
    conn.execute(
        """
        UPDATE job_postings
        SET extracted_json = ?, match_score = ?, match_level = ?, risk_level = ?,
            recommendation = ?, skip_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            dumps({"extracted": extracted, "scoring": scoring}),
            scoring["score"],
            scoring["level"],
            scoring["risk_level"],
            scoring["recommendation"],
            scoring["skip_reason"],
            now,
            job_id,
        ),
    )
    conn.execute(
        "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
        (job_id, "本地重新评分", "按当前本地规则重算匹配度，不调用模型或重新抓取 JD。", now),
    )
    log_agent_action(
        conn,
        action_type="job_rescore",
        status="完成",
        summary=f"本地重新评分：{job.get('title') or '岗位'}",
        platform=str(job.get("platform") or ""),
        job_id=job_id,
        decision={
            "model_called": False,
            "score": scoring["score"],
            "recommendation": scoring["recommendation"],
            "required_units": scoring["score_breakdown"]["required_units"],
            "matched_requirement_units": scoring["score_breakdown"]["matched_requirement_units"],
        },
    )
    return scoring


def insert_company_research(
    conn: Any,
    job_id: int,
    company: str,
    title: str,
    city: str,
    search_depth: str,
    *,
    replace_existing: bool = False,
) -> int:
    """Fetch and persist user-requested public company search evidence."""
    if not company:
        return 0
    results = search_company(company, title, city, search_depth)
    if results and replace_existing:
        conn.execute("DELETE FROM company_research WHERE job_id = ?", (job_id,))
    now = utc_now()
    for result in results:
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
    return len(results)


def run_company_research_for_job(
    job_id: int,
    *,
    requested_depth: str = "auto",
    trigger_type: str = "job_detail",
) -> dict[str, Any]:
    if requested_depth not in {"auto", "quick", "standard", "deep"}:
        return {"status": "未执行", "reason": "检索深度无效。"}
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return {"status": "未执行", "reason": "岗位不存在。"}
        job = {key: row[key] for key in row.keys()}
        company = str(job.get("company") or "").strip()
        if not company:
            return {"status": "未执行", "reason": "公司名称为空，请先补充岗位信息。"}

        search_depth = company_research_depth(job, requested_depth)
        result_count = insert_company_research(
            conn,
            job_id,
            company,
            str(job.get("title") or ""),
            str(job.get("city") or ""),
            search_depth,
            replace_existing=True,
        )
        status = "完成" if result_count else "无结果"
        note = (
            f"已查询“{company}”的公开公司资料，保存 {result_count} 条来源。"
            if result_count
            else f"未找到“{company}”可保存的公开公司资料，已保留原有查询结果。"
        )
        log_agent_action(
            conn,
            action_type="company_risk_research",
            status=status,
            summary=note,
            platform=str(job.get("platform") or ""),
            job_id=job_id,
            decision={
                "search_depth": search_depth,
                "source_count": result_count,
                "model_called": False,
                "user_triggered": True,
                "trigger_type": trigger_type,
                "recruitment_platform_accessed": False,
            },
        )
    return {
        "status": status,
        "note": note,
        "source_count": result_count,
        "search_depth": search_depth,
        "company": company,
    }


def normalized_company_name(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def company_research_depth(job: dict[str, Any], requested_depth: str) -> str:
    if requested_depth in {"quick", "standard", "deep"}:
        return requested_depth
    stored = loads(str(job.get("extracted_json") or ""), {})
    extracted = stored.get("extracted", {}) if isinstance(stored, dict) else {}
    scoring = stored.get("scoring", {}) if isinstance(stored, dict) else {}
    return auto_search_depth(scoring if isinstance(scoring, dict) else {}, extracted if isinstance(extracted, dict) else {})


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
        "controlled_edge": "受控 Edge",
        "controlled_edge_visual": "受控 Edge 视觉复核",
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


def text_line_items(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").splitlines()
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def optional_positive_int(value: Any, field_label: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError as exc:
        raise ValueError(f"{field_label}应填写正整数。") from exc
    if number <= 0:
        raise ValueError(f"{field_label}应填写正整数。")
    return number


def normalized_profile_preferences(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    cities = text_line_items(raw.get("cities")) or list(DEFAULT_DISCOVERY_CITIES)
    remote_policy = str(raw.get("remote_policy") or "接受").strip()
    if remote_policy not in {"接受", "仅远程", "不接受"}:
        remote_policy = "接受"
    return {
        "cities": cities,
        "min_salary_per_day": optional_positive_int(raw.get("min_salary_per_day"), "最低日薪"),
        "target_salary_per_day": optional_positive_int(raw.get("target_salary_per_day"), "目标日薪"),
        "internship_days": str(raw.get("internship_days") or "5 天左右").strip(),
        "internship_duration": str(raw.get("internship_duration") or "3 个月及以上").strip(),
        "remote_policy": remote_policy,
    }


def discovery_filters_from_profile(
    profile_data: dict[str, Any], overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    preferences = normalized_profile_preferences(loads(profile_data.get("preferences_json"), {}))
    roles = text_line_items(loads(profile_data.get("target_roles"), [])) or list(DEFAULT_DISCOVERY_ROLES)
    filters: dict[str, Any] = {"roles": roles, **preferences}
    if not overrides:
        return filters

    role = str(overrides.get("role") or "").strip()
    city = str(overrides.get("city") or "").strip()
    if role:
        filters["roles"] = [role]
    if city:
        filters["cities"] = [city]
    for field in (
        "min_salary_per_day",
        "target_salary_per_day",
        "internship_days",
        "internship_duration",
        "remote_policy",
    ):
        if field in overrides:
            filters[field] = overrides[field]
    return filters


def discovery_filters_from_form(form: Any) -> dict[str, Any]:
    filters: dict[str, Any] = {
        "role": str(form.get("role") or "").strip(),
        "city": str(form.get("city") or "").strip(),
    }
    for field, label in (("min_salary_per_day", "最低日薪"), ("target_salary_per_day", "目标日薪")):
        if field in form:
            filters[field] = optional_positive_int(form.get(field), label)
    for field in ("internship_days", "internship_duration"):
        if field in form:
            filters[field] = str(form.get(field) or "").strip()
    if "remote_policy" in form:
        remote_policy = str(form.get("remote_policy") or "接受").strip()
        if remote_policy not in {"接受", "仅远程", "不接受"}:
            remote_policy = "接受"
        filters["remote_policy"] = remote_policy
    return filters


def update_profile_discovery_preferences(
    conn: Any, profile: Any, filters: dict[str, Any]
) -> None:
    profile_data = {key: profile[key] for key in profile.keys()}
    preferences = loads(profile_data.get("preferences_json"), {})
    if not isinstance(preferences, dict):
        preferences = {}
    if filters.get("city"):
        old_cities = text_line_items(preferences.get("cities"))
        preferences["cities"] = [filters["city"], *[city for city in old_cities if city != filters["city"]]]
    for field in (
        "min_salary_per_day",
        "target_salary_per_day",
        "internship_days",
        "internship_duration",
        "remote_policy",
    ):
        if field not in filters:
            continue
        value = filters.get(field)
        if value is None or value == "":
            preferences.pop(field, None)
        else:
            preferences[field] = value
    roles = text_line_items(loads(profile_data.get("target_roles"), []))
    if filters.get("role"):
        roles = [filters["role"], *[role for role in roles if role != filters["role"]]]
    conn.execute(
        "UPDATE candidate_profile SET target_roles = ?, preferences_json = ?, updated_at = ? WHERE id = ?",
        (dumps(roles), dumps(preferences), utc_now(), profile["id"]),
    )


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
    matching_preferences: dict[str, Any] | None = None,
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
        matching_preferences=matching_preferences,
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
    return cursor.lastrowid, analysis


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
    matching_preferences: dict[str, Any] | None = None,
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
        matching_preferences=matching_preferences,
    )
    extracted = analysis["extracted"]
    scoring = analysis["scoring"]
    messages = analysis["messages"]
    status = existing["status"] or "待确认"
    if status in {"待分析", "待确认", "已归档"}:
        status = initial_job_status(jd_text, scoring)

    now = utc_now()
    previous_company = normalized_company_name(str(existing["company"] or ""))
    updated_company = normalized_company_name(str(extracted.get("company") or company))
    if previous_company != updated_company:
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


def controlled_job_discovery_plan(
    conn: Any,
    page_limit: int = JOB_DISCOVERY_SEARCH_PAGE_LIMIT,
    filters: dict[str, Any] | None = None,
) -> tuple[list[dict[str, str]], int | None]:
    profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
    profile_data = {key: profile[key] for key in profile.keys()} if profile else {}
    effective_filters = discovery_filters_from_profile(profile_data, filters)
    roles = effective_filters["roles"]
    cities = effective_filters["cities"]

    resume = conn.execute("SELECT id FROM resume_versions ORDER BY is_default DESC, id LIMIT 1").fetchone()
    history = conn.execute(
        """
        SELECT decision_json
        FROM agent_action_logs
        WHERE action_type = ? AND status IN ('完成', '部分完成')
        ORDER BY id DESC
        LIMIT 1
        """,
        ("job_discovery",),
    ).fetchone()
    role_index = 0
    city_index = 0
    explicit_role_or_city = bool(filters and (filters.get("role") or filters.get("city")))
    if history and not explicit_role_or_city:
        decision = loads(history["decision_json"], {})
        previous_plan = decision.get("plan", []) if isinstance(decision, dict) else []
        previous = previous_plan[0] if isinstance(previous_plan, list) and previous_plan else {}
        previous_role = str(previous.get("keyword") or "") if isinstance(previous, dict) else ""
        previous_city = str(previous.get("city") or "") if isinstance(previous, dict) else ""
        if previous_role in roles:
            role_index = (roles.index(previous_role) + 1) % len(roles)
            if previous_city in cities:
                city_index = cities.index(previous_city)
            if role_index == 0:
                city_index = (city_index + 1) % len(cities)
    role = roles[role_index]
    city = cities[city_index]
    plan = []
    for offset in range(max(1, min(int(page_limit), len(JOB_DISCOVERY_PLATFORMS)))):
        plan.append(
            {
                "platform": JOB_DISCOVERY_PLATFORMS[offset],
                "keyword": role,
                "city": city,
            }
        )
    return plan, int(resume["id"]) if resume else None


def discovery_candidate_screening(
    candidate: dict[str, Any], matching_preferences: dict[str, Any] | None = None
) -> tuple[bool, str]:
    title = str(candidate.get("title") or "").strip()
    summary = str(candidate.get("summary") or "").strip()
    text = f"{title} {summary}".lower()
    if not title or title == "候选岗位":
        return False, "搜索结果缺少可识别的岗位名称，未自动读取 JD；可手动导入复核。"
    if any("\ue000" <= char <= "\uf8ff" for char in f"{title}\n{summary}"):
        return False, "搜索结果包含无法识别的字体字符，未自动读取 JD；可手动导入复核。"
    senior_experience = JOB_DISCOVERY_SENIOR_EXPERIENCE_RE.search(f"{title}\n{summary}")
    if senior_experience:
        return False, f"搜索信息显示需要 {senior_experience.group(0)}，不符合在校实习初筛；未读取 JD，可手动导入复核。"
    if any(signal.lower() in text for signal in JOB_DISCOVERY_NON_ENGINEERING_SIGNALS):
        return False, "自动初筛识别为非研发方向，未读取 JD；可手动导入复核。"
    strong_match = any(signal.lower() in text for signal in JOB_DISCOVERY_STRONG_ROLE_SIGNALS)
    secondary_match = any(signal.lower() in text for signal in JOB_DISCOVERY_SECONDARY_ROLE_SIGNALS)
    if strong_match or (secondary_match and any(word in text for word in ("开发", "工程", "研发", "实习", "后端"))):
        preferences = matching_preferences if isinstance(matching_preferences, dict) else {}
        minimum_daily_salary = preferences.get("min_salary_per_day")
        minimum_daily_salary = minimum_daily_salary if isinstance(minimum_daily_salary, int) and minimum_daily_salary > 0 else None
        salary_text = extract_salary(str(candidate.get("summary") or ""))
        daily_bounds = daily_salary_bounds(salary_text)
        if daily_bounds and minimum_daily_salary and daily_bounds[0] < minimum_daily_salary:
            return (
                False,
                f"搜索摘要日薪最低 {daily_bounds[0]} 元/天，低于本轮底线 {minimum_daily_salary} 元/天；未读取 JD，可手动导入复核。",
            )
        return True, ""
    return False, "自动初筛未识别到 AI/Agent/大模型方向，未读取 JD；可手动导入复核。"


def discovery_candidate_priority(candidate: dict[str, Any]) -> int:
    """Rank clear AI-development candidates before consuming the bounded JD quota."""
    title = str(candidate.get("title") or "").lower()
    summary = str(candidate.get("summary") or "").lower()
    score = 0
    for signal in JOB_DISCOVERY_STRONG_ROLE_SIGNALS:
        if signal.lower() in title:
            score += 30
        elif signal.lower() in summary:
            score += 8
    for signal in JOB_DISCOVERY_SECONDARY_ROLE_SIGNALS:
        if signal.lower() in title:
            score += 8
        elif signal.lower() in summary:
            score += 3
    if any(word in title for word in ("开发", "工程师", "后端", "研发", "实习")):
        score += 6
    if str(candidate.get("company") or "").strip():
        score += 2
    return score


def import_discovery_candidate(
    candidate_id: int,
    resume_id: int | None,
    matching_preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_candidates WHERE id = ?", (candidate_id,)).fetchone()
    if not row:
        return {"candidate_id": candidate_id, "status": "缺失", "note": "候选岗位已不存在。"}

    candidate = {key: row[key] for key in row.keys()}
    if candidate.get("job_id"):
        return {"candidate_id": candidate_id, "status": "已存在", "job_id": int(candidate["job_id"]), "note": "候选岗位已有关联分析。"}

    try:
        fetched, detail_metadata = fetch_discovery_candidate_detail(candidate)
    except Exception as exc:
        note = str(exc)[:500]
        with connect() as conn:
            conn.execute(
                "UPDATE job_candidates SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                ("详情待补充", note, utc_now(), candidate_id),
            )
        return {"candidate_id": candidate_id, "status": "详情待补充", "note": note}

    try:
        with connect() as conn:
            existing_job = find_existing_job_by_source_url(conn, fetched.final_url)
            if existing_job:
                job_id = int(existing_job["id"])
                refresh_job_record(
                    conn,
                    job_id,
                    jd_text=fetched.text,
                    resume_id=resume_id,
                    platform=str(candidate.get("platform") or infer_platform_from_url(fetched.final_url)),
                    source_url=fetched.final_url,
                    title=fetched.title or str(candidate.get("title") or ""),
                    company=str(candidate.get("company") or ""),
                    city=str(candidate.get("city") or ""),
                    search_depth="auto",
                    generate_messages=False,
                    matching_preferences=matching_preferences,
                )
                event_type = "岗位发现刷新"
            else:
                job_id, _analysis = create_job_record(
                    conn,
                    jd_text=fetched.text,
                    resume_id=resume_id,
                    platform=str(candidate.get("platform") or infer_platform_from_url(fetched.final_url)),
                    source_url=fetched.final_url,
                    title=fetched.title or str(candidate.get("title") or ""),
                    company=str(candidate.get("company") or ""),
                    city=str(candidate.get("city") or ""),
                    search_depth="auto",
                    generate_messages=False,
                    matching_preferences=matching_preferences,
                )
                event_type = "岗位发现导入"
            linked_count = link_candidates_to_job(conn, fetched.final_url, job_id)
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (
                    job_id,
                    event_type,
                    f"通过受控 Edge 读取岗位详情并完成评分，关联 {linked_count} 条搜索候选。{fetched.note}",
                    utc_now(),
                ),
            )
        return {
            "candidate_id": candidate_id,
            "status": "已导入",
            "job_id": job_id,
            "fetch_mode": fetched.fetch_mode,
            **detail_metadata,
            "note": "岗位详情已读取并完成评分。",
        }
    except Exception as exc:
        note = str(exc)[:500]
        with connect() as conn:
            conn.execute(
                "UPDATE job_candidates SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                ("导入失败", note, utc_now(), candidate_id),
            )
        return {"candidate_id": candidate_id, "status": "导入失败", "note": note}


def run_controlled_job_discovery(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    with connect() as conn:
        profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        profile_data = {key: profile[key] for key in profile.keys()} if profile else {}
        effective_filters = discovery_filters_from_profile(profile_data, filters)
        plan, resume_id = controlled_job_discovery_plan(conn, filters=filters)

    run_ids: list[int] = []
    import_candidates: list[tuple[int, int]] = []
    search_errors: list[str] = []
    screened_out_count = 0
    salary_screened_out_count = 0
    candidate_count = 0
    controlled_edge_retry_count = 0
    visual_review_count = 0
    visual_reconciled_count = 0
    visual_review_failure_count = 0
    seen_urls: set[str] = set()
    for item in plan:
        try:
            result = search_jobs_in_controlled_edge(
                item["platform"],
                item["keyword"],
                item["city"],
                limit=JOB_DISCOVERY_CANDIDATE_LIMIT,
            )
            controlled_edge_retry_count += int(getattr(result, "retry_count", 0) or 0)
            if search_result_needs_visual_review(result):
                visual_result = run_visual_page_review(
                    "viewport",
                    f"受控岗位发现：{item['keyword']}，{item['city']}。请补充当前搜索页中 DOM 未可靠提取的候选字段。",
                    expected_url=result.search_url,
                    platform=item["platform"],
                )
                if visual_result.get("status") == "已完成":
                    visual_review_count += 1
                    review = visual_result.get("review") if isinstance(visual_result.get("review"), dict) else {}
                    reconciled = reconcile_search_result_with_visual_review(result, review)
                    visual_reconciled_count += reconciled
                    if reconciled:
                        result.note = (result.note + f" 视觉复核补全 {reconciled} 条已匹配候选字段。").strip()
                    elif not result.candidates and review.get("candidate_jobs"):
                        result.note = (result.note + " 视觉复核识别到候选信息，但没有稳定岗位链接，未自动导入。").strip()
                elif visual_result.get("status") not in {"未配置"}:
                    visual_review_failure_count += 1
            run_id = save_search_result(result)
        except Exception as exc:
            note = str(exc)[:500]
            run_id = save_search_failure(item["platform"], item["keyword"], item["city"], "msedge", note)
            search_errors.append(note)
            run_ids.append(run_id)
            continue

        run_ids.append(run_id)
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, title, city, summary, source_url FROM job_candidates WHERE search_run_id = ? ORDER BY id",
                (run_id,),
        ).fetchall()
        for row in rows:
            candidate = {key: row[key] for key in row.keys()}
            source_url = str(row["source_url"] or "")
            comparable = comparable_source_url(source_url)
            key = min(comparable) if comparable else source_url
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            candidate_count += 1
            should_import, screening_note = discovery_candidate_screening(candidate, effective_filters)
            if not should_import:
                with connect() as conn:
                    is_salary_filter = screening_note.startswith("搜索摘要日薪最低")
                    conn.execute(
                        "UPDATE job_candidates SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                        ("初筛跳过" if is_salary_filter else "初筛待确认", screening_note, utc_now(), int(row["id"])),
                )
                screened_out_count += 1
                salary_screened_out_count += int(is_salary_filter)
                continue
            import_candidates.append((discovery_candidate_priority(candidate), int(row["id"])))

    import_candidates.sort(key=lambda item: (-item[0], item[1]))
    import_results = [
        import_discovery_candidate(candidate_id, resume_id, effective_filters)
        for _priority, candidate_id in import_candidates[:JOB_DISCOVERY_IMPORT_LIMIT]
    ]
    imported_count = sum(1 for item in import_results if item["status"] == "已导入")
    pending_count = sum(1 for item in import_results if item["status"] == "详情待补充")
    failed_count = sum(1 for item in import_results if item["status"] == "导入失败")
    visual_detail_fallback_count = sum(1 for item in import_results if item.get("visual_detail_fallback"))
    if search_errors and len(search_errors) == len(plan):
        status = "失败"
    elif search_errors or failed_count:
        status = "部分完成"
    else:
        status = "完成"
    note = (
        f"完成 {len(run_ids)} 个受控搜索页，识别 {candidate_count} 个去重候选，"
        f"自动评分 {imported_count} 个岗位。"
    )
    if screened_out_count:
        note += f" {screened_out_count} 个候选未读取 JD。"
    if salary_screened_out_count:
        note += f" 其中 {salary_screened_out_count} 个搜索摘要已明确低于日薪底线。"
    if pending_count:
        note += f" {pending_count} 个详情页待补充。"
    if failed_count:
        note += f" {failed_count} 个导入失败。"
    if search_errors:
        note += f" {len(search_errors)} 个搜索页失败。"
    if controlled_edge_retry_count:
        note += f" 受控 Edge 已自动恢复 {controlled_edge_retry_count} 次短暂连接中断。"
    if visual_review_count:
        note += f" 视觉复核 {visual_review_count} 个字段异常搜索页，补全 {visual_reconciled_count} 条候选字段。"
    if visual_review_failure_count:
        note += f" {visual_review_failure_count} 个视觉复核失败，已保留 DOM 结果供人工复核。"
    if visual_detail_fallback_count:
        note += f" {visual_detail_fallback_count} 个详情页通过视觉复核补充 JD。"

    with connect() as conn:
        log_agent_action(
            conn,
            action_type="job_discovery",
            status=status,
            summary=note,
            decision={
                "search_page_limit": JOB_DISCOVERY_SEARCH_PAGE_LIMIT,
                "detail_import_limit": JOB_DISCOVERY_IMPORT_LIMIT,
                "plan": plan,
                "effective_filters": effective_filters,
                "run_ids": run_ids,
                "candidate_count": candidate_count,
                "auto_import_candidate_count": len(import_candidates),
                "screened_out_count": screened_out_count,
                "salary_screened_out_count": salary_screened_out_count,
                "imported_count": imported_count,
                "pending_detail_count": pending_count,
                "failed_import_count": failed_count,
                "failed_search_count": len(search_errors),
                "controlled_edge_retry_count": controlled_edge_retry_count,
                "visual_review_count": visual_review_count,
                "visual_reconciled_count": visual_reconciled_count,
                "visual_review_failure_count": visual_review_failure_count,
                "visual_detail_fallback_count": visual_detail_fallback_count,
                "auto_apply": False,
                "auto_message": False,
                "message_text_saved": False,
            },
            error_message="；".join(search_errors)[:500],
        )
    return {
        "status": status,
        "note": note,
        "run_ids": run_ids,
        "candidate_count": candidate_count,
        "auto_import_candidate_count": len(import_candidates),
        "screened_out_count": screened_out_count,
        "salary_screened_out_count": salary_screened_out_count,
        "imported_count": imported_count,
        "pending_detail_count": pending_count,
        "failed_import_count": failed_count,
        "failed_search_count": len(search_errors),
        "visual_review_count": visual_review_count,
        "visual_reconciled_count": visual_reconciled_count,
        "visual_review_failure_count": visual_review_failure_count,
        "visual_detail_fallback_count": visual_detail_fallback_count,
        "import_results": import_results,
    }


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
    # Chat pages often place the company name in the tab title rather than the visible message area.
    clean_text = "\n".join(item for item in [page_title, text] if item)
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
            j.risk_level AS job_risk_level,
            j.analysis_source AS job_analysis_source,
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
                "job_risk_level": str(row["job_risk_level"] or ""),
                "job_analysis_source": str(row["job_analysis_source"] or ""),
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


def run_controlled_edge_chat_page_calibration(trigger_type: str = "manual_browser") -> dict[str, Any]:
    try:
        result = calibrate_controlled_edge_chat_pages()
    except ValueError as exc:
        result = {
            "status": "浏览器未连接",
            "note": str(exc)[:500],
            "browser_connected": False,
            "checked_page_count": 0,
            "candidate_chat_count": 0,
            "structure_ready_count": 0,
            "review_count": 0,
            "sensitive_count": 0,
            "results": [],
            "page_text_saved": False,
            "page_url_saved": False,
            "page_title_saved": False,
            "browser_clicked": False,
            "message_filled": False,
        }
    with connect() as conn:
        log_agent_action(
            conn,
            action_type="communication_browser_calibration",
            status=str(result["status"]),
            summary=str(result["note"]),
            decision={
                "dry_run": True,
                "trigger_type": trigger_type,
                "browser_connected": bool(result.get("browser_connected")),
                "checked_page_count": int(result.get("checked_page_count") or 0),
                "candidate_chat_count": int(result.get("candidate_chat_count") or 0),
                "structure_ready_count": int(result.get("structure_ready_count") or 0),
                "review_count": int(result.get("review_count") or 0),
                "sensitive_count": int(result.get("sensitive_count") or 0),
                "unsupported_count": int(result.get("unsupported_count") or 0),
                "results": result.get("results", []),
                "page_text_saved": False,
                "page_url_saved": False,
                "page_title_saved": False,
                "browser_clicked": False,
                "message_filled": False,
            },
        )
    return result


def run_unread_conversation_scan(trigger_type: str = "manual_browser") -> dict[str, Any]:
    try:
        result = scan_controlled_edge_unread_conversations()
    except ValueError as exc:
        result = {
            "status": "浏览器未连接",
            "note": str(exc)[:500],
            "checked_page_count": 0,
            "message_list_page_count": 0,
            "unread_count": 0,
            "error_count": 0,
            "detector_version": "message-list-v1",
            "results": [],
            "page_text_saved": False,
            "page_url_saved": False,
            "page_title_saved": False,
            "conversation_opened": False,
            "browser_clicked": False,
            "message_filled": False,
            "message_sent": False,
        }
    with connect() as conn:
        now = utc_now()
        for item in result.get("results", []):
            if not isinstance(item, dict):
                continue
            conn.execute(
                """
                INSERT INTO unread_conversation_scans (
                    platform, status, trigger_type, checked_page_count, message_list_page_count,
                    unread_count, unread_badge_count, signal_types_json, detector_version, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(item.get("platform") or ""),
                    str(item.get("status") or ""),
                    trigger_type,
                    1,
                    1 if item.get("message_list_candidate") else 0,
                    int(item.get("unread_count") or 0),
                    int(item.get("unread_badge_count") or 0),
                    dumps(item.get("signal_types") or []),
                    str(result.get("detector_version") or "message-list-v1"),
                    "只读结构扫描；未读取或保存会话正文、名称、标题、链接。",
                    now,
                ),
            )
        log_agent_action(
            conn,
            action_type="unread_conversation_scan",
            status=str(result["status"]),
            summary=str(result["note"]),
            decision={
                "trigger_type": trigger_type,
                "checked_page_count": int(result.get("checked_page_count") or 0),
                "message_list_page_count": int(result.get("message_list_page_count") or 0),
                "unread_count": int(result.get("unread_count") or 0),
                "error_count": int(result.get("error_count") or 0),
                "detector_version": str(result.get("detector_version") or ""),
                "results": result.get("results", []),
                "page_text_saved": False,
                "page_url_saved": False,
                "page_title_saved": False,
                "conversation_opened": False,
                "browser_clicked": False,
                "message_filled": False,
                "message_sent": False,
                "model_called": False,
            },
        )
    return result


def run_communication_browser_fill(draft_id: int, message: str) -> dict[str, Any]:
    message = str(message or "").strip()
    policy = communication_policy()
    control = automation_control()
    with connect() as conn:
        row = conn.execute(
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
            WHERE d.id = ?
            """,
            (draft_id,),
        ).fetchone()
        if not row:
            return {"status": "已阻止", "note": "没有找到这条沟通草稿。", "draft_id": draft_id, "message_filled": False}
        if policy["mode"] == "off":
            result = {"status": "已阻止", "note": "沟通模式为关闭，不能填入浏览器。", "draft_id": draft_id, "message_filled": False}
        elif control["paused"]:
            result = {"status": "已阻止", "note": "自动化已暂停，恢复后才能填入浏览器。", "draft_id": draft_id, "message_filled": False}
        else:
            gate = evaluate_draft_send_gate(conn, row, message)
            plan_item = {
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
                "message_length": len(message),
                "gate_allowed": bool(gate["allowed"]),
                "gate_reasons": gate["reasons"],
                "job_status": str(row["job_status"] or ""),
                "job_recommendation": str(row["job_recommendation"] or ""),
                "job_match_level": str(row["job_match_level"] or ""),
                "capture_message_type": str(row["capture_message_type"] or ""),
            }
            browser_plan = build_browser_send_adapter_plan(
                {
                    "ok": True,
                    "dry_run": True,
                    "trigger_type": "manual_fill",
                    "status": "演练完成",
                    "note": "单条草稿浏览器填入准备。",
                    "policy_mode": policy["mode"],
                    "candidate_count": 1,
                    "allowed_count": 1 if gate["allowed"] else 0,
                    "blocked_count": 0 if gate["allowed"] else 1,
                    "plans": [plan_item],
                }
            )["browser_plans"][0]
            if not gate["allowed"]:
                result = {
                    "status": "已阻止",
                    "note": "发送闸门拦截：" + "；".join(gate["reasons"]),
                    "draft_id": draft_id,
                    "gate": gate,
                    "browser_plan": browser_plan,
                    "message_filled": False,
                }
            else:
                result = {"status": "待填入", "draft_id": draft_id, "gate": gate, "browser_plan": browser_plan, "message_filled": False}

        if result["status"] == "已阻止":
            log_agent_action(
                conn,
                action_type="communication_browser_fill",
                status="已阻止",
                summary=str(result["note"])[:500],
                platform=str(row["platform"] or ""),
                job_id=int(row["job_id"]) if row["job_id"] else None,
                capture_id=int(row["capture_id"]) if row["capture_id"] else None,
                draft_id=draft_id,
                decision={
                    "message_filled": False,
                    "browser_clicked": False,
                    "message_text_saved": False,
                    "gate": result.get("gate") or {"checked": False},
                },
            )
            return result

    try:
        browser_result = fill_message_in_controlled_edge(result["browser_plan"], message)
        result = {**result, **browser_result}
    except ValueError as exc:
        result = {**result, "status": "未填入", "note": str(exc)[:500], "message_filled": False, "browser_clicked": False}

    with connect() as conn:
        current = conn.execute("SELECT * FROM message_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not current:
            return {**result, "status": "未填入", "note": "草稿已不存在，未保留浏览器填入结果。", "message_filled": False}
        if result["status"] == "已填入":
            conn.execute("UPDATE message_drafts SET message = ?, updated_at = ? WHERE id = ?", (message, utc_now(), draft_id))
        log_agent_action(
            conn,
            action_type="communication_browser_fill",
            status=str(result["status"]),
            summary=str(result.get("note") or "浏览器草稿填入完成。")[:500],
            platform=str(current["platform"] or ""),
            job_id=int(current["job_id"]) if current["job_id"] else None,
            capture_id=int(current["capture_id"]) if current["capture_id"] else None,
            draft_id=draft_id,
            decision={
                "message_filled": bool(result.get("message_filled")),
                "browser_clicked": False,
                "message_text_saved": False,
                "filled_selector": str(result.get("filled_selector") or ""),
                "matched_page": result.get("matched_page") or {},
                "gate": result.get("gate") or {"checked": True},
            },
            error_message=str(result.get("note") or "") if result["status"] != "已填入" else "",
        )
    return result


def load_communication_draft_with_context(conn: Any, draft_id: int) -> Any:
    return conn.execute(
        """
        SELECT
            d.*,
            j.title AS job_title,
            j.company AS company,
            j.source_url AS job_source_url,
            j.status AS job_status,
            j.recommendation AS job_recommendation,
            j.match_level AS job_match_level,
            j.risk_level AS job_risk_level,
            j.analysis_source AS job_analysis_source,
            c.message_type AS capture_message_type,
            c.source_url AS capture_source_url
        FROM message_drafts d
        LEFT JOIN job_postings j ON j.id = d.job_id
        LEFT JOIN conversation_captures c ON c.id = d.capture_id
        WHERE d.id = ?
        """,
        (draft_id,),
    ).fetchone()


def evaluate_autonomous_send_gate(conn: Any, draft: Any, message: str) -> dict[str, Any]:
    policy = communication_policy()
    gate = dict(evaluate_draft_send_gate(conn, draft, message))
    reasons = list(gate["reasons"])
    job_id = int(draft["job_id"]) if draft["job_id"] else None
    platform = str(draft["platform"] or "")

    if policy["mode"] != "autonomous":
        reasons.append("沟通模式不是自主询问模式")
    if automation_control()["paused"]:
        reasons.append("自动化已暂停")
    if str(draft["communication_mode"] or "") != "autonomous":
        reasons.append("草稿不是在自主询问模式下生成")
    if str(draft["draft_type"] or "") != "自主询问候选":
        reasons.append("草稿不是自主询问候选")
    if str(draft["job_recommendation"] or "") not in APPLICATION_ELIGIBLE_RECOMMENDATIONS:
        reasons.append("岗位当前不属于必投或可投递")
    if str(draft["job_risk_level"] or "") not in APPLICATION_LOW_RISK_LEVELS:
        reasons.append("岗位风险不是低或低风险")
    if str(draft["job_analysis_source"] or "") == "local_demo":
        reasons.append("本地演练数据禁止自动发送")
    if int(draft["followup_index"] or 0) <= 0 or int(draft["followup_index"] or 0) > policy["max_auto_followups"]:
        reasons.append("自主询问轮次不在允许范围内")

    text = str(message or "")
    for token in AUTONOMOUS_MESSAGE_BLOCKING_TEXT:
        if token in text:
            reasons.append(f"消息包含需要人工确认的内容：{token}")

    day_start = f"{utc_now()[:10]}T00:00:00"
    sent_today = int(
        conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM message_drafts
            WHERE status = '已发送' AND communication_mode = 'autonomous' AND updated_at >= ?
            """,
            (day_start,),
        ).fetchone()["count"]
    )
    sent_on_platform = int(
        conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM message_drafts
            WHERE status = '已发送' AND communication_mode = 'autonomous'
              AND platform = ? AND updated_at >= ?
            """,
            (platform, day_start),
        ).fetchone()["count"]
    )
    if sent_today >= policy["daily_send_limit"]:
        reasons.append(f"今日自主沟通已达到 {policy['daily_send_limit']} 条上限")
    if sent_on_platform >= policy["platform_daily_send_limit"]:
        reasons.append(f"该平台今日自主沟通已达到 {policy['platform_daily_send_limit']} 条上限")

    return {
        **gate,
        "allowed": not reasons,
        "reasons": dedupe_texts(reasons),
        "autonomous": True,
        "daily_send_limit": policy["daily_send_limit"],
        "platform_daily_send_limit": policy["platform_daily_send_limit"],
        "sent_today": sent_today,
        "sent_on_platform": sent_on_platform,
        "job_id": job_id,
    }


def browser_plan_for_communication_draft(draft: Any, message: str, gate: dict[str, Any], *, trigger_type: str) -> dict[str, Any]:
    plan_item = {
        "draft_id": int(draft["id"]),
        "job_id": int(draft["job_id"]) if draft["job_id"] else None,
        "platform": str(draft["platform"] or ""),
        "company": str(draft["company"] or ""),
        "job_title": str(draft["job_title"] or ""),
        "source_url": str(draft["capture_source_url"] or draft["job_source_url"] or ""),
        "draft_type": str(draft["draft_type"] or ""),
        "communication_mode": str(draft["communication_mode"] or ""),
        "followup_index": int(draft["followup_index"] or 0),
        "followup_limit": int(draft["followup_limit"] or 0),
        "message_length": len(message),
        "gate_allowed": bool(gate["allowed"]),
        "gate_reasons": gate["reasons"],
        "job_status": str(draft["job_status"] or ""),
        "job_recommendation": str(draft["job_recommendation"] or ""),
        "job_match_level": str(draft["job_match_level"] or ""),
        "job_risk_level": str(draft["job_risk_level"] or ""),
        "capture_message_type": str(draft["capture_message_type"] or ""),
    }
    return build_browser_send_adapter_plan(
        {
            "ok": True,
            "dry_run": False,
            "trigger_type": trigger_type,
            "status": "执行准备",
            "note": "单条自主沟通发送准备。",
            "policy_mode": "autonomous",
            "candidate_count": 1,
            "allowed_count": 1 if gate["allowed"] else 0,
            "blocked_count": 0 if gate["allowed"] else 1,
            "plans": [plan_item],
        }
    )["browser_plans"][0]


def run_autonomous_draft_send(draft_id: int, trigger_type: str) -> dict[str, Any]:
    with connect() as conn:
        draft = load_communication_draft_with_context(conn, draft_id)
        if not draft:
            return {"status": "已阻止", "note": "没有找到这条自主沟通草稿。", "draft_id": draft_id, "browser_clicked": False}
        message = str(draft["message"] or "").strip()
        gate = evaluate_autonomous_send_gate(conn, draft, message)
        browser_plan = browser_plan_for_communication_draft(draft, message, gate, trigger_type=trigger_type)
        if not gate["allowed"]:
            result = {
                "status": "已阻止",
                "note": "自主沟通闸门拦截：" + "；".join(gate["reasons"]),
                "draft_id": draft_id,
                "gate": gate,
                "browser_plan": browser_plan,
                "message_filled": False,
                "browser_clicked": False,
            }
            log_agent_action(
                conn,
                action_type="communication_autonomous_send",
                status=result["status"],
                summary=result["note"][:500],
                platform=str(draft["platform"] or ""),
                job_id=int(draft["job_id"]) if draft["job_id"] else None,
                capture_id=int(draft["capture_id"]) if draft["capture_id"] else None,
                draft_id=draft_id,
                decision={
                    "trigger_type": trigger_type,
                    "gate": gate,
                    "message_filled": False,
                    "browser_clicked": False,
                    "message_text_saved": False,
                },
            )
            return result

    try:
        result = send_message_in_controlled_edge(browser_plan, message)
    except ValueError as exc:
        result = {
            "status": "未发送",
            "note": str(exc)[:500],
            "draft_id": draft_id,
            "gate": gate,
            "message_filled": False,
            "browser_clicked": False,
        }

    with connect() as conn:
        current = load_communication_draft_with_context(conn, draft_id)
        if not current:
            return {**result, "status": "未发送", "note": "草稿已不存在，未保留浏览器发送结果。", "browser_clicked": False}
        if result["status"] == "已发送":
            updated = conn.execute(
                "UPDATE message_drafts SET status = ?, updated_at = ? WHERE id = ? AND status = '待确认'",
                ("已发送", utc_now(), draft_id),
            )
            if updated.rowcount:
                event = f"Agent 已在受控 Edge 自主发送第 {int(current['followup_index'] or 0)} 轮岗位相关询问。"
                conn.execute(
                    "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                    (current["job_id"], "自主沟通已发送", event, utc_now()),
                )
            else:
                result = {
                    **result,
                    "status": "已发送待核对",
                    "note": "浏览器已点击发送，但本地草稿状态已变化，未覆盖原状态。",
                }
        log_agent_action(
            conn,
            action_type="communication_autonomous_send",
            status=str(result["status"]),
            summary=str(result.get("note") or "自主沟通发送完成。")[:500],
            platform=str(current["platform"] or ""),
            job_id=int(current["job_id"]) if current["job_id"] else None,
            capture_id=int(current["capture_id"]) if current["capture_id"] else None,
            draft_id=draft_id,
            decision={
                "trigger_type": trigger_type,
                "gate": gate,
                "message_filled": bool(result.get("message_filled")),
                "browser_clicked": bool(result.get("browser_clicked")),
                "filled_selector": str(result.get("filled_selector") or ""),
                "send_selector": str(result.get("send_selector") or ""),
                "matched_page": result.get("matched_page") or {},
                "message_text_saved": False,
            },
            error_message=str(result.get("note") or "") if result["status"] not in {"已发送", "已发送待核对"} else "",
        )
    return result


def run_autonomous_communication_executor(trigger_type: str) -> dict[str, Any]:
    policy = communication_policy()
    control = automation_control()
    if policy["mode"] != "autonomous":
        return {"status": "未启用", "note": "沟通模式不是自主询问模式。", "candidate_count": 0, "sent_count": 0, "blocked_count": 0, "failed_count": 0}
    if control["paused"]:
        return {"status": "已暂停", "note": "自动化已暂停，未执行自主沟通。", "candidate_count": 0, "sent_count": 0, "blocked_count": 0, "failed_count": 0}

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id FROM message_drafts
            WHERE status = '待确认' AND communication_mode = 'autonomous' AND draft_type = '自主询问候选'
            ORDER BY created_at ASC, id ASC
            LIMIT ?
            """,
            (COMMUNICATION_EXECUTOR_PLAN_LIMIT,),
        ).fetchall()
    results = [run_autonomous_draft_send(int(row["id"]), trigger_type) for row in rows]
    sent_count = sum(1 for item in results if item["status"] in {"已发送", "已发送待核对"})
    blocked_count = sum(1 for item in results if item["status"] == "已阻止")
    failed_count = len(results) - sent_count - blocked_count
    if not results:
        status = "无候选"
        note = "没有待发送的自主询问候选。"
    elif failed_count:
        status = "部分完成"
        note = f"已发送 {sent_count} 条，拦截 {blocked_count} 条，失败 {failed_count} 条。"
    else:
        status = "执行完成"
        note = f"已发送 {sent_count} 条，拦截 {blocked_count} 条。"

    with connect() as conn:
        log_agent_action(
            conn,
            action_type="communication_autonomous_executor",
            status=status,
            summary=note,
            decision={
                "trigger_type": trigger_type,
                "candidate_count": len(results),
                "sent_count": sent_count,
                "blocked_count": blocked_count,
                "failed_count": failed_count,
                "results": [
                    {
                        "draft_id": item.get("draft_id"),
                        "status": item.get("status"),
                        "browser_clicked": bool(item.get("browser_clicked")),
                    }
                    for item in results
                ],
                "message_text_saved": False,
            },
        )
    return {
        "status": status,
        "note": note,
        "candidate_count": len(results),
        "sent_count": sent_count,
        "blocked_count": blocked_count,
        "failed_count": failed_count,
        "results": results,
    }


def communication_executor_page_status(conn: Any) -> dict[str, Any]:
    preview = build_communication_execution_plan(conn, trigger_type="page_preview")
    latest_rows = conn.execute(
        """
        SELECT id, action_type, status, summary, decision_json, created_at
        FROM agent_action_logs
        WHERE action_type IN (
            'communication_executor_dry_run',
            'communication_browser_dry_run',
            'communication_browser_probe',
            'communication_browser_fill'
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
                f"当前为自主询问模式，已发送 {sent_count}/{max_followups} 轮；仅通过全部安全闸门时允许受控发送。",
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
        autonomous_candidates = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM message_drafts
            WHERE status = '待确认' AND communication_mode = 'autonomous' AND draft_type = '自主询问候选'
            """
        ).fetchone()["count"]
        discovery_plan, _resume_id = controlled_job_discovery_plan(conn)
    communication = communication_policy()
    control = automation_control()
    patrol = message_patrol_policy()
    workflow_active = bool(
        communication["mode"] == "autonomous" and not control["paused"] and patrol["enabled"]
    )
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "counts": counts,
            "recent_jobs": recent_jobs,
            "resumes": resumes,
            "token_stats": token_stats(),
            "communication_policy": communication,
            "automation_control": control,
            "message_patrol_policy": patrol,
            "workflow_active": workflow_active,
            "autonomous_candidates": autonomous_candidates,
            "discovery_plan": discovery_plan,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


def control_suggestions(intent_type: str, filters: dict[str, Any]) -> list[dict[str, str]]:
    if intent_type == "search_draft":
        return [{"label": "查看岗位搜索", "url": "/searches"}]
    if intent_type == "stats":
        return [{"label": "查看岗位列表", "url": "/jobs"}, {"label": "查看投递准备", "url": "/applications"}]
    if intent_type == "list_jobs":
        return [{"label": "查看岗位列表", "url": "/jobs"}]
    if intent_type == "explain_job" and filters.get("job_id"):
        return [{"label": "打开岗位", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "compare_jobs":
        job_ids = [int(job_id) for job_id in filters.get("job_ids") or [] if isinstance(job_id, int) and job_id > 0]
        return [{"label": f"打开岗位 #{job_id}", "url": f"/jobs/{job_id}"} for job_id in job_ids]
    if intent_type == "prepare_interview":
        return [{"label": "查看面试准备", "url": "/interviews"}]
    if intent_type == "update_job_status" and filters.get("job_id"):
        return [{"label": "打开岗位", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "next_job_action" and filters.get("job_id"):
        return [{"label": "打开岗位", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "resume_readiness":
        suggestions = [{"label": "候选人画像", "url": "/resumes"}]
        if filters.get("job_id"):
            suggestions.append({"label": "打开岗位", "url": f"/jobs/{filters['job_id']}"})
        return suggestions
    if intent_type == "scan_unread_conversations":
        return [{"label": "查看沟通草稿", "url": "/communications"}]
    if intent_type == "review_visual_page":
        return [{"label": "查看岗位列表", "url": "/jobs"}, {"label": "查看岗位搜索", "url": "/searches"}]
    if intent_type == "job_match_review" and filters.get("job_id"):
        return [{"label": "查看深度复核", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "company_research" and filters.get("job_id"):
        return [{"label": "查看公司来源", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "select_job" and filters.get("job_id"):
        return [{"label": "打开岗位", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "prepare_application" and filters.get("job_id"):
        return [{"label": "查看投递准备", "url": "/applications"}, {"label": "打开岗位", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "prepare_communication" and filters.get("job_id"):
        return [{"label": "查看沟通草稿", "url": "/communications#pending-drafts"}, {"label": "打开岗位", "url": f"/jobs/{filters['job_id']}"}]
    if intent_type == "ignore_broadcast":
        return [{"label": "查看沟通记录", "url": "/communications"}]
    return [{"label": "岗位搜索", "url": "/searches"}, {"label": "岗位列表", "url": "/jobs"}]


def create_control_plan(conn: Any, conversation_id: int, action_type: str, summary: str, payload: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO control_plans (conversation_id, action_type, status, summary, payload_json, created_at)
        VALUES (?, ?, '待确认', ?, ?, ?)
        """,
        (conversation_id, action_type, summary, dumps(payload), utc_now()),
    )
    return int(cursor.lastrowid)


def create_completed_control_execution(
    conn: Any,
    conversation_id: int,
    action_type: str,
    summary: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    status: str,
) -> int:
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO control_plans (
            conversation_id, action_type, status, summary, payload_json, result_json,
            created_at, confirmed_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (conversation_id, action_type, status, summary, dumps(payload), dumps(result), now, now, now),
    )
    return int(cursor.lastrowid)


def control_event(kind: str, status: str, summary: str) -> dict[str, str]:
    return {"kind": kind, "status": status, "summary": summary}


def local_job_comparison_item(job: dict[str, Any]) -> dict[str, Any]:
    scoring = (job.get("extracted") or {}).get("scoring") or {}
    return {
        "id": int(job["id"]),
        "company": str(job.get("company") or "未填写公司"),
        "title": str(job.get("title") or "未填写岗位名称"),
        "match_score": int(job.get("match_score") or 0),
        "recommendation": str(job.get("recommendation") or "待确认"),
        "risk_level": str(job.get("risk_level") or "待确认"),
        "status": str(job.get("status") or "待分析"),
        "salary_text": str(job.get("salary_text") or "未披露"),
        "matched_skills": compact_interview_items(scoring.get("matched_skills"), limit=4, item_limit=80),
        "missing_skills": compact_interview_items(scoring.get("missing_skills"), limit=3, item_limit=80),
        "risk_signals": compact_interview_items(scoring.get("risk_signals"), limit=3, item_limit=100),
        "caution_signals": compact_interview_items(scoring.get("caution_signals"), limit=3, item_limit=100),
        "skip_reason": str(job.get("skip_reason") or "").strip()[:180],
    }


def local_job_comparison_priority(item: dict[str, Any]) -> tuple[int, int, int]:
    recommendation_rank = {"必投": 3, "可冲": 2, "可投递": 2, "待确认": 1, "跳过": 0}.get(item["recommendation"], 1)
    blocked = item["risk_level"] == "高" or item["recommendation"] == "跳过" or item["status"] in {"已归档", "已忽略"}
    return (0 if blocked else 1, recommendation_rank, item["match_score"])


def compare_local_jobs(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_item = local_job_comparison_item(first)
    second_item = local_job_comparison_item(second)
    first_priority = local_job_comparison_priority(first_item)
    second_priority = local_job_comparison_priority(second_item)
    if first_priority == second_priority:
        preferred_job_id = None
        reason_codes = ["当前本地评分、建议和风险没有形成明确优先级。"]
    else:
        preferred = first_item if first_priority > second_priority else second_item
        other = second_item if preferred is first_item else first_item
        preferred_job_id = preferred["id"]
        reason_codes = []
        if local_job_comparison_priority(preferred)[0] > local_job_comparison_priority(other)[0]:
            reason_codes.append("另一岗位已归档、已忽略、标为跳过或存在高风险。")
        if preferred["recommendation"] != other["recommendation"]:
            reason_codes.append(f"本地建议为“{preferred['recommendation']}”，高于另一岗位的“{other['recommendation']}”。")
        if preferred["match_score"] != other["match_score"]:
            reason_codes.append(f"匹配分 {preferred['match_score']}，另一岗位为 {other['match_score']}。")
    return {
        "jobs": [first_item, second_item],
        "preferred_job_id": preferred_job_id,
        "reason_codes": reason_codes,
    }


def local_job_comparison_response(comparison: dict[str, Any]) -> str:
    jobs = comparison["jobs"]
    preferred_job_id = comparison.get("preferred_job_id")
    lines = []
    for item in jobs:
        facts = [
            f"岗位 #{item['id']} {item['company']} - {item['title']}：匹配分 {item['match_score']}，建议“{item['recommendation']}”，风险“{item['risk_level']}”，薪资 {item['salary_text']}。"
        ]
        if item["matched_skills"]:
            facts.append("已匹配：" + "、".join(item["matched_skills"]) + "。")
        if item["missing_skills"]:
            facts.append("待补强：" + "、".join(item["missing_skills"]) + "。")
        if item["risk_signals"] or item["caution_signals"]:
            facts.append("注意：" + "、".join((item["risk_signals"] + item["caution_signals"])[:3]) + "。")
        if item["skip_reason"]:
            facts.append("归档原因：" + item["skip_reason"] + "。")
        lines.append("".join(facts))
    if preferred_job_id:
        lines.append(f"按当前本地证据，建议优先处理岗位 #{preferred_job_id}。" + " ".join(comparison["reason_codes"]))
    else:
        lines.append("按当前本地证据无法区分优先级；建议先补全薪资、JD 或公司公开信息后再判断。")
    lines.append("本轮未调用模型、未访问浏览器、未改变岗位评分或状态，也未创建沟通或投递动作。")
    return " ".join(lines)


def local_job_list_label(filters: dict[str, Any]) -> str:
    labels = [
        str(filters.get("match_level") or ""),
        str(filters.get("recommendation") or ""),
        f"{filters['risk_level']}风险" if filters.get("risk_level") else "",
        str(filters.get("status") or ""),
    ]
    return "、".join(item for item in labels if item) or "全部已保存岗位"


def list_local_jobs(conn: Any, filters: dict[str, Any]) -> list[dict[str, Any]]:
    conditions = []
    params: list[Any] = []
    for column in ("match_level", "recommendation", "status"):
        value = str(filters.get(column) or "")
        if value:
            conditions.append(f"{column} = ?")
            params.append(value)
    risk_level = str(filters.get("risk_level") or "")
    if risk_level:
        if risk_level == "低":
            conditions.append("risk_level IN ('低', '低风险')")
        else:
            conditions.append("risk_level = ?")
            params.append(risk_level)
    where_clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    order_clause = (
        "updated_at DESC, id DESC"
        if filters.get("sort") == "recent"
        else "CASE risk_level WHEN '低' THEN 3 WHEN '低风险' THEN 3 WHEN '中' THEN 2 WHEN '中风险' THEN 2 ELSE 1 END DESC, "
        "CASE recommendation WHEN '必投' THEN 3 WHEN '可冲' THEN 2 ELSE 1 END DESC, match_score DESC, updated_at DESC, id DESC"
    )
    params.append(int(filters.get("limit") or 6))
    rows = conn.execute(
        f"""
        SELECT id, platform, title, company, city, salary_text, match_score, match_level, risk_level, recommendation, status
        FROM job_postings{where_clause}
        ORDER BY {order_clause}
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


def local_job_list_response(jobs: list[dict[str, Any]], filters: dict[str, Any]) -> str:
    label = local_job_list_label(filters)
    if not jobs:
        return f"本地没有符合“{label}”的岗位。可调整条件，或先执行岗位发现。"
    items = []
    for job in jobs:
        location = str(job.get("city") or "地点未披露")
        salary = str(job.get("salary_text") or "薪资未披露")
        items.append(
            f"#{job['id']} {job['company'] or '未填写公司'} - {job['title'] or '未填写岗位'}"
            f"（{location}，{salary}，{job['match_score']} 分，{job['recommendation'] or '待确认'}，{job['risk_level'] or '待确认'}，{job['status'] or '待分析'}）"
        )
    return (
        f"本地找到 {len(jobs)} 条“{label}”岗位，按当前本地优先级排序："
        + "；".join(items)
        + "。可继续说“选择岗位 #编号”进入后续复核或沟通准备。"
    )


def local_next_job_action(conn: Any, job_id: int) -> dict[str, Any]:
    job = conn.execute(
        "SELECT id, company, title, match_score, recommendation, risk_level, status FROM job_postings WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not job:
        return {"status": "未找到", "job_id": job_id}

    job_data = {key: job[key] for key in job.keys()}
    status = str(job_data["status"] or "待确认")
    recommendation = str(job_data["recommendation"] or "待确认")
    risk_level = str(job_data["risk_level"] or "待确认")
    application = conn.execute(
        "SELECT id, status FROM application_preparations WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    interview = conn.execute(
        "SELECT id FROM interview_preparations WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    company_research_count = conn.execute(
        "SELECT COUNT(*) AS count FROM company_research WHERE job_id = ?",
        (job_id,),
    ).fetchone()["count"]

    result: dict[str, Any] = {
        "status": "已完成",
        "job_id": job_id,
        "job_status": status,
        "recommendation": recommendation,
        "risk_level": risk_level,
        "match_score": int(job_data["match_score"] or 0),
        "application_preparation_id": int(application["id"]) if application else None,
        "interview_preparation_id": int(interview["id"]) if interview else None,
        "company_research_count": int(company_research_count or 0),
        "model_called": False,
        "browser_accessed": False,
        "job_status_changed": False,
    }
    if risk_level in {"高", "高风险"} or recommendation == "跳过":
        result.update({
            "advice_code": "manual_risk_review",
            "summary": "当前本地规则标记为高风险或建议跳过，不建议自动推进沟通或投递。",
            "next_command": "为当前岗位做深度匹配复核",
            "next_url": f"/jobs/{job_id}",
        })
    elif status == "待确认":
        result.update({
            "advice_code": "review_before_outreach",
            "summary": "先复核匹配证据和岗位风险，再决定是否准备沟通或投递。",
            "next_command": "为当前岗位做深度匹配复核",
            "next_url": f"/jobs/{job_id}",
        })
    elif status == "待投递":
        result.update({
            "advice_code": "review_application_preparation",
            "summary": "本地状态已进入待投递，请核对简历版本、附言和页面演练结果后再进行实际投递。",
            "next_command": "为当前岗位准备投递",
            "next_url": "/applications",
        })
    elif status == "已投递":
        result.update({
            "advice_code": "await_or_review_reply",
            "summary": "本地已记录投递，下一步是留意 HR 回复；没有新消息时不需要重复投递。",
            "next_command": "",
            "next_instruction": "请在沟通草稿页查看巡检与待处理回复。",
            "next_url": "/communications",
        })
    elif status == "已沟通":
        result.update({
            "advice_code": "await_or_review_reply",
            "summary": "等待或采集 HR 的后续回复；涉及面试时间、简历或敏感信息时继续人工处理。",
            "next_command": "",
            "next_instruction": "请在沟通草稿页查看巡检与待处理回复。",
            "next_url": "/communications",
        })
    elif status == "面试邀请":
        result.update({
            "advice_code": "confirm_interview_details",
            "summary": "先与 HR 人工确认面试时间、形式和流程；确认后再将本地状态更新为待面试。",
            "next_command": "将当前岗位标记为待面试",
            "next_url": f"/jobs/{job_id}",
        })
    elif status in INTERVIEW_PREP_TRIGGER_STATUSES:
        result.update({
            "advice_code": "work_on_interview_preparation",
            "summary": "本地状态已允许面试准备，优先查看计划、题库和待练习薄弱点。",
            "next_command": "为当前岗位准备面试",
            "next_url": f"/interviews/{result['interview_preparation_id']}" if result["interview_preparation_id"] else "/interviews",
        })
    elif status == "已面试":
        result.update({
            "advice_code": "record_interview_review",
            "summary": "建议尽快记录没答好的问题和复盘材料，再补强后续模拟面试。",
            "next_command": "",
            "next_instruction": "请在面试准备页记录复盘与薄弱点。",
            "next_url": "/interviews",
        })
    else:
        result.update({
            "advice_code": "review_job_record",
            "summary": "当前状态没有可自动推进的动作，请先查看岗位记录和已保存的审计信息。",
            "next_command": "解释当前岗位",
            "next_url": f"/jobs/{job_id}",
        })
    return result


def local_next_job_action_response(advice: dict[str, Any]) -> str:
    if advice.get("status") != "已完成":
        return f"没有找到岗位 #{advice.get('job_id')}。"
    continuation = (
        f" 可继续说“{advice['next_command']}”。"
        if advice.get("next_command")
        else f" {advice.get('next_instruction') or '请查看对应页面。'}"
    )
    return (
        f"岗位 #{advice['job_id']} 当前状态为“{advice['job_status']}”，匹配 {advice['match_score']} 分，"
        f"建议“{advice['recommendation']}”，风险“{advice['risk_level']}”。"
        f"下一步：{advice['summary']}{continuation}"
        "本轮只读取本地记录，未调用模型、浏览器或外部平台。"
    )


def local_resume_readiness(conn: Any, job_id: int | None = None) -> dict[str, Any]:
    profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
    resumes = conn.execute(
        "SELECT id, profile_id, name, target_role, file_type, parsed_text, is_default FROM resume_versions ORDER BY is_default DESC, id"
    ).fetchall()
    critical_gaps: list[str] = []
    advisory_gaps: list[str] = []
    if not profile:
        critical_gaps.append("候选人画像尚未创建")
    else:
        profile_data = {key: profile[key] for key in profile.keys()}
        for field, label in (
            ("name", "候选人名称"),
            ("education", "教育背景"),
            ("target_roles", "目标岗位方向"),
            ("skills_json", "明确技能"),
            ("projects_json", "项目事实"),
        ):
            raw_value = profile_data.get(field)
            parsed_value = loads(raw_value, []) if field.endswith("_json") or field == "target_roles" else raw_value
            if not parsed_value:
                critical_gaps.append(f"候选人画像缺少{label}")
        if not str(profile_data.get("github_url") or "").strip():
            advisory_gaps.append("可补充公开 GitHub 链接作为项目佐证")

    resume_data = [{key: row[key] for key in row.keys()} for row in resumes]
    default_resume = next((item for item in resume_data if item["is_default"]), None)
    imported_resumes = [item for item in resume_data if len(str(item.get("parsed_text") or "").strip()) >= 160]
    if not resume_data:
        critical_gaps.append("尚未登记任何简历版本")
    elif not imported_resumes:
        critical_gaps.append("尚未导入包含有效正文的简历版本")
    elif not default_resume:
        advisory_gaps.append("尚未设置默认简历版本")
    elif len(str(default_resume.get("parsed_text") or "").strip()) < 160:
        advisory_gaps.append("默认简历正文未导入或过短，可改设已导入的版本为默认")

    job_summary: dict[str, Any] | None = None
    if job_id:
        job = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        if not job:
            return {
                "status": "未找到岗位",
                "job_id": job_id,
                "critical_gaps": critical_gaps,
                "advisory_gaps": advisory_gaps,
                "model_called": False,
                "browser_accessed": False,
            }
        job_data = parse_json_fields({key: job[key] for key in job.keys()})
        selected_resume_id = int(job_data["selected_resume_id"]) if job_data.get("selected_resume_id") else None
        selected_resume = next((item for item in resume_data if item["id"] == selected_resume_id), None)
        if not selected_resume_id:
            critical_gaps.append("当前岗位尚未绑定简历版本")
        elif not selected_resume:
            critical_gaps.append("当前岗位绑定的简历版本不存在")
        elif len(str(selected_resume.get("parsed_text") or "").strip()) < 160:
            critical_gaps.append("当前岗位绑定的简历正文未导入或过短")
        scoring = (job_data.get("extracted") or {}).get("scoring") or {}
        missing_skills = compact_interview_items(scoring.get("missing_skills"), limit=5, item_limit=80)
        if missing_skills:
            advisory_gaps.append("岗位能力缺口待确认：" + "、".join(missing_skills))
        job_summary = {
            "id": job_id,
            "has_selected_resume": bool(selected_resume),
            "selected_resume_id": selected_resume_id,
            "missing_skills": missing_skills,
        }

    for resume in resume_data:
        if len(str(resume.get("parsed_text") or "").strip()) < 160:
            advisory_gaps.append(f"简历版本“{resume['name'] or '未命名'}”的正文未导入或过短")
    return {
        "status": "已完成",
        "job_id": job_id,
        "profile_present": bool(profile),
        "resume_count": len(resume_data),
        "default_resume_id": int(default_resume["id"]) if default_resume else None,
        "job": job_summary,
        "critical_gaps": list(dict.fromkeys(critical_gaps)),
        "advisory_gaps": list(dict.fromkeys(advisory_gaps)),
        "ready_for_preparation": not critical_gaps,
        "model_called": False,
        "browser_accessed": False,
        "job_status_changed": False,
    }


def local_resume_readiness_response(result: dict[str, Any]) -> str:
    if result.get("status") == "未找到岗位":
        return f"没有找到岗位 #{result.get('job_id')}。可以先在岗位列表确认编号，再检查简历准备情况。"
    scope = f"岗位 #{result['job_id']} 的简历就绪情况" if result.get("job_id") else "全局简历就绪情况"
    critical = result.get("critical_gaps") or []
    advisory = result.get("advisory_gaps") or []
    if critical:
        base = f"{scope}：当前尚未达到可准备投递的最低资料完整度。需要补充：" + "；".join(critical) + "。"
    else:
        base = f"{scope}：基础资料已满足本地检查。"
    if advisory:
        base += " 建议继续处理：" + "；".join(advisory[:5]) + "。"
    base += "请在候选人画像页补全；本轮未读取或展示联系方式、文件路径或简历正文。"
    return base


def compact_visual_review_text(value: object, limit: int = 500) -> str:
    return " ".join(redact_control_text(str(value or "")).split())[:limit]


def normalize_visual_page_review(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("视觉模型没有返回 JSON 对象。")
    page_type = str(value.get("page_type") or "unknown").strip()
    if page_type not in {"job_detail", "search_results", "unknown"}:
        page_type = "unknown"
    try:
        confidence = float(value.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    candidate_jobs = []
    raw_candidates = value.get("candidate_jobs")
    if isinstance(raw_candidates, list):
        for raw in raw_candidates[:6]:
            if not isinstance(raw, dict):
                continue
            candidate = {
                "company": compact_visual_review_text(raw.get("company"), 140),
                "title": compact_visual_review_text(raw.get("title"), 180),
                "city": compact_visual_review_text(raw.get("city"), 80),
                "salary_text": compact_visual_review_text(raw.get("salary_text"), 100),
                "experience_text": compact_visual_review_text(raw.get("experience_text"), 80),
            }
            if candidate["company"] or candidate["title"]:
                candidate_jobs.append(candidate)
    uncertainties = []
    raw_uncertainties = value.get("uncertainties")
    if isinstance(raw_uncertainties, list):
        uncertainties = [compact_visual_review_text(item, 180) for item in raw_uncertainties[:6] if compact_visual_review_text(item, 180)]
    return {
        "page_type": page_type,
        "company": compact_visual_review_text(value.get("company"), 140),
        "title": compact_visual_review_text(value.get("title"), 180),
        "city": compact_visual_review_text(value.get("city"), 80),
        "salary_text": compact_visual_review_text(value.get("salary_text"), 100),
        "summary": compact_visual_review_text(value.get("summary"), 700),
        "candidate_jobs": candidate_jobs,
        "confidence": max(0.0, min(confidence, 1.0)),
        "uncertainties": uncertainties,
    }


def run_visual_page_review(
    mode: str = "viewport",
    user_message: str = "",
    *,
    expected_url: str = "",
    platform: str = "",
) -> dict[str, Any]:
    # Reuse the task-chat model so screenshot interpretation keeps the same local conversation context.
    client = client_for_task("control_intent")
    if not client or not client.configured:
        return {
            "status": "未配置",
            "note": "请先将“控制层意图理解”配置为支持图像输入的 OpenAI-compatible 聊天模型。",
            "model_called": False,
            "image_sent_to_model": False,
        }
    try:
        capture = capture_controlled_edge_visual_page(mode, expected_url=expected_url, platform=platform)
        metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
        history = [item for item in control_history_for_model() if item.get("assistant")][-6:]
        history_text = "\n".join(
            f"用户：{item['user']}\nAgent：{item['assistant']}" for item in history
        ) or "（无）"
        prompt = (
            "请只分析这张招聘岗位或搜索结果页面截图，并只输出 JSON 对象。"
            "禁止转录或输出手机号、邮箱、联系人、聊天正文、验证码或任何联系方式。"
            "若无法确认字段必须留空并写入 uncertainties，不要猜测。"
            "JSON 字段：page_type(job_detail/search_results/unknown)、company、title、city、salary_text、"
            "summary(不超过300字)、candidate_jobs(最多6条，每条仅 company/title/city/salary_text/experience_text)、"
            "confidence(0到1)、uncertainties(字符串数组)。"
            "这只是视觉复核，不判断是否投递，不产生外部动作。"
        )
        user_prompt = (
            f"截图模式：{'整页缩放' if mode == 'full_page' else '当前可视区域'}；"
            f"招聘平台：{metadata.get('platform') or '未知'}。"
            f"最近对话：\n{history_text}\n\n当前用户任务：{redact_control_text(user_message)}\n"
            "请按约定 JSON 返回，并优先保留页面上明确可见的岗位信息。"
        )
        raw = client.complete_json_with_image(prompt, user_prompt, str(capture["image_data_url"]))
        review = normalize_visual_page_review(raw)
    except Exception as exc:
        if hasattr(client, "log_error"):
            client.log_error(str(exc))
        return {
            "status": "失败",
            "note": f"页面视觉复核失败：{str(exc)[:300]}",
            "error": str(exc)[:300],
            "model_called": True,
            "image_sent_to_model": True,
        }
    return {
        "status": "已完成",
        "note": "页面视觉复核已完成；截图未保存，仅保留结构化摘要。",
        "review": review,
        "capture": metadata,
        "model_called": True,
        "model_profile": str(getattr(client, "profile", {}).get("name") or ""),
        "model_name": str(getattr(client, "model", "") or ""),
        "image_sent_to_model": True,
    }


def visual_detail_fallback_allowed(error: Exception) -> bool:
    message = str(error or "")
    return any(
        signal in message
        for signal in (
            "页面文本太短",
            "没有返回可读取的页面内容",
            "页面脚本执行失败",
            "岗位详情页没有返回可读取",
        )
    ) and not any(signal in message for signal in ("登录", "验证码", "安全验证", "passport", "security"))


def normalize_visual_job_detail_review(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("视觉模型没有返回 JSON 对象。")
    jd_text = normalize_visible_text(redact_control_text(str(value.get("jd_text") or value.get("description") or "")))
    if len(jd_text) < 80:
        raise ValueError("视觉模型未识别到足够长度的岗位 JD。")
    try:
        confidence = float(value.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    uncertainties = value.get("uncertainties") if isinstance(value.get("uncertainties"), list) else []
    return {
        "company": compact_visual_review_text(value.get("company"), 140),
        "title": compact_visual_review_text(value.get("title"), 180),
        "city": compact_visual_review_text(value.get("city"), 80),
        "salary_text": compact_visual_review_text(value.get("salary_text"), 100),
        "experience_text": compact_visual_review_text(value.get("experience_text"), 80),
        "internship_days": compact_visual_review_text(value.get("internship_days"), 80),
        "internship_duration": compact_visual_review_text(value.get("internship_duration"), 80),
        "jd_text": validate_fetched_text(jd_text),
        "confidence": max(0.0, min(confidence, 1.0)),
        "uncertainties": [compact_visual_review_text(item, 180) for item in uncertainties[:6] if compact_visual_review_text(item, 180)],
    }


def run_visual_job_detail_fallback(url: str, candidate: dict[str, Any]) -> dict[str, Any]:
    """Recover a short/empty DOM JD through one temporary detail-page screenshot."""
    client = client_for_task("control_intent")
    if not client or not client.configured:
        return {
            "status": "未配置",
            "note": "控制层聊天模型未配置，无法执行详情页视觉复核。",
            "model_called": False,
            "image_sent_to_model": False,
        }
    target: dict[str, Any] | None = None
    image_sent = False
    try:
        safe_url = ensure_public_http_url(url)
        target = create_controlled_edge_target(safe_url)
        wait_for_cdp_document_ready(target)
        snapshot = evaluate_cdp_expression(target, controlled_job_snapshot_expression())
        if not isinstance(snapshot, dict):
            raise ValueError("受控 Edge 岗位详情页没有返回可读取的页面内容。")
        final_url = ensure_public_http_url(str(snapshot.get("url") or safe_url))
        if is_recruitment_interstitial_url(final_url):
            raise ValueError("岗位详情页已跳转到登录或安全验证页，请在受控 Edge 完成验证后重试。")
        capture = capture_visual_page_target(target, "viewport")
    except Exception as exc:
        if hasattr(client, "log_error"):
            client.log_error(str(exc))
        return {
            "status": "失败",
            "note": f"详情页视觉复核未完成：{str(exc)[:300]}",
            "error": str(exc)[:300],
            "model_called": False,
            "image_sent_to_model": False,
        }
    finally:
        if target:
            close_controlled_edge_target(target)

    metadata = capture.get("metadata") if isinstance(capture.get("metadata"), dict) else {}
    try:
        history = [item for item in control_history_for_model() if item.get("assistant")][-6:]
        history_text = "\n".join(f"用户：{item['user']}\nAgent：{item['assistant']}" for item in history) or "（无）"
        prompt = (
            "请只分析这张招聘岗位详情页截图，并只输出 JSON 对象。"
            "禁止转录或输出手机号、邮箱、联系人、聊天正文、验证码或任何联系方式。"
            "若字段无法确认必须留空并写入 uncertainties，不要猜测。"
            "JSON 字段：company、title、city、salary_text、experience_text、internship_days、internship_duration、"
            "jd_text(仅页面上明确可见的岗位职责和要求，最多5000字)、confidence(0到1)、uncertainties(字符串数组)。"
            "这只是详情页视觉复核，不判断是否投递，不产生外部动作。"
        )
        user_prompt = (
            f"招聘平台：{metadata.get('platform') or '未知'}。"
            f"候选链接对应的已知信息（可能不完整）：公司={compact_visual_review_text(candidate.get('company'), 100)}；"
            f"岗位={compact_visual_review_text(candidate.get('title'), 140)}；城市={compact_visual_review_text(candidate.get('city'), 60)}。"
            f"最近对话：\n{history_text}\n\n"
            "请按约定 JSON 返回。"
        )
        image_sent = True
        raw = client.complete_json_with_image(prompt, user_prompt, str(capture["image_data_url"]))
        review = normalize_visual_job_detail_review(raw)
    except Exception as exc:
        if hasattr(client, "log_error"):
            client.log_error(str(exc))
        return {
            "status": "失败",
            "note": f"详情页视觉复核未完成：{str(exc)[:300]}",
            "error": str(exc)[:300],
            "capture": metadata,
            "model_called": image_sent,
            "image_sent_to_model": image_sent,
        }

    fetched = FetchResult(
        url=safe_url,
        final_url=final_url,
        title=review["title"] or str(candidate.get("title") or ""),
        text=review["jd_text"],
        fetch_mode="controlled_edge_visual",
        note="DOM 岗位详情文本不足，已用临时页面视觉复核补充；截图未保存。",
    )
    return {
        "status": "已完成",
        "note": "详情页视觉复核已完成；截图未保存，已返回可校验 JD 文本。",
        "fetched": fetched,
        "review": review,
        "capture": metadata,
        "model_called": True,
        "model_profile": str(getattr(client, "profile", {}).get("name") or ""),
        "model_name": str(getattr(client, "model", "") or ""),
        "image_sent_to_model": True,
    }


def fetch_discovery_candidate_detail(candidate: dict[str, Any]) -> tuple[FetchResult, dict[str, Any]]:
    source_url = str(candidate.get("source_url") or "")
    try:
        return fetch_job_from_controlled_edge(source_url), {"visual_detail_fallback": False}
    except Exception as dom_error:
        if not visual_detail_fallback_allowed(dom_error):
            raise
        visual_result = run_visual_job_detail_fallback(source_url, candidate)
        fetched = visual_result.get("fetched")
        if visual_result.get("status") == "已完成" and isinstance(fetched, FetchResult):
            return fetched, {
                "visual_detail_fallback": True,
                "visual_confidence": (visual_result.get("review") or {}).get("confidence") if isinstance(visual_result.get("review"), dict) else None,
                "image_persisted": False,
            }
        raise ValueError(f"DOM 详情读取失败：{str(dom_error)[:180]}；{str(visual_result.get('note') or '视觉复核未完成')[:240]}")


def visual_page_review_response(result: dict[str, Any]) -> str:
    if result.get("status") != "已完成":
        return str(result.get("note") or "页面视觉复核未完成。")
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    parts = ["页面视觉复核已完成。"]
    overview = " - ".join(item for item in [str(review.get("company") or ""), str(review.get("title") or "")] if item)
    if overview:
        parts.append(overview + "。")
    details = [
        f"地点：{review['city']}" if review.get("city") else "",
        f"薪资：{review['salary_text']}" if review.get("salary_text") else "",
        f"置信度：{int(float(review.get('confidence') or 0) * 100)}%",
    ]
    if any(details):
        parts.append("；".join(item for item in details if item) + "。")
    if review.get("summary"):
        parts.append("摘要：" + str(review["summary"]))
    candidates = review.get("candidate_jobs") if isinstance(review.get("candidate_jobs"), list) else []
    if candidates:
        compact_candidates = [
            " / ".join(item for item in [candidate.get("company"), candidate.get("title"), candidate.get("city"), candidate.get("salary_text"), candidate.get("experience_text")] if item)
            for candidate in candidates
        ]
        parts.append("候选：" + "；".join(item for item in compact_candidates if item))
    if review.get("uncertainties"):
        parts.append("待确认：" + "；".join(str(item) for item in review["uncertainties"][:3]))
    parts.append("截图未保存，视觉结果只作补充证据；不会自动导入岗位、点击、发消息或投递。")
    return " ".join(parts)


def visual_candidate_matches_dom(visual_candidate: dict[str, Any], dom_candidate: Any) -> bool:
    visual_title = re.sub(r"\s+", "", str(visual_candidate.get("title") or "").lower())
    visual_company = re.sub(r"\s+", "", str(visual_candidate.get("company") or "").lower())
    dom_title = re.sub(r"\s+", "", str(getattr(dom_candidate, "title", "") or "").lower())
    dom_company = re.sub(r"\s+", "", str(getattr(dom_candidate, "company", "") or "").lower())
    title_match = bool(visual_title and dom_title and (visual_title in dom_title or dom_title in visual_title))
    company_match = bool(visual_company and dom_company and (visual_company in dom_company or dom_company in visual_company))
    return title_match or (company_match and bool(visual_title or dom_title))


def reconcile_search_result_with_visual_review(result: SearchResult, review: dict[str, Any]) -> int:
    """Merge visual fields only when they map to a DOM candidate with a stable source URL."""
    visual_candidates = review.get("candidate_jobs") if isinstance(review.get("candidate_jobs"), list) else []
    reconciled = 0
    for visual_candidate in visual_candidates:
        if not isinstance(visual_candidate, dict):
            continue
        matches = [candidate for candidate in result.candidates if visual_candidate_matches_dom(visual_candidate, candidate)]
        if len(matches) != 1:
            continue
        candidate = matches[0]
        changed = False
        if not candidate.company and visual_candidate.get("company"):
            candidate.company = compact_visual_review_text(visual_candidate.get("company"), 80)
            changed = True
        if not candidate.city and visual_candidate.get("city"):
            candidate.city = compact_visual_review_text(visual_candidate.get("city"), 80)
            changed = True
        details = [
            f"视觉薪资：{compact_visual_review_text(visual_candidate.get('salary_text'), 100)}" if visual_candidate.get("salary_text") else "",
            f"视觉经验：{compact_visual_review_text(visual_candidate.get('experience_text'), 80)}" if visual_candidate.get("experience_text") else "",
        ]
        details = [item for item in details if item]
        if details and not all(item in candidate.summary for item in details):
            candidate.summary = (candidate.summary + "\n" + "\n".join(details)).strip()[:420]
            changed = True
        reconciled += int(changed)
    return reconciled


def search_result_needs_visual_review(result: SearchResult) -> bool:
    if not result.candidates:
        return True
    incomplete = sum(
        not str(candidate.title or "").strip()
        or candidate.title == "候选岗位"
        or not str(candidate.company or "").strip()
        or len(str(candidate.summary or "").strip()) < 18
        for candidate in result.candidates
    )
    if incomplete * 2 >= len(result.candidates):
        return True
    candidate_text = "\n".join(
        f"{candidate.title or ''}\n{candidate.summary or ''}" for candidate in result.candidates
    )
    # Search queries target internships. A page with no visible internship signal
    # is often a social-hiring result list whose experience requirements DOM did not expose.
    return not bool(JOB_DISCOVERY_INTERNSHIP_SIGNAL_RE.search(candidate_text))


def control_memory_overview(conn: Any) -> dict[str, Any]:
    active_row = conn.execute(
        "SELECT * FROM control_memories WHERE memory_type = 'active_job' ORDER BY updated_at DESC, id DESC LIMIT 1"
    ).fetchone()
    active_job: dict[str, Any] | None = None
    if active_row:
        active_value = loads(active_row["value_json"], {})
        job_id = active_value.get("job_id")
        if isinstance(job_id, int) and job_id > 0:
            job_row = conn.execute(
                "SELECT id, company, title, platform, match_score, recommendation, risk_level, status FROM job_postings WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job_row:
                active_job = {key: job_row[key] for key in job_row.keys()}
                active_job["memory_id"] = int(active_row["id"])
                active_job["updated_at"] = str(active_row["updated_at"] or "")

    preference_rows = conn.execute(
        "SELECT * FROM control_memories WHERE memory_type = 'preference' ORDER BY updated_at DESC, id DESC LIMIT 12"
    ).fetchall()
    preferences = []
    for row in preference_rows:
        value = loads(row["value_json"], {})
        content = str(value.get("content") or "").strip()[:300]
        if content:
            preferences.append({"id": int(row["id"]), "content": content, "updated_at": str(row["updated_at"] or "")})
    return {"active_job": active_job, "preferences": preferences}


def load_control_memory_overview() -> dict[str, Any]:
    with connect() as conn:
        return control_memory_overview(conn)


def set_active_control_job(conn: Any, job_id: int, conversation_id: int | None = None) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, company, title, platform, match_score, recommendation, risk_level, status FROM job_postings WHERE id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    job = {key: row[key] for key in row.keys()}
    now = utc_now()
    conn.execute("DELETE FROM control_memories WHERE memory_type = 'active_job'")
    conn.execute(
        "INSERT INTO control_memories (memory_type, value_json, source_conversation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("active_job", dumps({"job_id": int(job["id"])}), conversation_id, now, now),
    )
    return job


def add_control_preference(conn: Any, content: str, conversation_id: int | None = None) -> int:
    now = utc_now()
    cursor = conn.execute(
        "INSERT INTO control_memories (memory_type, value_json, source_conversation_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("preference", dumps({"content": content.strip()[:300]}), conversation_id, now, now),
    )
    return int(cursor.lastrowid)


def control_active_reference_intent(message: str, fallback: dict[str, Any]) -> tuple[dict[str, Any], str]:
    normalized = " ".join(message.strip().split())
    active_reference = any(token in normalized for token in ("当前岗位", "这个岗位", "该岗位", "当前公司", "这家公司", "该公司"))
    active_memory_intents = {"compare_jobs", "prepare_interview", "update_job_status", "next_job_action", "resume_readiness"}
    if fallback["type"] in active_memory_intents and not active_reference:
        return fallback, ""
    if fallback["type"] != "help" and not (fallback["type"] in active_memory_intents and active_reference):
        return fallback, ""
    if not active_reference:
        return fallback, ""
    memory = load_control_memory_overview()
    active_job = memory.get("active_job")
    if not active_job:
        return {"type": "help", "filters": {"missing_active_job": True}}, "当前没有已选择的岗位。"
    job_id = int(active_job["id"])
    if any(token in normalized for token in ("比较", "对比", "哪个更适合", "哪个值得优先", "优先沟通哪个")):
        comparison_ids = [item for item in explicit_control_job_ids(normalized) if item != job_id]
        if comparison_ids:
            return {"type": "compare_jobs", "filters": {"job_ids": [job_id, comparison_ids[0]]}}, ""
        return {"type": "compare_jobs", "filters": {"job_ids": [job_id]}}, ""
    if any(token in normalized for token in ("准备面试", "面试准备", "开始备面", "开始准备面试")):
        return {"type": "prepare_interview", "filters": {"job_id": job_id}}, ""
    if fallback["type"] == "update_job_status" and fallback["filters"].get("status") in CONTROL_STATUS_UPDATE_TARGETS:
        return {"type": "update_job_status", "filters": {"job_id": job_id, "status": fallback["filters"]["status"]}}, ""
    if fallback["type"] == "next_job_action":
        return {"type": "next_job_action", "filters": {"job_id": job_id}}, ""
    if fallback["type"] == "resume_readiness":
        return {"type": "resume_readiness", "filters": {"job_id": job_id}}, ""
    if any(token in normalized for token in ("深度匹配复核", "深度复核", "匹配复核", "匹配解释")):
        return {"type": "job_match_review", "filters": {"job_id": job_id}}, ""
    if any(token in normalized for token in ("公司风险", "查公司", "公司尽调", "公司背调", "公司怎么样")):
        search_depth = "deep" if any(token in normalized for token in ("深度", "详细")) else "quick" if "快速" in normalized else "standard" if "标准" in normalized else "auto"
        return {"type": "company_research", "filters": {"job_id": job_id, "search_depth": search_depth}}, ""
    if any(token in normalized for token in ("准备沟通", "沟通准备", "准备打招呼", "开始沟通", "去沟通", "聊一下")):
        return {"type": "prepare_communication", "filters": {"job_id": job_id}}, ""
    if any(token in normalized for token in ("投递准备", "准备投递")):
        return {"type": "prepare_application", "filters": {"job_id": job_id}}, ""
    if any(token in normalized for token in ("解释", "分析", "详情", "看看", "怎么样")):
        return {"type": "explain_job", "filters": {"job_id": job_id}}, ""
    return fallback, ""


def control_history_for_model(limit: int = 6) -> list[dict[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_text, response_text FROM control_conversations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "user": str(row["user_text"] or "")[:700],
            "assistant": str(row["response_text"] or "")[:700],
        }
        for row in reversed(rows)
    ]


def control_intent_messages(message: str, history: list[dict[str, str]]) -> list[dict[str, str]]:
    role_options = "、".join(ROLE_NAMES)
    city_options = "、".join(CITY_NAMES)
    history_text = "\n".join(
        f"用户：{item['user']}\nAgent：{item['assistant']}"
        for item in history
    ) or "（无）"
    return [
        {
            "role": "system",
            "content": (
                "你是本地求职 Agent 的受限任务路由器。只输出 JSON 对象，不要回答用户，也不要输出分析过程。"
                "允许的 type 只有 search_draft、stats、explain_job、ignore_broadcast、show_plan、help。"
                "search_draft 的 filters 只能包含 role、city、min_salary_per_day；role 只能是"
                f" {role_options} 或空字符串；city 只能是 {city_options} 或空字符串；薪资为整数或 null。"
                "explain_job 只能返回正整数 job_id；ignore_broadcast 只能返回正整数 capture_id。"
                "stats、show_plan、help 的 filters 必须为空对象。"
                "岗位短名单、岗位比较、下一步建议、简历就绪检查、未读扫描、深度匹配复核、面试准备、状态变更、公司查询、沟通准备和投递准备必须返回 help，不能猜测目标；它们只由本地规则在用户明确编号或已选择当前岗位后处理。"
                "reason 只写一句不超过 40 个字的可公开判断摘要，不能写思维链、隐私信息或工具参数。"
            ),
        },
        {
            "role": "user",
            "content": f"最近已保存的对话：\n{history_text}\n\n当前任务：\n{redact_control_text(message)}",
        },
    ]


def resolve_control_intent(message: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fallback, active_job_error = control_active_reference_intent(message, parse_control_intent(message))
    if fallback["type"] != "help":
        return fallback, {
            "parser": "local_rules",
            "reasoning_summary": "本轮由本地规则完成任务理解，未产生模型调用。",
            "events": [control_event("判断摘要", "完成", f"使用本地规则识别为“{fallback['type']}”，未调用 LLM。")],
        }

    if active_job_error:
        return fallback, {
            "parser": "local_rules",
            "reasoning_summary": active_job_error,
            "events": [control_event("工作记忆", "需要选择", "请先输入“选择岗位 #编号”，再使用“当前岗位”。")],
        }

    client = client_for_task("control_intent")
    if not client or not client.configured:
        return fallback, {
            "parser": "local_rules",
            "reasoning_summary": "本轮由本地规则完成任务理解；当前没有可用的控制层模型。",
            "events": [control_event("判断摘要", "完成", f"使用本地规则识别为“{fallback['type']}”，未调用 LLM。")],
        }

    try:
        parsed = normalize_model_control_intent(client.complete_json(control_intent_messages(message, control_history_for_model())))
        if not parsed:
            raise ValueError("模型返回的意图不在允许范围内。")
    except Exception as exc:
        if hasattr(client, "log_error"):
            client.log_error(str(exc))
        return fallback, {
            "parser": "local_rules",
            "reasoning_summary": "模型意图理解不可用，已回退到本地规则；本轮安全边界不变。",
            "events": [
                control_event("模型调用", "已回退", f"控制层模型未产生可用受限意图：{str(exc)[:180]}"),
                control_event("判断摘要", "完成", f"使用本地规则识别为“{fallback['type']}”。"),
            ],
        }

    profile_name = str(getattr(client, "profile", {}).get("name") or "已配置模型")
    model_name = str(getattr(client, "model", "") or "")
    model_label = f"{profile_name}{f' / {model_name}' if model_name else ''}"
    reason = str(parsed.pop("reason", "") or "")
    reasoning_summary = reason or f"模型将本轮任务归类为“{parsed['type']}”，随后由本地策略层校验。"
    return parsed, {
        "parser": "llm_json",
        "model": {"profile": profile_name, "name": model_name, "task_type": "control_intent"},
        "reasoning_summary": reasoning_summary,
        "events": [
            control_event("模型调用", "完成", f"{model_label} 返回了受限结构化意图。"),
            control_event("判断摘要", "完成", f"本地策略层校验通过：{parsed['type']}。"),
        ],
    }


def control_conversation_data(row: Any) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    item["evidence"] = loads(item["evidence_json"], {})
    return item


def load_control_conversation(conversation_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM control_conversations WHERE id = ?", (conversation_id,)).fetchone()
    if not row:
        raise ValueError("任务记录未保存。")
    return control_conversation_data(row)


def control_message_response(conversation_id: int) -> dict[str, Any]:
    conversation = load_control_conversation(conversation_id)
    conversation["memory"] = load_control_memory_overview()
    return conversation


@app.get("/control")
def control_page(request: Request) -> Any:
    with connect() as conn:
        history_rows = conn.execute("SELECT * FROM control_conversations ORDER BY id DESC LIMIT 20").fetchall()
        pending = conn.execute("SELECT * FROM control_plans WHERE status = '待确认' ORDER BY id DESC").fetchall()
        control_memory = control_memory_overview(conn)
    history = [control_conversation_data(row) for row in reversed(history_rows)]
    latest_data = history[-1] if history else None
    return templates.TemplateResponse(request, "control.html", {
        "latest": latest_data,
        "history": history,
        "pending_plans": [{key: row[key] for key in row.keys()} for row in pending],
        "control_memory": control_memory,
        "notice": request.query_params.get("notice", ""),
        "notice_type": request.query_params.get("notice_type", "info"),
    })


async def process_control_message(message: str) -> dict[str, Any]:
    message = message.strip()
    if not message:
        raise ValueError("请输入一个任务，例如“找杭州 Agent 实习，日薪至少 200”。")
    intent, parse_evidence = await run_in_threadpool(resolve_control_intent, message)
    intent_type, filters = intent["type"], intent["filters"]
    suggestions = control_suggestions(intent_type, filters)
    events = parse_evidence["events"]
    evidence: dict[str, Any] = {
        "parser": parse_evidence["parser"],
        "filters": filters,
        "suggestions": suggestions,
        "reasoning_summary": parse_evidence["reasoning_summary"],
        "events": events,
    }
    if parse_evidence.get("model"):
        evidence["model"] = parse_evidence["model"]
    if intent_type == "search_draft":
        role = filters.get("role") or "沿用画像目标岗位"
        city = filters.get("city") or "沿用画像城市"
        salary = filters.get("min_salary_per_day")
        suffix = f"，最低日薪 {salary} 元" if salary else ""
        summary = f"在受控 Edge 中发现 {city} 的 {role}{suffix}。"
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
        events.append(
            control_event(
                "工具调用",
                "执行中",
                f"调用受控岗位发现：{city}，{role}{suffix or '，沿用画像筛选条件'}。",
            )
        )
        try:
            result = await run_in_threadpool(run_controlled_job_discovery, filters)
            execution_status = "已完成" if result.get("status") != "失败" else "失败"
            response_text = (
                f"已执行岗位发现：{result.get('note') or '任务已完成。'}"
                " 已保留搜索、JD 评分和审计记录；未发送消息、未上传简历、未投递。"
            )
        except Exception as exc:
            result = {"error": str(exc)[:300]}
            execution_status = "失败"
            response_text = f"岗位发现未完成：{result['error']}。未执行投递或沟通动作。"
        events.append(
            control_event(
                "工具结果",
                execution_status,
                str(result.get("note") or result.get("error") or "岗位发现已返回结果。")[:500],
            )
        )
        events.append(
            control_event(
                "安全结论",
                "已拦截提交动作",
                "本轮未发送消息、未上传简历、未投递。",
            )
        )
        evidence["execution"] = {
            "mode": "chat_direct_non_submitting",
            "action_type": "job_discovery",
            "status": execution_status,
            "result": result,
        }
        with connect() as conn:
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "job_discovery",
                summary,
                filters,
                result,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                decision={
                    "intent": intent_type,
                    "filters": filters,
                    "execution_mode": "chat_direct_non_submitting",
                    "message_saved_redacted": True,
                    "model_called": evidence["parser"] == "llm_json",
                    "model_task_type": (evidence.get("model") or {}).get("task_type", ""),
                    "auto_apply": False,
                    "auto_message": False,
                    "message_text_saved": False,
                },
                error_message=str(result.get("error") or "")[:300],
            )
        return control_message_response(conversation_id)
    if intent_type == "scan_unread_conversations":
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
        events.append(control_event("工具调用", "执行中", "只读检查已打开受控 Edge 消息列表的未读结构标记。"))
        try:
            result = await run_in_threadpool(run_unread_conversation_scan, "control_chat")
            execution_status = str(result.get("status") or "未完成")
            response_text = (
                f"未读会话只读扫描：{execution_status}。{str(result.get('note') or '')}"
                "结果仅表示页面未读标记候选，不读取会话正文、不打开会话、不生成回复。"
            )
        except Exception as exc:
            result = {"status": "失败", "error": str(exc)[:300]}
            execution_status = "失败"
            response_text = f"未读会话只读扫描失败：{result['error']}。未读取或发送任何消息。"
        result_summary = {
            "status": execution_status,
            "note": str(result.get("note") or "")[:500],
            "checked_page_count": int(result.get("checked_page_count") or 0),
            "message_list_page_count": int(result.get("message_list_page_count") or 0),
            "unread_count": int(result.get("unread_count") or 0),
            "error_count": int(result.get("error_count") or 0),
            "detector_version": str(result.get("detector_version") or ""),
            "model_called": False,
            "page_text_saved": False,
            "conversation_opened": False,
            "browser_clicked": False,
            "message_sent": False,
        }
        events.append(control_event("工具结果", execution_status, response_text[:500]))
        events.append(control_event("安全结论", "只读未读标记", "未读取或保存会话正文、名称、标题或链接；未打开会话、未点击、未填入或发送消息。"))
        evidence["execution"] = {
            "mode": "chat_direct_read_only_unread_scan",
            "action_type": "scan_unread_conversations",
            "status": execution_status,
            "result": result_summary,
        }
        with connect() as conn:
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "scan_unread_conversations",
                "通过聊天触发受控 Edge 消息列表的只读未读扫描。",
                {},
                result_summary,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                decision={
                    "intent": intent_type,
                    "filters": {},
                    "execution_mode": "chat_direct_read_only_unread_scan",
                    "message_saved_redacted": True,
                    "model_called": False,
                    "page_text_saved": False,
                    "page_url_saved": False,
                    "page_title_saved": False,
                    "conversation_opened": False,
                    "browser_clicked": False,
                    "message_filled": False,
                    "message_sent": False,
                },
                error_message=str(result.get("error") or "")[:300],
            )
        return control_message_response(conversation_id)
    if intent_type == "review_visual_page":
        mode = str(filters.get("mode") or "viewport")
        if mode not in {"viewport", "full_page"}:
            mode = "viewport"
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
        events.append(control_event("工具调用", "执行中", f"抓取受控 Edge 当前招聘页的{'整页缩放' if mode == 'full_page' else '可视区域'}截图，交给视觉模型复核。"))
        result = await run_in_threadpool(run_visual_page_review, mode, message)
        execution_status = str(result.get("status") or "未完成")
        response_text = visual_page_review_response(result)
        review = result.get("review") if isinstance(result.get("review"), dict) else {}
        capture = result.get("capture") if isinstance(result.get("capture"), dict) else {}
        result_summary = {
            "status": execution_status,
            "note": str(result.get("note") or "")[:500],
            "review": review,
            "capture": capture,
            "model_called": bool(result.get("model_called")),
            "model_profile": str(result.get("model_profile") or ""),
            "model_name": str(result.get("model_name") or ""),
            "image_sent_to_model": bool(result.get("image_sent_to_model")),
            "image_persisted": False,
        }
        events.append(control_event("模型调用", "完成" if execution_status == "已完成" else execution_status, str(result.get("note") or response_text)[:500]))
        events.append(control_event("工具结果", execution_status, response_text[:500]))
        events.append(control_event("安全结论", "视觉只读复核", "截图不落库；不保存页面正文，不自动导入岗位，不点击、不打开会话、不填入或发送消息。"))
        evidence["execution"] = {
            "mode": "chat_direct_visual_page_review",
            "action_type": "review_visual_page",
            "status": execution_status,
            "result": result_summary,
        }
        with connect() as conn:
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "review_visual_page",
                f"通过聊天对受控 Edge 当前招聘页执行{'整页' if mode == 'full_page' else '可视区域'}视觉复核。",
                {"mode": mode},
                result_summary,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                decision={
                    "intent": intent_type,
                    "filters": {"mode": mode},
                    "execution_mode": "chat_direct_visual_page_review",
                    "message_saved_redacted": True,
                    "model_called": bool(result.get("model_called")),
                    "model_task_type": "control_intent" if result.get("model_called") else "",
                    "image_sent_to_model": bool(result.get("image_sent_to_model")),
                    "image_persisted": False,
                    "page_text_saved": False,
                    "conversation_opened": False,
                    "browser_clicked": False,
                    "message_filled": False,
                    "message_sent": False,
                    "auto_apply": False,
                    "auto_message": False,
                },
                error_message=str(result.get("error") or "")[:300],
            )
        return control_message_response(conversation_id)
    if intent_type == "list_jobs":
        normalized_filters = parse_control_job_list_filters("")
        normalized_filters.update({key: value for key, value in filters.items() if key in normalized_filters})
        summary = f"读取{local_job_list_label(normalized_filters)}短名单。"
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
            jobs = list_local_jobs(conn, normalized_filters)
            result_summary = {
                "status": "已完成",
                "filters": normalized_filters,
                "returned_job_ids": [int(job["id"]) for job in jobs],
                "returned_count": len(jobs),
                "model_called": False,
                "browser_accessed": False,
            }
            response_text = local_job_list_response(jobs, normalized_filters)
            events.append(control_event("工具调用", "完成", "读取本地岗位短名单，不访问浏览器或模型。"))
            events.append(control_event("工具结果", "已完成", response_text[:500]))
            events.append(control_event("安全结论", "仅本地读取", "未调用模型、未访问浏览器、未修改岗位或创建沟通/投递动作。"))
            evidence["execution"] = {
                "mode": "chat_direct_local_job_list",
                "action_type": "list_jobs",
                "status": "已完成",
                "result": result_summary,
            }
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "list_jobs",
                summary,
                normalized_filters,
                result_summary,
                "已完成",
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status="已完成",
                summary=response_text,
                decision={
                    "intent": intent_type,
                    "filters": normalized_filters,
                    "execution_mode": "chat_direct_local_job_list",
                    "message_saved_redacted": True,
                    "model_called": False,
                    "browser_accessed": False,
                    "auto_apply": False,
                    "auto_message": False,
                    "job_score_changed": False,
                    "job_status_changed": False,
                },
            )
        return control_message_response(conversation_id)
    if intent_type == "next_job_action":
        job_id = filters.get("job_id")
        if not isinstance(job_id, int) or job_id <= 0:
            response_text = "请指定岗位编号，例如“岗位 #12 下一步怎么做”，或先选择当前岗位后再问“当前岗位下一步怎么做”。"
            events.append(control_event("工具调用", "未执行", "下一步建议需要明确岗位编号或当前岗位工作记忆。"))
            with connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (redact_control_text(message), intent_type, response_text, dumps(evidence), utc_now()),
                )
                conversation_id = int(cursor.lastrowid)
                log_agent_action(
                    conn,
                    action_type="control_request",
                    status="未执行",
                    summary=response_text,
                    decision={"intent": intent_type, "filters": filters, "message_saved_redacted": True, "model_called": False},
                )
            return control_message_response(conversation_id)
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
            advice = local_next_job_action(conn, job_id)
            execution_status = str(advice.get("status") or "未完成")
            response_text = local_next_job_action_response(advice)
            result_summary = {
                key: advice.get(key)
                for key in (
                    "status", "job_id", "job_status", "recommendation", "risk_level", "match_score",
                    "application_preparation_id", "interview_preparation_id", "company_research_count",
                    "advice_code", "next_command", "model_called", "browser_accessed", "job_status_changed",
                )
            }
            suggestions = [{"label": "打开岗位", "url": f"/jobs/{job_id}"}]
            if advice.get("next_url") and advice["next_url"] != f"/jobs/{job_id}":
                suggestions.append({"label": "查看下一步材料", "url": str(advice["next_url"])})
            evidence["suggestions"] = suggestions
            events.append(control_event("工具调用", "完成" if execution_status == "已完成" else execution_status, "读取本地岗位状态和准备记录，生成下一步建议。"))
            events.append(control_event("工具结果", execution_status, response_text[:500]))
            events.append(control_event("安全结论", "仅本地读取", "未调用模型、未访问浏览器、未修改岗位或创建沟通/投递动作。"))
            evidence["execution"] = {
                "mode": "chat_direct_local_next_action",
                "action_type": "next_job_action",
                "status": execution_status,
                "result": result_summary,
            }
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "next_job_action",
                f"读取岗位 #{job_id} 的下一步本地建议。",
                {"job_id": job_id},
                result_summary,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                job_id=job_id,
                decision={
                    "intent": intent_type,
                    "filters": {"job_id": job_id},
                    "execution_mode": "chat_direct_local_next_action",
                    "message_saved_redacted": True,
                    "model_called": False,
                    "browser_accessed": False,
                    "auto_apply": False,
                    "auto_message": False,
                    "job_status_changed": False,
                },
            )
        return control_message_response(conversation_id)
    if intent_type == "resume_readiness":
        job_id = filters.get("job_id")
        safe_job_id = job_id if isinstance(job_id, int) and job_id > 0 else None
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
            readiness = local_resume_readiness(conn, safe_job_id)
            execution_status = str(readiness.get("status") or "未完成")
            response_text = local_resume_readiness_response(readiness)
            result_summary = {
                key: readiness.get(key)
                for key in (
                    "status", "job_id", "profile_present", "resume_count", "default_resume_id", "job",
                    "critical_gaps", "advisory_gaps", "ready_for_preparation", "model_called", "browser_accessed", "job_status_changed",
                )
            }
            suggestions = [{"label": "候选人画像", "url": "/resumes"}]
            if safe_job_id:
                suggestions.append({"label": "打开岗位", "url": f"/jobs/{safe_job_id}"})
            evidence["suggestions"] = suggestions
            events.append(control_event("工具调用", "完成" if execution_status == "已完成" else execution_status, "检查本地候选人画像和简历版本完整度。"))
            events.append(control_event("工具结果", execution_status, response_text[:500]))
            events.append(control_event("安全结论", "仅本地读取", "未展示联系方式、文件路径或简历正文；未调用模型、浏览器或外部平台。"))
            evidence["execution"] = {
                "mode": "chat_direct_local_resume_readiness",
                "action_type": "resume_readiness",
                "status": execution_status,
                "result": result_summary,
            }
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "resume_readiness",
                "检查候选人画像和简历版本的本地就绪情况。",
                {"job_id": safe_job_id},
                result_summary,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                job_id=safe_job_id,
                decision={
                    "intent": intent_type,
                    "filters": {"job_id": safe_job_id},
                    "execution_mode": "chat_direct_local_resume_readiness",
                    "message_saved_redacted": True,
                    "model_called": False,
                    "browser_accessed": False,
                    "auto_apply": False,
                    "auto_message": False,
                    "job_status_changed": False,
                    "resume_text_exposed": False,
                    "contact_information_exposed": False,
                },
            )
        return control_message_response(conversation_id)
    if intent_type == "prepare_interview":
        job_id = filters.get("job_id")
        if not isinstance(job_id, int) or job_id <= 0:
            response_text = "请指定岗位编号，例如“为岗位 #12 准备面试”，或先选择当前岗位后再说“为当前岗位准备面试”。"
            events.append(control_event("工具调用", "未执行", "面试准备需要明确岗位编号或当前岗位工作记忆。"))
            with connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (redact_control_text(message), intent_type, response_text, dumps(evidence), utc_now()),
                )
                conversation_id = int(cursor.lastrowid)
                log_agent_action(
                    conn,
                    action_type="control_request",
                    status="未执行",
                    summary=response_text,
                    decision={"intent": intent_type, "filters": filters, "message_saved_redacted": True, "model_called": False},
                )
            return control_message_response(conversation_id)
        summary = f"为岗位 #{job_id} 创建或打开本地面试准备。"
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
            job = conn.execute("SELECT id, company, title, status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
            if not job:
                execution_status = "未找到"
                result_summary = {"status": execution_status, "job_id": job_id, "model_called": False, "browser_accessed": False}
                response_text = f"没有找到岗位 #{job_id}，未创建面试准备。"
            elif str(job["status"] or "") not in INTERVIEW_PREP_TRIGGER_STATUSES:
                execution_status = "等待人工确认"
                result_summary = {
                    "status": execution_status,
                    "job_id": job_id,
                    "job_status": str(job["status"] or ""),
                    "model_called": False,
                    "browser_accessed": False,
                }
                response_text = (
                    f"岗位 #{job_id} 当前状态为“{job['status'] or '待确认'}”，暂不创建面试准备。"
                    "请先与 HR 人工确认面试时间、形式和流程，再在岗位页改为“待面试”。"
                )
            else:
                result = ensure_interview_preparation_for_job(conn, job_id, trigger_type="control_chat")
                interview_id = result.get("interview_id")
                created = bool(result.get("created"))
                execution_status = "已完成" if interview_id else "未完成"
                result_summary = {
                    "status": execution_status,
                    "job_id": job_id,
                    "job_status": str(job["status"] or ""),
                    "interview_id": interview_id,
                    "created": created,
                    "model_called": False,
                    "browser_accessed": False,
                }
                if interview_id:
                    action = "已创建" if created else "已有"
                    response_text = (
                        f"{action}岗位 #{job_id} 的本地面试准备 #{interview_id}。"
                        "可进入面试准备页继续查看计划、题库和模拟面试；本轮未确认面试时间、未访问浏览器、未联系 HR。"
                    )
                else:
                    response_text = f"岗位 #{job_id} 的面试准备未完成：{result.get('error') or '本地记录创建失败'}。"
            events.append(control_event("工具调用", "完成" if execution_status == "已完成" else execution_status, "检查岗位面试状态并维护本地准备记录。"))
            events.append(control_event("工具结果", execution_status, response_text[:500]))
            events.append(control_event("安全结论", "仅本地准备", "未确认面试时间、未访问浏览器、未发送消息、未改变岗位状态。"))
            evidence["execution"] = {
                "mode": "chat_direct_local_interview_prep",
                "action_type": "prepare_interview",
                "status": execution_status,
                "result": result_summary,
            }
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "prepare_interview",
                summary,
                {"job_id": job_id},
                result_summary,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                job_id=job_id,
                decision={
                    "intent": intent_type,
                    "filters": {"job_id": job_id},
                    "execution_mode": "chat_direct_local_interview_prep",
                    "message_saved_redacted": True,
                    "model_called": False,
                    "browser_accessed": False,
                    "auto_apply": False,
                    "auto_message": False,
                    "job_status_changed": False,
                    "interview_time_confirmed": False,
                },
            )
        return control_message_response(conversation_id)
    if intent_type == "compare_jobs":
        job_ids = [int(job_id) for job_id in filters.get("job_ids") or [] if isinstance(job_id, int) and job_id > 0]
        if len(job_ids) != 2 or job_ids[0] == job_ids[1]:
            response_text = "请指定两个不同的岗位编号，例如“比较岗位 #12 和岗位 #15”。"
            events.append(control_event("工具调用", "未执行", "岗位比较需要两个不同的明确岗位编号。"))
            with connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (redact_control_text(message), intent_type, response_text, dumps(evidence), utc_now()),
                )
                conversation_id = int(cursor.lastrowid)
                log_agent_action(
                    conn,
                    action_type="control_request",
                    status="未执行",
                    summary=response_text,
                    decision={"intent": intent_type, "filters": filters, "message_saved_redacted": True, "model_called": False},
                )
            return control_message_response(conversation_id)
        summary = f"比较岗位 #{job_ids[0]} 与岗位 #{job_ids[1]} 的本地优先级。"
        with connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_postings WHERE id IN (?, ?)",
                (job_ids[0], job_ids[1]),
            ).fetchall()
            jobs_by_id = {int(row["id"]): parse_json_fields({key: row[key] for key in row.keys()}) for row in rows}
            missing_job_ids = [job_id for job_id in job_ids if job_id not in jobs_by_id]
            if missing_job_ids:
                execution_status = "未找到"
                result_summary = {"status": execution_status, "job_ids": job_ids, "missing_job_ids": missing_job_ids, "model_called": False}
                response_text = "没有找到岗位 #" + "、#".join(str(job_id) for job_id in missing_job_ids) + "，未执行比较。"
            else:
                comparison = compare_local_jobs(jobs_by_id[job_ids[0]], jobs_by_id[job_ids[1]])
                execution_status = "已完成"
                result_summary = {
                    "status": execution_status,
                    "job_ids": job_ids,
                    "preferred_job_id": comparison["preferred_job_id"],
                    "reason_codes": comparison["reason_codes"],
                    "model_called": False,
                    "browser_accessed": False,
                }
                response_text = local_job_comparison_response(comparison)
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "compare_jobs",
                summary,
                {"job_ids": job_ids},
                result_summary,
                execution_status,
            )
            evidence["execution"] = {
                "mode": "chat_direct_local_comparison",
                "action_type": "compare_jobs",
                "status": execution_status,
                "result": result_summary,
            }
            evidence["execution_id"] = execution_id
            events.append(control_event("工具调用", "完成" if execution_status == "已完成" else execution_status, "读取本地岗位分析结果进行比较。"))
            events.append(control_event("工具结果", execution_status, response_text[:500]))
            events.append(control_event("安全结论", "仅本地读取", "未调用模型、未访问浏览器、未修改岗位或创建沟通/投递动作。"))
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                decision={
                    "intent": intent_type,
                    "filters": {"job_ids": job_ids},
                    "execution_mode": "chat_direct_local_comparison",
                    "message_saved_redacted": True,
                    "model_called": False,
                    "browser_accessed": False,
                    "auto_apply": False,
                    "auto_message": False,
                    "job_score_changed": False,
                    "job_status_changed": False,
                },
            )
        return control_message_response(conversation_id)
    if intent_type == "job_match_review" and filters.get("job_id"):
        summary = f"为岗位 #{filters['job_id']} 进行深度匹配复核。"
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
        events.append(control_event("工具调用", "执行中", f"调用岗位匹配解释模型复核岗位 #{filters['job_id']}。"))
        try:
            result = await run_in_threadpool(run_job_match_review, int(filters["job_id"]))
            execution_status = str(result.get("status") or "未完成")
            content = result.get("content") if isinstance(result.get("content"), dict) else {}
            conclusion = str(content.get("conclusion") or "").strip()
            gaps = [str(item).strip() for item in content.get("gaps") or [] if str(item).strip()]
            questions = [str(item).strip() for item in content.get("questions_to_confirm") or [] if str(item).strip()]
            if execution_status == "已完成":
                parts = [str(result.get("note") or "深度匹配复核已完成。")]
                if conclusion:
                    parts.append(f"结论：{conclusion[:360]}")
                if gaps:
                    parts.append("待补强/确认：" + "；".join(gaps[:3]))
                if questions:
                    parts.append("沟通前问题：" + "；".join(questions[:2]))
                response_text = " ".join(parts)
            else:
                response_text = str(result.get("note") or "深度匹配复核未完成。")
        except Exception as exc:
            result = {"status": "未完成", "error": str(exc)[:300], "model_called": False}
            execution_status = "未完成"
            response_text = f"深度匹配复核未完成：{result['error']}。"
            content = {}
        model_called = bool(result.get("model_called"))
        model_status = "完成" if execution_status == "已完成" else "未配置" if execution_status == "未配置" else "未完成"
        events.append(control_event("模型调用", model_status, str(result.get("note") or response_text)[:500]))
        events.append(control_event("工具结果", execution_status, response_text[:500]))
        events.append(control_event("安全结论", "仅补充证据", "未修改本地评分、风险、建议或岗位状态；未访问浏览器、未发送消息、未投递。"))
        result_summary = {
            "status": execution_status,
            "note": str(result.get("note") or "")[:500],
            "model_called": model_called,
            "model_profile": str(result.get("model_profile") or ""),
            "model_name": str(result.get("model_name") or ""),
            "review_stored": bool(content),
            "model_fields": list(content.get("model_fields") or []),
        }
        evidence["execution"] = {
            "mode": "chat_direct_model_review",
            "action_type": "job_match_review",
            "status": execution_status,
            "result": result_summary,
        }
        with connect() as conn:
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "job_match_review",
                summary,
                {"job_id": int(filters["job_id"])},
                result_summary,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                job_id=int(filters["job_id"]),
                decision={
                    "intent": intent_type,
                    "filters": {"job_id": int(filters["job_id"])},
                    "execution_mode": "chat_direct_model_review",
                    "message_saved_redacted": True,
                    "model_called": model_called,
                    "model_task_type": "job_match" if model_called else "",
                    "browser_accessed": False,
                    "auto_apply": False,
                    "auto_message": False,
                    "job_score_changed": False,
                    "job_status_changed": False,
                },
                error_message=str(result.get("error") or "")[:300],
            )
        return control_message_response(conversation_id)
    if intent_type == "company_research" and filters.get("job_id"):
        requested_depth = str(filters.get("search_depth") or "auto")
        summary = f"查询岗位 #{filters['job_id']} 的公开公司风险信息（{requested_depth}）。"
        with connect() as conn:
            conversation_cursor = conn.execute(
                "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
                (redact_control_text(message), intent_type, utc_now()),
            )
            conversation_id = int(conversation_cursor.lastrowid)
        events.append(control_event("工具调用", "执行中", f"查询岗位 #{filters['job_id']} 的公开公司信息，检索深度为“{requested_depth}”。"))
        try:
            result = await run_in_threadpool(
                lambda: run_company_research_for_job(
                    int(filters["job_id"]), requested_depth=requested_depth, trigger_type="control_chat"
                )
            )
            execution_status = str(result.get("status") or "完成")
            if execution_status == "未执行":
                response_text = f"公司公开风险查询未执行：{result.get('reason') or '当前岗位不满足查询条件'}"
            else:
                response_text = str(result.get("note") or "公司公开风险查询已完成。")
        except Exception as exc:
            result = {"status": "失败", "error": str(exc)[:300]}
            execution_status = "失败"
            response_text = f"公司公开风险查询未完成：{result['error']}。"
        events.append(control_event("工具结果", execution_status, response_text[:500]))
        events.append(control_event("安全结论", "仅公开检索", "未访问招聘平台、未发送消息、未改变投递或沟通状态。"))
        evidence["execution"] = {
            "mode": "chat_direct_public_research",
            "action_type": "company_research",
            "status": execution_status,
            "result": result,
        }
        with connect() as conn:
            execution_id = create_completed_control_execution(
                conn,
                conversation_id,
                "company_research",
                summary,
                filters,
                result,
                execution_status,
            )
            evidence["execution_id"] = execution_id
            conn.execute(
                "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
                (response_text, dumps(evidence), conversation_id),
            )
            log_agent_action(
                conn,
                action_type="control_request",
                status=execution_status,
                summary=response_text,
                job_id=int(filters["job_id"]),
                decision={
                    "intent": intent_type,
                    "filters": filters,
                    "execution_mode": "chat_direct_public_research",
                    "message_saved_redacted": True,
                    "model_called": evidence["parser"] == "llm_json",
                    "model_task_type": (evidence.get("model") or {}).get("task_type", ""),
                    "recruitment_platform_accessed": False,
                    "auto_apply": False,
                    "auto_message": False,
                },
                error_message=str(result.get("error") or "")[:300],
            )
        return control_message_response(conversation_id)
    with connect() as conn:
        conversation_cursor = conn.execute(
            "INSERT INTO control_conversations (user_text, intent_type, response_text, evidence_json, created_at) VALUES (?, ?, '', '{}', ?)",
            (redact_control_text(message), intent_type, utc_now()),
        )
        conversation_id = int(conversation_cursor.lastrowid)
        if intent_type == "stats":
            row = conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN status = '待确认' THEN 1 ELSE 0 END) AS pending FROM job_postings").fetchone()
            response_text = f"本地共有 {row['total']} 条岗位，其中 {row['pending'] or 0} 条待确认。可从岗位列表继续查看。"
            events.append(control_event("工具调用", "完成", "查询本地岗位统计，不访问浏览器或模型。"))
        elif intent_type == "explain_job" and filters.get("job_id"):
            job = conn.execute("SELECT title, company, match_score, recommendation, risk_level, status FROM job_postings WHERE id = ?", (filters["job_id"],)).fetchone()
            if job:
                response_text = f"岗位 #{filters['job_id']}：{job['company']} - {job['title']}，匹配 {job['match_score']} 分，建议“{job['recommendation']}”，风险“{job['risk_level']}”，当前状态“{job['status']}”。"
            else:
                response_text = f"没有找到岗位 #{filters['job_id']}。请从岗位列表确认编号。"
            events.append(control_event("工具调用", "完成", "读取本地岗位记录并生成解释。"))
        elif intent_type == "select_job" and filters.get("job_id"):
            job = set_active_control_job(conn, int(filters["job_id"]), conversation_id)
            if job:
                response_text = (
                    f"已将岗位 #{job['id']} 设为当前工作记忆：{job['company']} - {job['title']}。"
                    "后续可说“当前岗位怎么样”或“为当前岗位创建投递准备”。"
                )
                events.append(control_event("工作记忆", "完成", f"当前岗位已更新为 #{job['id']}。"))
                log_agent_action(
                    conn,
                    action_type="control_memory",
                    status="当前岗位已更新",
                    summary=f"已选择当前岗位：{job['company']} - {job['title']}",
                    job_id=int(job["id"]),
                    decision={"memory_type": "active_job", "external_effect": False},
                )
            else:
                response_text = f"没有找到岗位 #{filters['job_id']}，未更新工作记忆。"
                events.append(control_event("工作记忆", "未找到", "岗位记录不存在，当前工作记忆保持不变。"))
        elif intent_type == "remember_preference":
            content = str(filters.get("content") or "").strip()[:300]
            if control_memory_contains_sensitive_text(content):
                response_text = "偏好记忆未保存：其中包含可能的联系方式或密钥。请在对应的安全设置或候选人画像中处理。"
                events.append(control_event("长期记忆", "已拦截", "不保存可能的联系方式、密码、Token 或 API Key。"))
            else:
                memory_id = add_control_preference(conn, content, conversation_id)
                response_text = f"已保存本地偏好记忆 #{memory_id}。它不会自动发送到招聘平台，也不会直接改变岗位评分。"
                events.append(control_event("长期记忆", "完成", "仅保存你明确提出的本地偏好，可在右侧编辑或删除。"))
                log_agent_action(
                    conn,
                    action_type="control_memory",
                    status="偏好已保存",
                    summary="已保存一条显式本地偏好记忆。",
                    decision={"memory_type": "preference", "external_effect": False, "content_logged": False},
                )
        elif intent_type == "show_memory":
            memory = control_memory_overview(conn)
            active_job = memory.get("active_job")
            active_label = (
                f"#{active_job['id']} {active_job['company']} - {active_job['title']}"
                if active_job else "未选择"
            )
            preference_text = "；".join(item["content"] for item in memory["preferences"][:3]) or "无"
            response_text = f"当前岗位：{active_label}。本地偏好记忆 {len(memory['preferences'])} 条：{preference_text}。"
            events.append(control_event("工作记忆", "完成", "读取本地工作记忆和显式偏好，不访问模型或浏览器。"))
        elif intent_type == "prepare_application" and filters.get("job_id"):
            result = ensure_application_preparation_for_job(conn, int(filters["job_id"]), trigger_type="control_chat")
            preparation_id = result.get("preparation_id")
            if preparation_id:
                action = "已生成" if result.get("created") else "已有"
                response_text = f"{action}岗位 #{filters['job_id']} 的本地投递准备 #{preparation_id}。请在投递准备页核对简历版本后再确认。"
                events.append(control_event("工具调用", "完成", f"{action}本地投递准备 #{preparation_id}。"))
                events.append(control_event("安全结论", "未执行投递", "未打开平台、未上传简历、未点击投递。"))
            else:
                response_text = f"暂未生成岗位 #{filters['job_id']} 的投递准备：{result.get('reason') or '岗位不满足本地准入条件'}"
                events.append(control_event("工具调用", "未执行", str(result.get("reason") or "岗位不满足本地准入条件。")))
        elif intent_type == "prepare_communication" and filters.get("job_id"):
            result = ensure_communication_preparation_for_job(conn, int(filters["job_id"]), trigger_type="control_chat")
            draft_id = result.get("draft_id")
            if draft_id:
                action = "已生成" if result.get("created") else "已有"
                response_text = (
                    f"{action}岗位 #{filters['job_id']} 的本地沟通准备草稿 #{draft_id}。"
                    f"{result.get('platform_note') or '请在沟通记录页人工审核。'}"
                    "本轮未打开聊天页、未填入消息、未发送消息。"
                )
                events.append(control_event("工具调用", "完成", f"{action}本地沟通准备草稿 #{draft_id}。"))
                events.append(control_event("安全结论", "未执行外部沟通", "未打开聊天页、未填入消息、未发送消息。"))
                evidence["execution"] = {
                    "mode": "chat_direct_local_preparation",
                    "action_type": "communication_preparation",
                    "status": "已完成",
                    "result": result,
                }
            else:
                response_text = f"暂未生成岗位 #{filters['job_id']} 的沟通准备：{result.get('reason') or '岗位不满足本地准入条件'}"
                events.append(control_event("工具调用", "未执行", str(result.get("reason") or "岗位不满足本地准入条件。")))
        elif intent_type == "update_job_status":
            job_id = filters.get("job_id")
            target_status = str(filters.get("status") or "")
            if not isinstance(job_id, int) or job_id <= 0 or target_status not in CONTROL_STATUS_UPDATE_TARGETS:
                response_text = "请指定岗位和目标状态，例如“将岗位 #12 标记为待投递”。"
                events.append(control_event("工具调用", "未执行", "状态变更需要明确岗位编号或当前岗位，以及受支持的目标状态。"))
            else:
                job = conn.execute("SELECT id, company, title, status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
                if not job:
                    response_text = f"没有找到岗位 #{job_id}，未创建状态变更计划。"
                    events.append(control_event("工具调用", "未找到", "岗位记录不存在。"))
                elif str(job["status"] or "") == target_status:
                    response_text = f"岗位 #{job_id} 当前已经是“{target_status}”，无需更新。"
                    events.append(control_event("工具调用", "无需更新", "目标状态与当前状态相同。"))
                else:
                    source_status = str(job["status"] or "待确认")
                    summary = f"将岗位 #{job_id} 从“{source_status}”更新为“{target_status}”。"
                    plan_id = create_control_plan(
                        conn,
                        conversation_id,
                        "job_status_update",
                        summary,
                        {"job_id": job_id, "source_status": source_status, "target_status": target_status},
                    )
                    evidence["plan_id"] = plan_id
                    response_text = (
                        f"已生成本地状态变更计划 #{plan_id}：{summary}"
                        "确认后只更新本地记录；不会访问招聘平台、发送消息或上传简历。"
                    )
                    events.append(control_event("安全结论", "等待确认", "状态变更需要在右侧待确认动作中确认；确认时会重新检查当前状态。"))
        elif intent_type == "ignore_broadcast":
            capture_id = filters.get("capture_id")
            if capture_id:
                summary = f"将对话采集 #{capture_id} 标记为无需回复/群发负样本。"
                plan_id = create_control_plan(conn, conversation_id, "ignore_broadcast", summary, filters)
                evidence["plan_id"] = plan_id
                response_text = f"已生成本地标记计划。{summary}确认后只更新反馈和审计，不会回复或删除对话。"
                events.append(control_event("安全结论", "等待确认", "群发标记会修改本地反馈，等待你确认后执行。"))
            else:
                response_text = "请指定对话采集编号，例如“将对话 #12 的群发消息标为忽略”。"
        elif intent_type == "show_plan":
            response_text = "可直接说“找杭州 Agent 实习，日薪至少 200”执行受控发现，也可以说“检查简历准备情况”“列出高匹配低风险岗位”“检查未读消息”或“识图分析当前页面”。选择岗位后可问“当前岗位下一步怎么做”，或创建状态确认计划；岗位确认到“待面试”后可准备面试。投递、发简历和敏感信息仍需人工确认。"
        elif filters.get("missing_active_job"):
            response_text = "还没有当前岗位。请先在岗位列表确认编号，再说“选择岗位 #编号”。"
        else:
            response_text = "我目前可直接执行：受控岗位搜索、本地岗位短名单、隐私保护的简历就绪检查、用户明确触发的未读标记扫描和视觉页面复核、JD 读取和评分，并可解释、比较或给出岗位下一步建议，创建本地状态确认计划，维护待面试后的本地准备，查看统计和计划。投递、发简历、敏感信息和沟通发送仍会保留人工确认。"
        conn.execute(
            "UPDATE control_conversations SET response_text = ?, evidence_json = ? WHERE id = ?",
            (response_text, dumps(evidence), conversation_id),
        )
        log_agent_action(
            conn,
            action_type="control_request",
            status="已解析",
            summary=response_text,
            decision={
                "intent": intent_type,
                "filters": filters,
                "message_saved_redacted": True,
                "model_called": evidence["parser"] == "llm_json",
                "model_task_type": (evidence.get("model") or {}).get("task_type", ""),
            },
        )
    return control_message_response(conversation_id)


@app.post("/control")
async def create_control_request(request: Request) -> RedirectResponse:
    form = await request.form()
    message = str(form.get("message") or "")
    try:
        conversation = await process_control_message(message)
    except ValueError as exc:
        return redirect_with_notice("/control", str(exc), "error")
    notice_type = "error" if conversation["evidence"].get("execution", {}).get("status") == "失败" else "success"
    return redirect_with_notice("/control", "已完成聊天任务。", notice_type)


@app.post("/api/control/messages")
async def control_message_api(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except ValueError:
        return api_error("请求体必须是 JSON 对象。")
    if not isinstance(payload, dict):
        return api_error("请求体必须是 JSON 对象。")
    try:
        conversation = await process_control_message(str(payload.get("message") or ""))
    except ValueError as exc:
        return api_error(str(exc))
    return JSONResponse({"ok": True, "conversation": conversation})


@app.post("/control/memories/active/clear")
def clear_active_control_job() -> RedirectResponse:
    with connect() as conn:
        deleted = conn.execute("DELETE FROM control_memories WHERE memory_type = 'active_job'").rowcount
        if deleted:
            log_agent_action(
                conn,
                action_type="control_memory",
                status="当前岗位已清除",
                summary="用户清除了当前岗位工作记忆。",
                decision={"memory_type": "active_job", "external_effect": False},
            )
    return redirect_with_notice("/control", "当前岗位工作记忆已清除。" if deleted else "没有可清除的当前岗位。", "success" if deleted else "info")


@app.post("/control/memories/{memory_id}/update")
async def update_control_preference(memory_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    content = str(form.get("content") or "").strip()[:300]
    if not content:
        return redirect_with_notice("/control", "偏好记忆不能为空。", "error")
    if control_memory_contains_sensitive_text(content):
        return redirect_with_notice("/control", "偏好记忆不能保存联系方式、密码、Token 或 API Key。", "error")
    with connect() as conn:
        row = conn.execute("SELECT id FROM control_memories WHERE id = ? AND memory_type = 'preference'", (memory_id,)).fetchone()
        if not row:
            return redirect_with_notice("/control", "偏好记忆不存在或不可编辑。", "error")
        conn.execute("UPDATE control_memories SET value_json = ?, updated_at = ? WHERE id = ?", (dumps({"content": content}), utc_now(), memory_id))
        log_agent_action(
            conn,
            action_type="control_memory",
            status="偏好已更新",
            summary="用户更新了一条本地偏好记忆。",
            decision={"memory_type": "preference", "memory_id": memory_id, "external_effect": False, "content_logged": False},
        )
    return redirect_with_notice("/control", "偏好记忆已更新。", "success")


@app.post("/control/memories/{memory_id}/delete")
def delete_control_preference(memory_id: int) -> RedirectResponse:
    with connect() as conn:
        deleted = conn.execute("DELETE FROM control_memories WHERE id = ? AND memory_type = 'preference'", (memory_id,)).rowcount
        if deleted:
            log_agent_action(
                conn,
                action_type="control_memory",
                status="偏好已删除",
                summary="用户删除了一条本地偏好记忆。",
                decision={"memory_type": "preference", "memory_id": memory_id, "external_effect": False},
            )
    return redirect_with_notice("/control", "偏好记忆已删除。" if deleted else "偏好记忆不存在。", "success" if deleted else "error")


@app.post("/control/plans/{plan_id}/confirm")
async def confirm_control_plan(plan_id: int) -> RedirectResponse:
    with connect() as conn:
        plan = conn.execute("SELECT * FROM control_plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan or plan["status"] != "待确认":
            return redirect_with_notice("/control", "计划不存在或已处理。", "error")
        payload = loads(plan["payload_json"], {})
        conn.execute("UPDATE control_plans SET status = '执行中', confirmed_at = ? WHERE id = ?", (utc_now(), plan_id))
    try:
        if plan["action_type"] == "job_discovery":
            result = await run_in_threadpool(run_controlled_job_discovery, payload)
        elif plan["action_type"] == "job_status_update":
            job_id = int(payload["job_id"])
            source_status = str(payload.get("source_status") or "")
            target_status = str(payload.get("target_status") or "")
            if target_status not in CONTROL_STATUS_UPDATE_TARGETS:
                raise ValueError("计划中的目标状态无效。")
            with connect() as conn:
                job = conn.execute("SELECT id, company, title, status FROM job_postings WHERE id = ?", (job_id,)).fetchone()
                if not job:
                    raise ValueError("没有找到待更新的岗位。")
                current_status = str(job["status"] or "")
                if current_status != source_status:
                    raise ValueError(f"岗位状态已从“{source_status}”变为“{current_status}”，未覆盖，请重新确认。")
                now = utc_now()
                conn.execute("UPDATE job_postings SET status = ?, updated_at = ? WHERE id = ?", (target_status, now, job_id))
                conn.execute(
                    "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                    (job_id, "状态更新", f"控制层确认：{source_status} -> {target_status}", now),
                )
                prep_result = None
                if target_status in INTERVIEW_PREP_TRIGGER_STATUSES:
                    prep_result = ensure_interview_preparation_for_job(
                        conn,
                        job_id,
                        trigger_type="control_plan_confirmation",
                        source_text=f"用户确认将岗位状态更新为“{target_status}”。",
                    )
                result = {
                    "job_id": job_id,
                    "previous_status": source_status,
                    "status": target_status,
                    "interview_preparation": prep_result or {},
                    "model_called": False,
                    "browser_accessed": False,
                    "external_effect": False,
                }
                log_agent_action(
                    conn,
                    action_type="job_status_update",
                    status=target_status,
                    summary=f"控制层确认将岗位状态从“{source_status}”更新为“{target_status}”。",
                    job_id=job_id,
                    decision={
                        "plan_id": plan_id,
                        "source_status": source_status,
                        "target_status": target_status,
                        "user_confirmed": True,
                        "model_called": False,
                        "browser_accessed": False,
                        "external_effect": False,
                    },
                )
        elif plan["action_type"] == "ignore_broadcast":
            capture_id = int(payload["capture_id"])
            with connect() as conn:
                capture = conn.execute("SELECT id FROM conversation_captures WHERE id = ?", (capture_id,)).fetchone()
                if not capture:
                    raise ValueError("没有找到该对话采集。")
                conn.execute("UPDATE conversation_captures SET feedback_status = '正确', expected_message_type = '无需回复', feedback_note = '控制层确认：群发负样本', feedback_updated_at = ? WHERE id = ?", (utc_now(), capture_id))
                log_agent_action(conn, action_type="conversation_feedback", status="无需回复", capture_id=capture_id, summary="控制层确认标记群发负样本。", decision={"source": "control_layer", "message_text_saved": False})
            result = {"capture_id": capture_id, "status": "已标记"}
        else:
            raise ValueError("不支持的计划类型。")
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE control_plans SET status = '失败', result_json = ?, completed_at = ? WHERE id = ?", (dumps({"error": str(exc)[:300]}), utc_now(), plan_id))
        return redirect_with_notice("/control", f"计划执行失败：{str(exc)[:160]}", "error")
    with connect() as conn:
        conn.execute("UPDATE control_plans SET status = '已完成', result_json = ?, completed_at = ? WHERE id = ?", (dumps(result), utc_now(), plan_id))
        log_agent_action(conn, action_type="control_plan", status="已完成", summary=str(plan["summary"]), decision={"plan_id": plan_id, "action_type": plan["action_type"], "result": result, "model_called": False})
    return redirect_with_notice("/control", "已按确认计划调用既有服务。", "success")


@app.post("/control/plans/{plan_id}/cancel")
def cancel_control_plan(plan_id: int) -> RedirectResponse:
    with connect() as conn:
        cursor = conn.execute("UPDATE control_plans SET status = '已取消', completed_at = ? WHERE id = ? AND status = '待确认'", (utc_now(), plan_id))
    return redirect_with_notice("/control", "已取消计划。" if cursor.rowcount else "计划不存在或已处理。", "info")


@app.get("/resumes")
def resumes_page(request: Request) -> Any:
    with connect() as conn:
        profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        resumes = conn.execute("SELECT * FROM resume_versions ORDER BY id").fetchall()
    profile_dict = {key: profile[key] for key in profile.keys()} if profile else {}
    projects = loads(profile_dict.get("projects_json"), []) if profile_dict else []
    preferences = normalized_profile_preferences(loads(profile_dict.get("preferences_json"), {}))
    return templates.TemplateResponse(
        request,
        "resumes.html",
        {
            "profile": profile_dict,
            "resumes": [{key: row[key] for key in row.keys()} for row in resumes],
            "projects": projects if isinstance(projects, list) else [],
            "project_lines": format_project_lines(projects if isinstance(projects, list) else []),
            "preferences": preferences,
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
    try:
        with connect() as conn:
            profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
            if not profile:
                return redirect_with_notice("/resumes", "没有找到候选人画像。", "error")
            preferences = loads(profile["preferences_json"], {})
            if not isinstance(preferences, dict):
                preferences = {}
            if "cities" in form:
                preferences["cities"] = text_line_items(form.get("cities"))
            for field, label in (("min_salary_per_day", "最低日薪"), ("target_salary_per_day", "目标日薪")):
                if field in form:
                    value = optional_positive_int(form.get(field), label)
                    if value is None:
                        preferences.pop(field, None)
                    else:
                        preferences[field] = value
            for field in ("internship_days", "internship_duration"):
                if field in form:
                    value = str(form.get(field) or "").strip()
                    if value:
                        preferences[field] = value
                    else:
                        preferences.pop(field, None)
            if "remote_policy" in form:
                remote_policy = str(form.get("remote_policy") or "接受").strip()
                if remote_policy not in {"接受", "仅远程", "不接受"}:
                    raise ValueError("远程偏好无效。")
                preferences["remote_policy"] = remote_policy
            values = (
                str(form.get("name") or ""),
                str(form.get("education") or ""),
                str(form.get("github_url") or ""),
                str(form.get("demo_url") or ""),
                dumps([item.strip() for item in str(form.get("target_roles") or "").splitlines() if item.strip()]),
                dumps([item.strip() for item in str(form.get("skills") or "").splitlines() if item.strip()]),
                dumps(parse_project_lines(str(form.get("projects") or ""))),
                dumps(preferences),
                now,
            )
            conn.execute(
                """
                UPDATE candidate_profile
                SET name = ?, education = ?, github_url = ?, demo_url = ?,
                    target_roles = ?, skills_json = ?, projects_json = ?, preferences_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (*values, profile["id"]),
            )
    except ValueError as exc:
        return redirect_with_notice("/resumes", str(exc), "error")
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
                   r.name AS resume_name, r.file_path AS resume_file_path, r.file_type AS resume_file_type,
                   LENGTH(r.parsed_text) AS resume_text_length
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
            WHERE recommendation IN ('必投', '可投递')
              AND risk_level IN ('低', '低风险')
              AND status IN ('待确认', '待投递')
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
        f"已检查 {len(results)} 条建议投递且低风险岗位：新增 {created_count} 条投递准备，已有 {existing_count} 条。",
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
    application_message = str(form.get("application_message") or "").strip()[:1500]
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
            SET resume_id = ?, resume_reason = ?, user_note = ?, application_message = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (resume_id, resume_reason, user_note, application_message, status, now, preparation_id),
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


@app.post("/applications/{preparation_id}/browser-fill-note")
async def fill_application_note_in_browser(preparation_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/applications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/applications"
    if str(form.get("confirmation") or "").strip() != "填入投递附言":
        return redirect_with_notice(return_to, "请在确认框中输入“填入投递附言”后再执行。", "error")

    result = await run_in_threadpool(run_application_browser_note_fill, preparation_id)
    notice_type = "success" if result.get("status") == "已填入" else "error"
    return redirect_with_notice(return_to, f"投递附言填入：{result.get('note') or result.get('status')}", notice_type)


@app.post("/applications/{preparation_id}/browser-upload-resume")
async def upload_application_resume_in_browser(preparation_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/applications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/applications"
    if str(form.get("confirmation") or "").strip() != "选择并上传简历":
        return redirect_with_notice(return_to, "请在确认框中输入“选择并上传简历”后再执行。", "error")

    result = await run_in_threadpool(run_application_browser_resume_upload, preparation_id)
    notice_type = "success" if result.get("status") == "已选择简历" else "error"
    return redirect_with_notice(return_to, f"简历文件选择：{result.get('note') or result.get('status')}", notice_type)


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
        unread_scans = conn.execute(
            """
            SELECT platform, status, trigger_type, unread_count, unread_badge_count,
                   signal_types_json, detector_version, created_at
            FROM unread_conversation_scans
            ORDER BY created_at DESC, id DESC
            LIMIT 20
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
            "unread_scans": [{key: row[key] for key in row.keys()} for row in unread_scans],
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


@app.post("/message-drafts/{draft_id}/browser-fill")
async def fill_message_draft_in_browser(draft_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    confirmation = str(form.get("confirmation") or "").strip()
    if confirmation != "填入草稿":
        return redirect_with_notice("/communications", "请在确认框中输入“填入草稿”后再执行。", "error")
    message = str(form.get("message") or "").strip()
    result = await run_in_threadpool(run_communication_browser_fill, draft_id, message)
    notice_type = "success" if result.get("status") == "已填入" else "error"
    return redirect_with_notice("/communications", f"浏览器填入：{result.get('note') or result.get('status')}", notice_type)


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


@app.post("/communication-executor/browser-calibration-dry-run")
async def communication_browser_calibration_dry_run_route(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    result = await run_in_threadpool(run_controlled_edge_chat_page_calibration, "manual_browser")
    notice_type = "success" if result["status"] in {"校准完成", "未发现招聘平台页面"} else "error"
    return redirect_with_notice(return_to, f"聊天页结构校准：{result['note']}", notice_type)


@app.post("/message-patrol/unread-scan")
async def trigger_unread_conversation_scan(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/communications")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/communications"
    result = await run_in_threadpool(run_unread_conversation_scan, "manual_browser")
    notice_type = "success" if result["status"] in {"发现未读", "无未读", "未识别消息列表", "未发现可巡检页面"} else "error"
    return redirect_with_notice(return_to, f"未读会话只读扫描：{result['status']}。{result['note']}", notice_type)


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
    edge_status = controlled_edge_status()
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
        profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
        profile_data = {key: profile[key] for key in profile.keys()} if profile else {}
        discovery_filters = discovery_filters_from_profile(profile_data)
        discovery_plan, _resume_id = controlled_job_discovery_plan(conn)
    return templates.TemplateResponse(
        request,
        "searches.html",
        {
            "runs": [{key: row[key] for key in row.keys()} for row in runs],
            "search_form": search_form,
            "discovery_plan": discovery_plan,
            "discovery_filters": discovery_filters,
            "controlled_edge_status": edge_status,
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


@app.post("/job-discovery/start")
async def start_controlled_job_discovery(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/searches")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/searches"
    try:
        filters = discovery_filters_from_form(form)
        if str(form.get("save_as_profile") or "").lower() in {"1", "true", "on", "yes"}:
            with connect() as conn:
                profile = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
                if not profile:
                    return redirect_with_notice(return_to, "没有找到候选人画像。", "error")
                update_profile_discovery_preferences(conn, profile, filters)
        result = await run_in_threadpool(run_controlled_job_discovery, filters)
    except ValueError as exc:
        return redirect_with_notice(return_to, str(exc), "error")
    except Exception as exc:
        return redirect_with_notice(return_to, f"岗位发现失败：{str(exc)[:180]}", "error")
    notice_type = "success" if result["status"] == "完成" else "warning" if result["status"] == "部分完成" else "error"
    return redirect_with_notice(return_to, f"岗位发现：{result['note']}", notice_type)


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
        feedback_rows = conn.execute(
            """
            SELECT feedback_status, COUNT(*) AS count
            FROM job_candidates
            WHERE search_run_id = ? AND feedback_status != ''
            GROUP BY feedback_status
            """,
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
            "candidate_feedback_statuses": CANDIDATE_FEEDBACK_STATUSES,
            "candidate_expected_screenings": CANDIDATE_EXPECTED_SCREENINGS,
            "candidate_feedback_counts": {row["feedback_status"]: row["count"] for row in feedback_rows},
            "browser_channel_label": browser_channel_label,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


def candidate_calibration_report(conn: Any) -> dict[str, Any]:
    total_candidates = int(conn.execute("SELECT COUNT(*) AS count FROM job_candidates").fetchone()["count"])
    feedback_counts = {
        str(row["feedback_status"]): int(row["count"])
        for row in conn.execute(
            """
            SELECT feedback_status, COUNT(*) AS count
            FROM job_candidates
            WHERE feedback_status != ''
            GROUP BY feedback_status
            """
        ).fetchall()
    }
    reviewed_count = sum(feedback_counts.values())
    platform_rows = conn.execute(
        """
        SELECT
            platform,
            COUNT(*) AS total_count,
            SUM(CASE WHEN feedback_status != '' THEN 1 ELSE 0 END) AS reviewed_count,
            SUM(CASE WHEN feedback_status = '正确' THEN 1 ELSE 0 END) AS correct_count,
            SUM(CASE WHEN feedback_status = '误判' THEN 1 ELSE 0 END) AS misclassified_count,
            SUM(CASE WHEN feedback_status = '待观察' THEN 1 ELSE 0 END) AS observing_count
        FROM job_candidates
        GROUP BY platform
        ORDER BY misclassified_count DESC, reviewed_count DESC, total_count DESC, platform
        """
    ).fetchall()
    platform_summary = []
    for row in platform_rows:
        correct_count = int(row["correct_count"] or 0)
        misclassified_count = int(row["misclassified_count"] or 0)
        settled_count = correct_count + misclassified_count
        platform_summary.append(
            {
                "platform": str(row["platform"] or "平台待补充"),
                "total_count": int(row["total_count"] or 0),
                "reviewed_count": int(row["reviewed_count"] or 0),
                "correct_count": correct_count,
                "misclassified_count": misclassified_count,
                "observing_count": int(row["observing_count"] or 0),
                "precision_percent": round(correct_count / settled_count * 100) if settled_count else None,
            }
        )
    expected_rows = conn.execute(
        """
        SELECT expected_screening, COUNT(*) AS count
        FROM job_candidates
        WHERE feedback_status = '误判' AND expected_screening != ''
        GROUP BY expected_screening
        ORDER BY count DESC, expected_screening
        """
    ).fetchall()
    recent_misclassifications = conn.execute(
        """
        SELECT c.*, r.platform AS run_platform, r.keyword AS run_keyword, r.city AS run_city
        FROM job_candidates c
        LEFT JOIN job_search_runs r ON r.id = c.search_run_id
        WHERE c.feedback_status = '误判'
        ORDER BY c.feedback_updated_at DESC, c.id DESC
        LIMIT 20
        """
    ).fetchall()
    detail_pending_count = int(
        conn.execute("SELECT COUNT(*) AS count FROM job_candidates WHERE status = '详情待补充'").fetchone()["count"]
    )
    suggestions: list[str] = []
    if reviewed_count < 5:
        suggestions.append("当前已标注样本不足 5 条，先在不同平台各积累几条正确和误判反馈，再决定是否调整筛选规则。")
    if feedback_counts.get("误判", 0):
        suggestions.append(f"有 {feedback_counts['误判']} 条候选被标记为误判，优先复核下方样本的标题、摘要和期望分流。")
    if detail_pending_count:
        suggestions.append(f"有 {detail_pending_count} 条候选仍为“详情待补充”，这反映详情页读取覆盖，不等同于筛选准确率。")
    if not suggestions:
        suggestions.append("还没有足够的候选校准反馈。搜索后可在候选详情中标记正确、误判或待观察。")
    return {
        "total_candidates": total_candidates,
        "reviewed_count": reviewed_count,
        "feedback_counts": feedback_counts,
        "platform_summary": platform_summary,
        "expected_summary": [{"expected_screening": str(row["expected_screening"]), "count": int(row["count"])} for row in expected_rows],
        "recent_misclassifications": [{key: row[key] for key in row.keys()} for row in recent_misclassifications],
        "detail_pending_count": detail_pending_count,
        "suggestions": suggestions,
    }


@app.get("/calibration/candidates")
def candidate_calibration_page(request: Request) -> Any:
    with connect() as conn:
        report = candidate_calibration_report(conn)
    return templates.TemplateResponse(
        request,
        "candidate_calibration.html",
        {
            "report": report,
            "notice": request.query_params.get("notice", ""),
            "notice_type": request.query_params.get("notice_type", "info"),
        },
    )


@app.post("/candidates/{candidate_id}/feedback")
async def update_candidate_feedback(candidate_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    feedback_status = str(form.get("feedback_status") or "").strip()
    expected_screening = str(form.get("expected_screening") or "").strip()
    feedback_note = str(form.get("feedback_note") or "").strip()[:1000]
    if feedback_status not in CANDIDATE_FEEDBACK_STATUSES:
        return redirect_with_notice("/searches", "候选反馈状态无效。", "error")
    if expected_screening not in CANDIDATE_EXPECTED_SCREENINGS:
        return redirect_with_notice("/searches", "期望分流无效。", "error")

    now = utc_now()
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_candidates WHERE id = ?", (candidate_id,)).fetchone()
        if not row:
            return redirect_with_notice("/searches", "没有找到该候选岗位。", "error")
        run_id = int(row["search_run_id"])
        conn.execute(
            """
            UPDATE job_candidates
            SET feedback_status = ?, expected_screening = ?, feedback_note = ?, feedback_updated_at = ?
            WHERE id = ?
            """,
            (feedback_status, expected_screening, feedback_note, now, candidate_id),
        )
        log_agent_action(
            conn,
            action_type="job_candidate_feedback",
            status=feedback_status or "已清除",
            summary=f"候选校准：{row['title'] or '候选岗位'}",
            platform=str(row["platform"] or ""),
            job_id=int(row["job_id"]) if row["job_id"] else None,
            decision={
                "candidate_id": candidate_id,
                "candidate_status": row["status"],
                "expected_screening": expected_screening,
                "feedback_note_length": len(feedback_note),
            },
        )
    return redirect_with_notice(f"/searches/{run_id}", "候选校准反馈已保存。", "success")


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
        if fetch_mode == "controlled_edge":
            fetched, detail_metadata = await run_in_threadpool(fetch_discovery_candidate_detail, candidate)
        else:
            fetched = await run_in_threadpool(fetch_job_from_url, candidate["source_url"], fetch_mode=fetch_mode, browser_channel=channel)
            detail_metadata = {"visual_detail_fallback": False}
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
                event_content = f"从搜索候选 {candidate.get('source_url')} 刷新已有岗位详情。{fetched.note}"
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
                event_content = f"从搜索候选 {candidate.get('source_url')} 导入岗位详情。{fetched.note}"
            now = utc_now()
            conn.execute(
                "UPDATE job_candidates SET job_id = ?, status = ?, error_message = '', updated_at = ? WHERE id = ?",
                (job_id, "已导入", now, candidate_id),
            )
            conn.execute(
                "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
                (job_id, event_type, event_content, now),
            )
        notice = f"已从候选岗位导入并通过{fetch_mode_label(fetched.fetch_mode)}完成分析。"
        if detail_metadata.get("visual_detail_fallback"):
            notice += " DOM 文本不足，已使用视觉复核补充 JD。"
        return redirect_with_notice(f"/jobs/{job_id}", notice, "success")
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


def compact_interview_items(value: object, *, limit: int, item_limit: int = 300) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for raw in value:
        item = str(raw or "").strip()
        if item and item not in items:
            items.append(item[:item_limit])
        if len(items) >= limit:
            break
    return items


def redact_llm_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱已省略]", text)
    text = re.sub(r"(?<!\d)(?:1[3-9]\d{9}|\+?86[-\s]?)?(?:1[3-9]\d{9})(?!\d)", "[手机号已省略]", text)
    return text[:limit]


def candidate_context_for_job(conn: Any, job: dict[str, Any] | None) -> dict[str, Any]:
    profile_row = conn.execute("SELECT * FROM candidate_profile ORDER BY id LIMIT 1").fetchone()
    profile = {key: profile_row[key] for key in profile_row.keys()} if profile_row else {}
    job_id = int(job["id"]) if job and job.get("id") else None
    selected_resume_id = int(job["selected_resume_id"]) if job and job.get("selected_resume_id") else None
    preparation_resume = None
    if job_id:
        preparation_resume = conn.execute(
            """
            SELECT r.*
            FROM application_preparations p
            JOIN resume_versions r ON r.id = p.resume_id
            WHERE p.job_id = ? AND p.resume_id IS NOT NULL
            ORDER BY CASE p.status WHEN '已确认' THEN 0 ELSE 1 END, p.updated_at DESC, p.id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    if preparation_resume:
        resume = {key: preparation_resume[key] for key in preparation_resume.keys()}
    elif selected_resume_id:
        resume_row = conn.execute("SELECT * FROM resume_versions WHERE id = ?", (selected_resume_id,)).fetchone()
        resume = {key: resume_row[key] for key in resume_row.keys()} if resume_row else {}
    else:
        resume = {}

    projects: list[dict[str, Any]] = []
    for raw_project in loads(profile.get("projects_json"), []):
        if not isinstance(raw_project, dict):
            continue
        name = str(raw_project.get("name") or "").strip()[:160]
        highlights = compact_interview_items(raw_project.get("highlights"), limit=5, item_limit=240)
        if name or highlights:
            projects.append({"name": name, "highlights": highlights})
        if len(projects) >= 6:
            break

    return {
        "education": str(profile.get("education") or "").strip()[:300],
        "target_roles": compact_interview_items(loads(profile.get("target_roles"), []), limit=8, item_limit=100),
        "skills": compact_interview_items(loads(profile.get("skills_json"), []), limit=30, item_limit=80),
        "projects": projects,
        "resume_version": str(resume.get("name") or "").strip()[:160],
        "resume_target_role": str(resume.get("target_role") or "").strip()[:160],
        "resume_evidence": redact_llm_text(str(resume.get("parsed_text") or ""), 6000),
    }


def normalized_interview_job_context(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {}
    extracted = job.get("extracted") if isinstance(job.get("extracted"), dict) else {}
    return {
        "title": str(job.get("title") or extracted.get("title") or "").strip()[:240],
        "company": str(job.get("company") or extracted.get("company") or "").strip()[:240],
        "city": str(job.get("city") or extracted.get("city") or "").strip()[:120],
        "required_skills": compact_interview_items(extracted.get("required_skills"), limit=30, item_limit=100),
        "bonus_skills": compact_interview_items(extracted.get("bonus_skills"), limit=20, item_limit=100),
        "responsibilities": compact_interview_items(extracted.get("responsibilities"), limit=15, item_limit=300),
        "requirements": compact_interview_items(extracted.get("requirements"), limit=15, item_limit=300),
        "jd_text": redact_llm_text(strip_platform_safety_notice(str(job.get("jd_text") or "")), 12000),
    }


def interview_prep_fallback(review: dict[str, Any], job: dict[str, Any] | None, feedback_context: str) -> dict[str, Any]:
    generated = build_interview_review(job, "\n\n".join(item for item in [str(review.get("source_text") or "").strip(), feedback_context] if item))
    saved_plan = loads(review.get("prep_plan_json"), {})
    saved_questions = loads(review.get("question_bank_json"), [])
    three_day_plan = compact_interview_items(saved_plan.get("three_day_plan") if isinstance(saved_plan, dict) else [], limit=5)
    seven_day_plan = compact_interview_items(saved_plan.get("seven_day_plan") if isinstance(saved_plan, dict) else [], limit=10)
    questions = compact_interview_items(saved_questions, limit=15, item_limit=400)
    return {
        "plan": {
            "title": str((saved_plan if isinstance(saved_plan, dict) else {}).get("title") or generated["plan"]["title"]),
            "three_day_plan": three_day_plan or generated["plan"]["three_day_plan"],
            "seven_day_plan": seven_day_plan or generated["plan"]["seven_day_plan"],
        },
        "questions": questions or generated["questions"],
        "markdown": str(review.get("review_markdown") or generated["markdown"]).strip(),
    }


def normalize_interview_prep_generation(result: object, fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("模型没有返回 JSON 对象。")
    plan_source = result.get("plan") if isinstance(result.get("plan"), dict) else result
    three_day_plan = compact_interview_items(plan_source.get("three_day_plan"), limit=5)
    seven_day_plan = compact_interview_items(plan_source.get("seven_day_plan"), limit=10)
    questions = compact_interview_items(result.get("questions"), limit=15, item_limit=400)
    markdown = str(result.get("markdown") or result.get("review_markdown") or "").strip()[:16000]
    model_fields = [
        name
        for name, value in {
            "three_day_plan": three_day_plan,
            "seven_day_plan": seven_day_plan,
            "questions": questions,
            "markdown": markdown,
        }.items()
        if value
    ]
    if not model_fields:
        raise ValueError("模型没有返回可用的面试准备内容。")
    fallback_plan = fallback["plan"]
    return {
        "plan": {
            "title": str(plan_source.get("title") or fallback_plan["title"]).strip()[:240],
            "three_day_plan": three_day_plan or fallback_plan["three_day_plan"],
            "seven_day_plan": seven_day_plan or fallback_plan["seven_day_plan"],
        },
        "questions": questions or fallback["questions"],
        "markdown": markdown or fallback["markdown"],
        "model_fields": model_fields,
    }


def run_interview_preparation_enhancement(review_id: int) -> dict[str, Any]:
    with connect() as conn:
        review_row = conn.execute("SELECT * FROM interview_preparations WHERE id = ?", (review_id,)).fetchone()
        if not review_row:
            return {"status": "未找到", "note": "没有找到这条面试准备记录。"}
        review = {key: review_row[key] for key in review_row.keys()}
        job_row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (review["job_id"],)).fetchone() if review.get("job_id") else None
        job = parse_json_fields({key: job_row[key] for key in job_row.keys()}) if job_row else None
        feedback_context = interview_feedback_context(conn, int(review["job_id"]) if review.get("job_id") else None)
        candidate = candidate_context_for_job(conn, job)
        fallback = interview_prep_fallback(review, job, feedback_context)

    client = client_for_task("interview_prep")
    if not client or not client.configured:
        result = {
            "status": "未配置",
            "note": "请先在设置中为“面试准备”配置可用模型，再生成智能强化准备。",
            "model_called": False,
        }
    else:
        payload = {
            "job": normalized_interview_job_context(job),
            "candidate": candidate,
            "interview_context": {
                "source_text": redact_llm_text(str(review.get("source_text") or ""), 12000),
                "unresolved_feedback": redact_llm_text(feedback_context, 4000),
                "existing_questions": fallback["questions"],
            },
        }
        prompt = (
            "你是 AI 应用开发实习的面试教练。根据岗位 JD、候选人明确提供的技能/项目事实和历史薄弱点，"
            "生成有针对性的中文面试准备。不得编造候选人经历、公司业务、技术指标或掌握程度；"
            "候选人资料未覆盖的技术只能标为待学习或待确认。"
            "只输出 JSON：three_day_plan(3-5 条数组)、seven_day_plan(5-7 条数组)、"
            "questions(8-15 条问题数组)、markdown(包含岗位重点、项目表达、模拟题和复习安排的 Markdown)。"
            "问题要覆盖 JD 技术栈、项目追问、工程实践和行为面试，优先历史薄弱点。"
        )
        try:
            generated = client.complete_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": dumps(payload)},
                ]
            )
            result = {
                "status": "已增强",
                "note": "已基于岗位、候选人事实和薄弱点更新本地面试准备。",
                "content": normalize_interview_prep_generation(generated, fallback),
                "model_called": True,
            }
        except Exception as exc:
            if hasattr(client, "log_error"):
                client.log_error(str(exc))
            result = {
                "status": "未增强",
                "note": f"智能面试准备生成失败：{str(exc)[:240]}",
                "model_called": True,
            }

    with connect() as conn:
        current = conn.execute("SELECT job_id FROM interview_preparations WHERE id = ?", (review_id,)).fetchone()
        if not current:
            return {"status": "未找到", "note": "面试准备已不存在，未写入生成结果。"}
        job_id = int(current["job_id"]) if current["job_id"] else None
        if result.get("content"):
            content = result["content"]
            conn.execute(
                """
                UPDATE interview_preparations
                SET prep_plan_json = ?, question_bank_json = ?, review_markdown = ?, updated_at = ?
                WHERE id = ?
                """,
                (dumps(content["plan"]), dumps(content["questions"]), content["markdown"], utc_now(), review_id),
            )
        log_agent_action(
            conn,
            action_type="interview_prep_enhance",
            status=str(result["status"]),
            summary=str(result["note"]),
            platform=str((job or {}).get("platform") or ""),
            job_id=job_id,
            decision={
                "review_id": review_id,
                "model_called": bool(result.get("model_called")),
                "updated": bool(result.get("content")),
                "model_fields": list((result.get("content") or {}).get("model_fields") or []),
                "user_triggered": True,
                "input_redacted": True,
            },
        )
    return result


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
    recommendation = str(job.get("recommendation") or "")
    risk_level = str(job.get("risk_level") or "")
    if recommendation not in APPLICATION_ELIGIBLE_RECOMMENDATIONS:
        return False, "该岗位当前不是“必投”或“可投递”建议，暂不自动进入投递准备。"
    if risk_level not in APPLICATION_LOW_RISK_LEVELS:
        return False, "该岗位风险不是“低/低风险”，需要人工确认后再决定是否投递。"
    if job.get("status") not in {"待确认", "待投递"}:
        return False, f"岗位当前状态为“{job.get('status') or '未设置'}”，不在待投递阶段。"
    return True, "岗位建议投递且风险低，可进入投递准备。"


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
    parts = [
        f"匹配分 {job.get('match_score') or 0}，当前分析建议为“{job.get('recommendation') or '待确认'}”，"
        f"风险为“{job.get('risk_level') or '待确认'}”。"
    ]
    if matched_skills:
        parts.append("已匹配：" + "、".join(matched_skills[:6]) + "。")
    if missing_skills:
        parts.append("投递前仍需确认：" + "、".join(missing_skills[:4]) + "。")
    return "".join(parts)


def local_match_review_context(job: dict[str, Any]) -> dict[str, Any]:
    scoring = (job.get("extracted") or {}).get("scoring") or {}
    return {
        "score": int(job.get("match_score") or 0),
        "match_level": str(job.get("match_level") or ""),
        "recommendation": str(job.get("recommendation") or ""),
        "risk_level": str(job.get("risk_level") or ""),
        "matching_evidence": str(scoring.get("matching_evidence") or ""),
        "matched_skills": compact_interview_items(scoring.get("matched_skills"), limit=20, item_limit=100),
        "missing_skills": compact_interview_items(scoring.get("missing_skills"), limit=20, item_limit=100),
        "fit_notes": compact_interview_items(scoring.get("fit_notes"), limit=12, item_limit=260),
        "preference_notes": compact_interview_items(scoring.get("preference_notes"), limit=12, item_limit=260),
        "risk_signals": compact_interview_items(scoring.get("risk_signals"), limit=12, item_limit=160),
        "caution_signals": compact_interview_items(scoring.get("caution_signals"), limit=12, item_limit=160),
    }


def normalize_job_match_review(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("模型没有返回 JSON 对象。")
    conclusion = str(result.get("conclusion") or result.get("summary") or "").strip()[:1400]
    matched_evidence = compact_interview_items(result.get("matched_evidence"), limit=10, item_limit=360)
    gaps = compact_interview_items(result.get("gaps"), limit=10, item_limit=360)
    questions_to_confirm = compact_interview_items(result.get("questions_to_confirm"), limit=8, item_limit=360)
    caution_points = compact_interview_items(result.get("caution_points"), limit=8, item_limit=360)
    resume_focus = compact_interview_items(result.get("resume_focus"), limit=8, item_limit=360)
    populated_fields = [
        name
        for name, value in {
            "conclusion": conclusion,
            "matched_evidence": matched_evidence,
            "gaps": gaps,
            "questions_to_confirm": questions_to_confirm,
            "caution_points": caution_points,
            "resume_focus": resume_focus,
        }.items()
        if value
    ]
    if not populated_fields:
        raise ValueError("模型没有返回可用的匹配复核内容。")
    return {
        "conclusion": conclusion,
        "matched_evidence": matched_evidence,
        "gaps": gaps,
        "questions_to_confirm": questions_to_confirm,
        "caution_points": caution_points,
        "resume_focus": resume_focus,
        "model_fields": populated_fields,
    }


def run_job_match_review(job_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return {"status": "未找到", "note": "没有找到这个岗位。", "model_called": False}
        job = parse_json_fields({key: row[key] for key in row.keys()})
        candidate = candidate_context_for_job(conn, job)

    client = client_for_task("job_match")
    if not client or not client.configured:
        result = {
            "status": "未配置",
            "note": "请先在设置中为“岗位匹配解释”配置可用模型，再进行深度匹配复核。",
            "model_called": False,
        }
    else:
        payload = {
            "job": normalized_interview_job_context(job),
            "candidate": candidate,
            "local_rule_result": local_match_review_context(job),
        }
        prompt = (
            "你是 AI 应用开发实习的岗位匹配复核助手。基于岗位 JD、候选人明确填写的技能/项目/简历事实和本地规则结果，"
            "只给出可由用户复核的补充意见。不得编造候选人经历、公司业务、技术指标或技能熟练度；"
            "候选人资料没有覆盖的内容必须描述为待学习、待确认或能力缺口。"
            "不得替用户投递、联系招聘方、修改岗位分数、风险等级、推荐或状态，也不得淡化本地规则识别到的风险。"
            "只输出 JSON：conclusion(string)、matched_evidence(array)、gaps(array)、questions_to_confirm(array)、"
            "caution_points(array)、resume_focus(array)。"
        )
        try:
            generated = client.complete_json(
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": dumps(payload)},
                ]
            )
            result = {
                "status": "已完成",
                "note": "已生成补充性深度匹配复核，本地评分和岗位状态未改变。",
                "content": normalize_job_match_review(generated),
                "model_called": True,
                "model_profile": str(client.profile.get("name") or ""),
                "model_name": str(client.model or ""),
            }
        except Exception as exc:
            if hasattr(client, "log_error"):
                client.log_error(str(exc))
            result = {
                "status": "未完成",
                "note": f"深度匹配复核生成失败：{str(exc)[:240]}",
                "model_called": True,
            }

    with connect() as conn:
        current = conn.execute("SELECT platform FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        if not current:
            return {"status": "未找到", "note": "岗位已不存在，未写入复核结果。", "model_called": bool(result.get("model_called"))}
        if result.get("content"):
            conn.execute(
                """
                INSERT INTO job_match_reviews (job_id, status, review_json, model_profile, model_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(result["status"]),
                    dumps(result["content"]),
                    str(result.get("model_profile") or ""),
                    str(result.get("model_name") or ""),
                    utc_now(),
                ),
            )
        log_agent_action(
            conn,
            action_type="job_match_review",
            status=str(result["status"]),
            summary=str(result["note"]),
            platform=str(current["platform"] or ""),
            job_id=job_id,
            decision={
                "model_called": bool(result.get("model_called")),
                "stored_review": bool(result.get("content")),
                "model_fields": list((result.get("content") or {}).get("model_fields") or []),
                "user_triggered": True,
                "input_redacted": True,
                "local_score_changed": False,
                "job_status_changed": False,
            },
        )
    return result


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


def communication_preparation_platform_note(platform: str) -> tuple[str, str]:
    if is_pc_message_automation_platform(platform):
        return "supported", f"{platform} 可在沟通记录页人工审核后进入既有的受控填入确认流程。"
    if platform == "实习僧":
        return "manual_only", "实习僧 PC 端暂不支持消息填入或发送，草稿仅供复制后在移动端人工处理。"
    if platform in {"智联招聘", "前程无忧"}:
        return "manual_only", f"{platform} 尚未启用可验证的 PC 消息自动化，草稿仅供人工处理。"
    return "manual_only", "该平台尚未配置消息自动化，草稿仅供人工处理。"


def communication_preparation_message(job: dict[str, Any]) -> tuple[str, list[str]]:
    extracted = job.get("extracted") if isinstance(job.get("extracted"), dict) else {}
    title = str(job.get("title") or extracted.get("title") or "该实习岗位").strip()
    required_skills = [str(item).strip() for item in extracted.get("required_skills") or [] if str(item).strip()]
    questions = ["岗位的主要工作内容和团队当前优先推进的方向"]
    if required_skills:
        questions.append(f"实际会使用的技术栈，以及 {'、'.join(required_skills[:3])} 在项目中的应用场景")
    else:
        questions.append("实际会使用的技术栈和协作方式")
    if not str(extracted.get("internship_days") or "").strip() or not str(extracted.get("internship_duration") or "").strip():
        questions.append("实习周期和每周到岗要求")
    message = f"您好，我对「{title}」实习岗位很感兴趣，想进一步了解一下{'、'.join(questions)}。如果方便，期待和您沟通，谢谢！"
    return message, questions


def ensure_communication_preparation_for_job(conn: Any, job_id: int, *, trigger_type: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {"created": False, "draft_id": None, "reason": "岗位不存在。"}

    job = parse_json_fields({key: row[key] for key in row.keys()})
    eligible, reason = application_preparation_eligibility(job)
    if not eligible:
        return {"created": False, "draft_id": None, "reason": reason}

    platform = str(job.get("platform") or "")
    capability, platform_note = communication_preparation_platform_note(platform)
    existing = conn.execute(
        """
        SELECT id, status FROM message_drafts
        WHERE job_id = ? AND draft_type = '聊天沟通准备' AND status = '待确认'
        ORDER BY id DESC LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if existing:
        return {
            "created": False,
            "draft_id": int(existing["id"]),
            "status": str(existing["status"] or "待确认"),
            "platform_capability": capability,
            "platform_note": platform_note,
        }

    message, questions = communication_preparation_message(job)
    now = utc_now()
    reason_text = "聊天指令生成的本地沟通准备；仅基于岗位记录与 JD 提问点，不来自真实 HR 对话。草稿模式下需人工审核。"
    cursor = conn.execute(
        """
        INSERT INTO message_drafts (
            job_id, platform, draft_type, status, communication_mode,
            followup_index, followup_limit, reason, message, risk_flags_json,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            platform,
            "聊天沟通准备",
            "待确认",
            "draft",
            0,
            0,
            reason_text,
            message,
            dumps(["未采集真实 HR 对话；草稿不能视为已获得沟通邀请。"]),
            now,
            now,
        ),
    )
    draft_id = int(cursor.lastrowid)
    label = " - ".join(item for item in [job.get("company"), job.get("title")] if item) or f"岗位 {job_id}"
    conn.execute(
        "INSERT INTO application_events (job_id, event_type, content, created_at) VALUES (?, ?, ?, ?)",
        (job_id, "沟通准备生成", f"{label} 已生成本地沟通准备草稿，未发送消息。", now),
    )
    log_agent_action(
        conn,
        action_type="communication_preparation",
        status="待确认",
        summary=f"已生成本地沟通准备：{label}",
        platform=platform,
        job_id=job_id,
        draft_id=draft_id,
        decision={
            "trigger_type": trigger_type,
            "draft_type": "聊天沟通准备",
            "communication_mode": "draft",
            "question_count": len(questions),
            "platform_capability": capability,
            "browser_opened": False,
            "message_filled": False,
            "message_sent": False,
            "model_called": False,
            "hr_conversation_used": False,
        },
    )
    return {
        "created": True,
        "draft_id": draft_id,
        "platform_capability": capability,
        "platform_note": platform_note,
        "message_length": len(message),
        "question_count": len(questions),
    }


def application_browser_item(conn: Any, preparation_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT p.id AS preparation_id, p.status AS preparation_status, p.resume_id, p.application_message,
               j.id AS job_id, j.platform, j.source_url, j.title AS job_title,
               j.company, j.status AS job_status, r.name AS resume_name,
               r.file_path AS resume_file_path, r.file_type AS resume_file_type,
               LENGTH(r.parsed_text) AS resume_text_length,
               (SELECT name FROM candidate_profile ORDER BY id LIMIT 1) AS candidate_name
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


def run_application_browser_note_fill(preparation_id: int) -> dict[str, Any]:
    with connect() as conn:
        item = application_browser_item(conn, preparation_id)
    if not item:
        return {"status": "未找到投递准备", "note": "没有找到这条投递准备。", "preparation_id": preparation_id}

    message = str(item.get("application_message") or "").strip()
    if not message:
        return {"status": "未填入", "note": "请先在投递准备中填写投递附言。", "preparation_id": preparation_id}

    plan = build_application_browser_plan(item)
    try:
        result = fill_application_note_in_controlled_edge(plan, message)
    except ValueError as exc:
        result = {
            "status": "未填入",
            "note": str(exc)[:500],
            "application_message_filled": False,
            "browser_clicked": False,
            "resume_uploaded": False,
        }

    with connect() as conn:
        log_agent_action(
            conn,
            action_type="application_browser_fill",
            status=str(result.get("status") or "未知"),
            summary=str(result.get("note") or "投递附言浏览器填入完成。"),
            platform=str(item.get("platform") or ""),
            job_id=int(item["job_id"]),
            decision={
                "preparation_id": preparation_id,
                "application_message_length": len(message),
                "filled_selector": result.get("filled_selector") or "",
                "application_message_filled": bool(result.get("application_message_filled")),
                "browser_clicked": False,
                "resume_uploaded": False,
                "model_called": False,
                "user_confirmation_required": True,
            },
        )
    return result


def run_application_browser_resume_upload(preparation_id: int) -> dict[str, Any]:
    with connect() as conn:
        item = application_browser_item(conn, preparation_id)
    if not item:
        return {"status": "未找到投递准备", "note": "没有找到这条投递准备。", "preparation_id": preparation_id}
    if not str(item.get("candidate_name") or "").strip():
        return {
            "status": "未选择",
            "note": "候选人名称尚未填写，停止选择简历文件。请先在候选人画像页补全后再确认。",
            "resume_uploaded": False,
            "browser_clicked": False,
        }
    if int(item.get("resume_text_length") or 0) < 160:
        return {
            "status": "未选择",
            "note": "当前简历版本正文未导入或过短，停止选择简历文件。请先完善简历版本。",
            "resume_uploaded": False,
            "browser_clicked": False,
        }

    plan = build_application_browser_plan(item)
    try:
        result = upload_application_resume_in_controlled_edge(plan, str(item.get("resume_file_path") or ""))
    except ValueError as exc:
        result = {
            "status": "未选择",
            "note": str(exc)[:500],
            "file_selection_verified": False,
            "browser_clicked": False,
            "resume_uploaded": False,
            "resume_path_saved": False,
        }

    with connect() as conn:
        log_agent_action(
            conn,
            action_type="application_browser_resume_upload",
            status=str(result.get("status") or "未知"),
            summary=str(result.get("note") or "投递简历文件选择完成。"),
            platform=str(item.get("platform") or ""),
            job_id=int(item["job_id"]),
            decision={
                "preparation_id": preparation_id,
                "resume_id": item.get("resume_id"),
                "resume_file_type": str(result.get("resume_suffix") or item.get("resume_file_type") or ""),
                "resume_size_bytes": int(result.get("resume_size_bytes") or 0),
                "file_input_count": int(result.get("file_input_count") or 0),
                "file_selection_verified": bool(result.get("file_selection_verified")),
                "resume_uploaded": bool(result.get("resume_uploaded")),
                "resume_path_saved": False,
                "browser_clicked": False,
                "application_submitted": False,
                "model_called": False,
                "user_confirmation_required": True,
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
        match_review = conn.execute(
            "SELECT * FROM job_match_reviews WHERE job_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
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
    match_review_data = {key: match_review[key] for key in match_review.keys()} if match_review else None
    if match_review_data is not None:
        match_review_data["content"] = loads(match_review_data.get("review_json"), {})
    application_eligible, application_block_reason = application_preparation_eligibility(job_data)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job_data,
            "research": [{key: row[key] for key in row.keys()} for row in research],
            "match_review": match_review_data,
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


@app.post("/jobs/{job_id}/match-review")
async def review_job_match(job_id: int) -> RedirectResponse:
    result = await run_in_threadpool(run_job_match_review, job_id)
    notice_type = "success" if result.get("status") == "已完成" else "error"
    return redirect_with_notice(f"/jobs/{job_id}", str(result.get("note") or result.get("status")), notice_type)


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
        previous_company = normalized_company_name(str(job.get("company") or ""))
        updated_company = normalized_company_name(str(extracted.get("company") or ""))
        if previous_company != updated_company:
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
    return redirect_with_notice(f"/jobs/{job_id}", "已重新分析岗位。", "success")


@app.post("/jobs/{job_id}/company-research")
async def research_company_for_job(job_id: int, request: Request) -> RedirectResponse:
    form = await request.form()
    requested_depth = str(form.get("search_depth") or "auto").strip()
    if requested_depth not in {"auto", "quick", "standard", "deep"}:
        return redirect_with_notice(f"/jobs/{job_id}", "检索深度无效，未查询公司风险。", "error")

    with connect() as conn:
        row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return redirect_with_notice("/jobs", "没有找到这个岗位。", "error")
        job = {key: row[key] for key in row.keys()}
        company = str(job.get("company") or "").strip()
        if not company:
            return redirect_with_notice(f"/jobs/{job_id}", "公司名称为空，请在重新分析中补充后再查询。", "error")

        search_depth = company_research_depth(job, requested_depth)
        result_count = insert_company_research(
            conn,
            job_id,
            company,
            str(job.get("title") or ""),
            str(job.get("city") or ""),
            search_depth,
            replace_existing=True,
        )
        log_agent_action(
            conn,
            action_type="company_risk_research",
            status="完成" if result_count else "无结果",
            summary=f"公司公开风险查询：{company}，保存 {result_count} 条来源。",
            platform=str(job.get("platform") or ""),
            job_id=job_id,
            decision={
                "company": company,
                "search_depth": search_depth,
                "source_count": result_count,
                "model_called": False,
                "user_triggered": True,
            },
        )
    if result_count:
        return redirect_with_notice(f"/jobs/{job_id}", f"已查询公司公开风险信息，保存 {result_count} 条来源。", "success")
    return redirect_with_notice(f"/jobs/{job_id}", "未找到可保存的公司公开资料，已保留原有查询结果。", "warning")


@app.post("/jobs/{job_id}/rescore")
def rescore_job(job_id: int) -> RedirectResponse:
    try:
        with connect() as conn:
            rescore_saved_job(conn, job_id)
    except ValueError as exc:
        return redirect_with_notice("/jobs", str(exc), "error")
    return redirect_with_notice(f"/jobs/{job_id}", "已按当前本地规则重新评分，未调用模型。", "success")


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


@app.post("/interviews/{review_id}/enhance")
async def enhance_interview_preparation(review_id: int) -> RedirectResponse:
    result = await run_in_threadpool(run_interview_preparation_enhancement, review_id)
    notice_type = "success" if result.get("status") == "已增强" else "error"
    return redirect_with_notice(f"/interviews/{review_id}", str(result.get("note") or result.get("status")), notice_type)


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


@app.get("/interviews/{review_id}/download.pdf")
def download_interview_review_pdf(review_id: int) -> Response:
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
    try:
        pdf_bytes = render_interview_review_pdf(str(row["title"] or "interview-review"), str(row["review_markdown"] or ""))
    except ValueError as exc:
        return Response(str(exc), status_code=503, media_type="text/plain; charset=utf-8")
    filename = safe_filename(row["title"] or "interview-review") + ".pdf"
    return Response(
        pdf_bytes,
        media_type="application/pdf",
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


@app.post("/communications/autonomous/start")
async def start_autonomous_communication_route(request: Request) -> RedirectResponse:
    form = await request.form()
    return_to = str(form.get("return_to") or "/")
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/"
    try:
        result = await run_in_threadpool(start_autonomous_communication_workflow, str(form.get("start_url") or ""))
    except Exception as exc:
        return redirect_with_notice(return_to, f"未能启动自主沟通：{str(exc)[:180]}", "error")
    return redirect_with_notice(
        return_to,
        f"自主沟通已启动。已打开受控 Edge，首次巡检将在约 30 秒后开始。",
        "success",
    )


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
