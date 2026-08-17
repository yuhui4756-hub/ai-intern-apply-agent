from __future__ import annotations

import re
from typing import Any


CITY_NAMES = ("北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京")
ROLE_NAMES = ("AI 应用开发实习", "Agent 开发实习", "AI 后端实习", "RAG 开发实习")
CONTROL_STATUS_UPDATE_TARGETS = (
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
)
ALLOWED_CONTROL_INTENTS = (
    "search_draft",
    "stats",
    "list_jobs",
    "explain_job",
    "compare_jobs",
    "job_match_review",
    "prepare_interview",
    "update_job_status",
    "company_research",
    "prepare_application",
    "prepare_communication",
    "ignore_broadcast",
    "show_plan",
    "help",
)
MODEL_ROUTABLE_CONTROL_INTENTS = (
    "search_draft",
    "stats",
    "explain_job",
    "ignore_broadcast",
    "show_plan",
    "help",
)
CONTROL_MEMORY_SECRET_PATTERN = re.compile(r"(?i)(?:sk-[a-z0-9_-]{12,}|(?:api[_\s-]*key|password|密码|token)\s*[:=]|bearer\s+[a-z0-9._-]{12,})")


def explicit_control_job_ids(text: str) -> list[int]:
    job_ids = []
    for raw_id in re.findall(r"(?:岗位|职位)?\s*#\s*(\d+)", text):
        job_id = int(raw_id)
        if job_id not in job_ids:
            job_ids.append(job_id)
    return job_ids


def parse_control_job_list_filters(text: str) -> dict[str, Any]:
    match_level = next((item for item in ("高匹配", "中匹配", "低匹配") if item in text), "")
    recommendation = next((item for item in ("必投", "可冲", "跳过") if item in text), "")
    risk_level = "低" if "低风险" in text else "中" if "中风险" in text else "高" if "高风险" in text else ""
    status = next(
        (
            item for item in ("待确认", "待投递", "已沟通", "面试邀请", "待面试", "面试准备中", "已归档")
            if item in text
        ),
        "",
    )
    limit_match = re.search(r"(?:前|最多|列出)\s*(\d{1,2})\s*(?:个|条)?", text)
    limit = int(limit_match.group(1)) if limit_match else 6
    return {
        "match_level": match_level,
        "recommendation": recommendation,
        "risk_level": risk_level,
        "status": status,
        "sort": "recent" if any(token in text for token in ("最近", "最新")) else "priority",
        "limit": max(1, min(limit, 10)),
    }


def normalize_model_control_intent(value: Any) -> dict[str, Any] | None:
    """Accept only the narrow intent schema that the policy layer understands."""
    if not isinstance(value, dict):
        return None
    intent_type = str(value.get("type") or "").strip()
    if intent_type not in MODEL_ROUTABLE_CONTROL_INTENTS:
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

    if intent_type in {"explain_job", "ignore_broadcast"}:
        key = "job_id" if intent_type == "explain_job" else "capture_id"
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
    job_list_requested = any(token in normalized for token in ("列出", "清单", "列表", "有哪些", "推荐", "看看", "查看", "最近", "最新"))
    if job_list_requested and any(token in normalized for token in ("岗位", "职位", "机会")) and not explicit_control_job_ids(normalized):
        return {"type": "list_jobs", "filters": parse_control_job_list_filters(normalized)}
    if any(token in normalized for token in ("比较", "对比", "哪个更适合", "哪个值得优先", "优先沟通哪个")):
        job_ids = explicit_control_job_ids(normalized)
        return {"type": "compare_jobs", "filters": {"job_ids": job_ids[:2]}}
    status_target = next((item for item in CONTROL_STATUS_UPDATE_TARGETS if item in normalized), "")
    status_action = any(token in normalized for token in ("标记", "改为", "改成", "设为", "更新状态", "变更状态"))
    if status_target and status_action:
        job_id = re.search(r"(?:岗位|职位)\s*#?(\d+)", normalized)
        return {
            "type": "update_job_status",
            "filters": {"job_id": int(job_id.group(1)) if job_id else None, "status": status_target},
        }
    if any(token in normalized for token in ("准备面试", "面试准备", "开始备面", "开始准备面试")):
        job_id = re.search(r"(?:岗位|职位)\s*#?(\d+)", normalized)
        return {"type": "prepare_interview", "filters": {"job_id": int(job_id.group(1)) if job_id else None}}
    company_research = any(token in normalized for token in ("公司风险", "查公司", "公司尽调", "公司背调", "公司怎么样"))
    if company_research:
        job_id = re.search(r"(?:岗位|职位)\s*#?(\d+)", normalized)
        if job_id:
            search_depth = "deep" if any(token in normalized for token in ("深度", "详细")) else "quick" if "快速" in normalized else "standard" if "标准" in normalized else "auto"
            return {"type": "company_research", "filters": {"job_id": int(job_id.group(1)), "search_depth": search_depth}}
    if any(token in normalized for token in ("深度匹配复核", "深度复核", "匹配复核", "匹配解释")):
        job_id = re.search(r"(?:岗位|职位)\s*#?(\d+)", normalized)
        if job_id:
            return {"type": "job_match_review", "filters": {"job_id": int(job_id.group(1))}}
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
