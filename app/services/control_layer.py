from __future__ import annotations

import re
from typing import Any


CITY_NAMES = ("北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京")
ROLE_NAMES = ("AI 应用开发实习", "Agent 开发实习", "AI 后端实习", "RAG 开发实习")
ALLOWED_CONTROL_INTENTS = (
    "search_draft",
    "stats",
    "explain_job",
    "prepare_application",
    "prepare_communication",
    "ignore_broadcast",
    "show_plan",
    "help",
)
CONTROL_MEMORY_SECRET_PATTERN = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|(?:api[_\s-]*key|password|密码|token)\s*[:=]|bearer\s+[a-z0-9._-]{12,})")


def normalize_model_control_intent(value: Any) -> dict[str, Any] | None:
    """Accept only the narrow intent schema that the policy layer understands."""
    if not isinstance(value, dict):
        return None
    intent_type = str(value.get("type") or "").strip()
    if intent_type not in ALLOWED_CONTROL_INTENTS:
        return None
    raw_filters = value.get("filters")
    filters = raw_filters if isinstance(raw_filters, dict) else {}
    reason = str(value.get("reason") or "").strip()[:240]

    if intent_type == "search_draft":
        if set(filters) - {"role", "city", "min_salary_per_day"}:
            return None
        role = str(filters.get("role") or "").strip()
        city = str(filters.get("city") or "").strip()
        salary = filters.get("min_salary_per_day")
        if role and role not in ROLE_NAMES:
            return None
        if city and city not in CITY_NAMES:
            return None
        if salary in (None, ""):
            min_salary = None
        elif isinstance(salary, bool):
            return None
        else:
            try:
                min_salary = int(salary)
            except (TypeError, ValueError):
                return None
            if not 0 <= min_salary <= 10000:
                return None
        return {
            "type": intent_type,
            "filters": {"role": role, "city": city, "min_salary_per_day": min_salary},
            "reason": reason,
        }

    if intent_type in {"explain_job", "prepare_application", "prepare_communication", "ignore_broadcast"}:
        key = "capture_id" if intent_type == "ignore_broadcast" else "job_id"
        if set(filters) != {key}:
            return None
        raw_id = filters.get(key)
        if isinstance(raw_id, bool):
            return None
        try:
            item_id = int(raw_id)
        except (TypeError, ValueError):
            return None
        if item_id <= 0:
            return None
        return {"type": intent_type, "filters": {key: item_id}, "reason": reason}

    if filters:
        return None
    return {"type": intent_type, "filters": {}, "reason": reason}


def control_memory_contains_sensitive_text(text: str) -> bool:
    return bool(
        CONTROL_MEMORY_SECRET_PATTERN.search(text)
        or re.search(r"1[3-9]\d{9}", text)
        or re.search(r"[\w.+-]+@[\w.-]+", text)
    )


def parse_control_intent(text: str) -> dict[str, Any]:
    normalized = " ".join(text.strip().split())
    lower = normalized.lower()
    city = next((item for item in CITY_NAMES if item in normalized), "")
    role = next((item for item in ROLE_NAMES if item.lower() in lower), "")
    if not role:
        if "agent" in lower or "智能体" in normalized:
            role = "Agent 开发实习"
        elif "后端" in normalized:
            role = "AI 后端实习"
        elif "rag" in lower:
            role = "RAG 开发实习"
        elif "ai" in lower or "大模型" in normalized:
            role = "AI 应用开发实习"
    salary = re.search(r"(?:日薪|每天|薪资)\s*(?:至少|不低于|>=|＞=?)?\s*(\d{2,4})(?:\s*(?:元\s*/?\s*天|元/天|/天))?", normalized)
    min_salary = int(salary.group(1)) if salary else None

    if any(token in normalized for token in ("统计", "数据概览", "多少岗位", "投递情况")):
        return {"type": "stats", "filters": {}}
    selected_job = re.search(r"(?:选择|选中|设为当前|当前选择)\s*(?:岗位|职位)?\s*#?(\d+)", normalized)
    if selected_job:
        return {"type": "select_job", "filters": {"job_id": int(selected_job.group(1))}}
    remembered = re.match(r"^(?:请)?记住\s*[:：]?\s*(.+)$", normalized)
    if remembered and remembered.group(1).strip():
        return {"type": "remember_preference", "filters": {"content": remembered.group(1).strip()[:300]}}
    if any(token in normalized for token in ("查看记忆", "当前记忆", "我的偏好", "我的记忆")):
        return {"type": "show_memory", "filters": {}}
    if any(token in normalized for token in ("计划", "下一步", "怎么做")):
        return {"type": "show_plan", "filters": {}}
    if any(token in normalized for token in ("群发", "忽略", "无需回复")):
        capture_id = re.search(r"(?:记录|对话|采集)\s*#?(\d+)", normalized)
        return {"type": "ignore_broadcast", "filters": {"capture_id": int(capture_id.group(1)) if capture_id else None}}
    if any(token in normalized for token in ("准备沟通", "沟通准备", "准备打招呼", "开始沟通", "去沟通")):
        job_id = re.search(r"(?:岗位|职位)\s*#?(\d+)", normalized)
        if job_id:
            return {"type": "prepare_communication", "filters": {"job_id": int(job_id.group(1))}}
    if any(token in normalized for token in ("投递准备", "准备投递")):
        job_id = re.search(r"(?:岗位|职位)\s*#?(\d+)", normalized)
        if job_id:
            return {"type": "prepare_application", "filters": {"job_id": int(job_id.group(1))}}
    if any(token in normalized for token in ("解释", "看看岗位", "岗位详情", "岗位 #")):
        job_id = re.search(r"(?:岗位|职位)\s*#?(\d+)", normalized)
        return {"type": "explain_job", "filters": {"job_id": int(job_id.group(1)) if job_id else None}}
    if any(token in normalized for token in ("找", "搜索", "发现")) and (role or city or min_salary):
        return {"type": "search_draft", "filters": {"role": role, "city": city, "min_salary_per_day": min_salary}}
    return {"type": "help", "filters": {}}


def redact_control_text(text: str) -> str:
    text = re.sub(r"1[3-9]\d{9}", "[手机号已脱敏]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+", "[邮箱已脱敏]", text)
    return text[:1000]
