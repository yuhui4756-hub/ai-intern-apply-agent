from __future__ import annotations

import asyncio
from typing import Any

from ..db import connect, dumps, loads, set_setting, utc_now
from .llm import OpenAICompatibleClient, route_profile


MAX_TOOL_CALLS = 8
MAX_PLANNING_ROUNDS = 3
MAX_HISTORY_MESSAGES = 12
ROLE_OPTIONS = {"AI 应用开发实习", "Agent 开发实习", "AI 后端实习", "RAG 开发实习"}
CITY_OPTIONS = {"北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京"}
TOOL_PERMISSIONS = {
    "list_jobs": "本地只读",
    "select_job": "本地写入",
    "inspect_job": "本地只读",
    "search_jobs": "受控读取",
    "inspect_current_page": "受控读取",
    "prepare_application": "本地写入",
    "prepare_communication": "本地写入",
    "prepare_interview": "本地写入",
    "check_resume": "本地只读",
    "check_unread": "受控读取",
}


def row_dict(row: Any) -> dict[str, Any] | None:
    return {key: row[key] for key in row.keys()} if row else None


def compact(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def current_main() -> Any:
    # Import lazily to keep the existing FastAPI application free of an import cycle.
    from .. import main

    return main


def configured_model_profiles() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM model_profiles ORDER BY is_default DESC, name COLLATE NOCASE, id"
        ).fetchall()
    profiles = []
    for row in rows:
        profile = row_dict(row) or {}
        client = OpenAICompatibleClient(profile, "agent_chat")
        profiles.append(
            {
                "id": int(profile["id"]),
                "name": str(profile.get("name") or "未命名模型"),
                "model": str(profile.get("model") or ""),
                "configured": client.configured,
                "is_default": bool(profile.get("is_default")),
            }
        )
    return profiles


def default_agent_profile_id() -> int | None:
    profile = route_profile("agent_chat")
    return int(profile["id"]) if profile and profile.get("id") else None


def create_session(title: str = "", model_profile_id: int | None = None) -> dict[str, Any]:
    now = utc_now()
    profile_id = model_profile_id or default_agent_profile_id()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO agent_sessions (title, model_profile_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (compact(title, 120) or "新任务", profile_id, now, now),
        )
        session_id = int(cursor.lastrowid)
    return get_session(session_id) or {"id": session_id, "title": "新任务"}


def list_sessions(limit: int = 30) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*, p.name AS model_profile_name, p.model AS model_name,
                   (SELECT COUNT(*) FROM agent_messages m WHERE m.session_id = s.id) AS message_count
            FROM agent_sessions s
            LEFT JOIN model_profiles p ON p.id = s.model_profile_id
            ORDER BY s.updated_at DESC, s.id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 100)),),
        ).fetchall()
    return [serialize_session(row_dict(row) or {}) for row in rows]


def serialize_session(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(session["id"]),
        "title": str(session.get("title") or "新任务"),
        "model_profile_id": int(session["model_profile_id"]) if session.get("model_profile_id") else None,
        "model_profile_name": str(session.get("model_profile_name") or ""),
        "model_name": str(session.get("model_name") or ""),
        "auto_communication": bool(session.get("auto_communication")),
        "active_job_id": int(session["active_job_id"]) if session.get("active_job_id") else None,
        "summary": str(session.get("summary") or ""),
        "message_count": int(session.get("message_count") or 0),
        "created_at": str(session.get("created_at") or ""),
        "updated_at": str(session.get("updated_at") or ""),
    }


def get_session(session_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT s.*, p.name AS model_profile_name, p.model AS model_name,
                   (SELECT COUNT(*) FROM agent_messages m WHERE m.session_id = s.id) AS message_count
            FROM agent_sessions s
            LEFT JOIN model_profiles p ON p.id = s.model_profile_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    return serialize_session(row_dict(row) or {}) if row else None


def session_messages(session_id: int, limit: int = 80) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT m.*, p.name AS model_profile_name, p.model AS model_name
            FROM agent_messages m
            LEFT JOIN model_profiles p ON p.id = m.model_profile_id
            WHERE m.session_id = ?
            ORDER BY m.id DESC
            LIMIT ?
            """,
            (session_id, max(1, min(limit, 200))),
        ).fetchall()
    messages = []
    for row in reversed(rows):
        item = row_dict(row) or {}
        events = loads(item.get("events_json"), [])
        messages.append(
            {
                "id": int(item["id"]),
                "role": str(item.get("role") or "assistant"),
                "content": str(item.get("content") or ""),
                "events": events if isinstance(events, list) else [],
                "model_profile_id": int(item["model_profile_id"]) if item.get("model_profile_id") else None,
                "model_profile_name": str(item.get("model_profile_name") or ""),
                "model_name": str(item.get("model_name") or ""),
                "created_at": str(item.get("created_at") or ""),
            }
        )
    return messages


def update_session_summary(session_id: int, summary: str) -> dict[str, Any] | None:
    with connect() as conn:
        updated = conn.execute(
            "UPDATE agent_sessions SET summary = ?, updated_at = ? WHERE id = ?",
            (compact(summary, 600), utc_now(), session_id),
        )
    return get_session(session_id) if updated.rowcount else None


def active_job_for_session(session_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT j.* FROM agent_sessions s
            LEFT JOIN job_postings j ON j.id = s.active_job_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
    if not row or row["id"] is None:
        return None
    job = row_dict(row) or {}
    extracted_json = loads(job.get("extracted_json"), {})
    extracted = extracted_json.get("extracted") if isinstance(extracted_json, dict) else {}
    scoring = extracted_json.get("scoring") if isinstance(extracted_json, dict) else {}
    extracted = extracted if isinstance(extracted, dict) else {}
    scoring = scoring if isinstance(scoring, dict) else {}
    return {
        **job_card(job),
        "required_skills": [compact(item, 80) for item in list(extracted.get("required_skills") or [])[:10] if compact(item, 80)],
        "matched_skills": [compact(item, 80) for item in list(scoring.get("matched_skills") or [])[:8] if compact(item, 80)],
        "missing_skills": [compact(item, 80) for item in list(scoring.get("missing_skills") or [])[:8] if compact(item, 80)],
        "skip_reason": compact(job.get("skip_reason"), 300),
    }


def job_card(job: dict[str, Any]) -> dict[str, Any]:
    extracted = loads(job.get("extracted_json"), {})
    scoring = extracted.get("scoring") if isinstance(extracted, dict) else {}
    scoring = scoring if isinstance(scoring, dict) else {}
    return {
        "id": int(job["id"]),
        "title": str(job.get("title") or "岗位待补充"),
        "company": str(job.get("company") or "公司待补充"),
        "platform": str(job.get("platform") or ""),
        "city": str(job.get("city") or ""),
        "salary_text": str(job.get("salary_text") or "薪资待确认"),
        "match_score": int(job.get("match_score") or 0),
        "match_level": str(job.get("match_level") or ""),
        "recommendation": str(job.get("recommendation") or "待确认"),
        "risk_level": str(job.get("risk_level") or "待确认"),
        "status": str(job.get("status") or "待分析"),
        "summary": compact("；".join(str(item) for item in scoring.get("fit_notes") or []), 220),
        "updated_at": str(job.get("updated_at") or ""),
    }


def canvas_items(limit: int = 120) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM job_postings
            ORDER BY CASE status WHEN '待确认' THEN 0 WHEN '待投递' THEN 1 WHEN '已沟通' THEN 2
                                 WHEN '面试邀请' THEN 3 WHEN '待面试' THEN 4 ELSE 5 END,
                     match_score DESC, updated_at DESC, id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 300)),),
        ).fetchall()
    return [job_card(row_dict(row) or {}) for row in rows]


def load_client(profile_id: int | None) -> tuple[OpenAICompatibleClient | None, dict[str, Any] | None]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM model_profiles WHERE id = ?", (profile_id,)).fetchone() if profile_id else None
    profile = row_dict(row) if row else route_profile("agent_chat")
    if not profile:
        return None, None
    client = OpenAICompatibleClient(profile, "agent_chat")
    return (client if client.configured else None), profile


def session_context(session_id: int) -> dict[str, Any]:
    session = get_session(session_id)
    if not session:
        raise ValueError("没有找到 Agent 任务会话。")
    app_main = current_main()
    return {
        "session": session,
        "active_job": active_job_for_session(session_id),
        "recent_jobs": canvas_items(8),
        "edge": app_main.controlled_edge_status(),
        "communication": app_main.communication_policy(),
        "automation": app_main.automation_control(),
        "tools": [{"name": name, "permission": permission} for name, permission in TOOL_PERMISSIONS.items()],
    }


def tool_schema_prompt() -> str:
    return "\n".join(
        [
            "可调用工具及参数：",
            "- list_jobs({limit?:1..20,status?:string,recommendation?:string,risk_level?:string})：读取本地岗位卡。",
            "- select_job({job_id:number})：将本会话当前岗位设为指定本地岗位。",
            "- inspect_job({job_id?:number})：读取指定或当前岗位的本地详情。",
            "- search_jobs({role?:AI 应用开发实习|Agent 开发实习|AI 后端实习|RAG 开发实习,city?:北京|上海|广州|深圳|杭州|重庆|成都|南京,min_salary_per_day?:number})：启动受控 Edge 的只读搜索、JD 获取和本地评分任务。",
            "- inspect_current_page({full_page?:boolean})：仅截图分析当前唯一受控招聘页面，不保存截图。",
            "- prepare_application({job_id?:number})：仅创建本地投递准备，绝不上传或提交。",
            "- prepare_communication({job_id?:number})：仅生成本地岗位沟通草稿；自动沟通关闭时不会发送。",
            "- prepare_interview({job_id?:number})：只在岗位已经处于待面试/面试准备中时生成本地准备。",
            "- check_resume({job_id?:number})：检查画像、简历和当前岗位绑定关系，不返回联系方式或文件路径。",
            "- check_unread({})：只读扫描已打开 Boss/猎聘消息列表的未读结构，不读会话正文、不打开会话。",
        ]
    )


def planner_messages(user_text: str, context: dict[str, Any], history: list[dict[str, Any]], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history_text = "\n".join(
        f"{('用户' if item['role'] == 'user' else 'Agent')}：{compact(item['content'], 700)}" for item in history[-MAX_HISTORY_MESSAGES:]
    ) or "（无）"
    observations_text = dumps(observations[-MAX_TOOL_CALLS:]) if observations else "[]"
    system = (
        "你是‘求职agent’的主任务规划模型。你可以自然理解用户目标，并在受限工具范围内主动拆解、观察和推进。"
        "禁止编造用户经历、岗位事实、公司信息、工具结果或平台状态。禁止输出思维链；plan 只写一两句可审计的执行摘要。"
        "不能调用未列出的工具，不能把 URL、CSS 选择器、命令、SQL、文件路径或投递/上传/提交动作放进参数。"
        "自动沟通开关只代表已授权的受限发送工作流；本轮仍不能选择简历、上传或点击投递/提交。"
        "工具结果已经足够时必须停止调用工具并给出清晰回答。"
        + tool_schema_prompt()
        + "\n只输出 JSON 对象：plan(string,<=220)、tool_calls(array,最多3项，每项 name 和 arguments)、response(string,<=1600)、task_summary(string,<=300)。"
    )
    user = (
        f"会话上下文：\n{dumps(context)}\n\n最近对话：\n{history_text}\n\n"
        f"已完成工具观察：\n{observations_text}\n\n当前用户请求：\n{user_text}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def normalize_plan(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("模型没有返回 JSON 对象。")
    raw_calls = value.get("tool_calls") if isinstance(value.get("tool_calls"), list) else []
    tool_calls = []
    for raw in raw_calls[:3]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        arguments = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
        if name not in TOOL_PERMISSIONS:
            continue
        tool_calls.append({"name": name, "arguments": arguments})
    return {
        "plan": compact(value.get("plan"), 220),
        "tool_calls": tool_calls,
        "response": compact(value.get("response"), 1600),
        "task_summary": compact(value.get("task_summary"), 300),
    }


def insert_message(session_id: int, role: str, content: str, events: list[dict[str, Any]], model_profile_id: int | None = None) -> int:
    now = utc_now()
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO agent_messages (session_id, role, content, events_json, model_profile_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, role, content, dumps(events), model_profile_id, now),
        )
        conn.execute("UPDATE agent_sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    return int(cursor.lastrowid)


def insert_tool_run(
    session_id: int,
    message_id: int,
    tool_name: str,
    permission_level: str,
    status: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
    error_message: str = "",
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO agent_tool_runs (
                session_id, message_id, tool_name, permission_level, status,
                arguments_json, result_json, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, message_id, tool_name, permission_level, status, dumps(arguments), dumps(result), compact(error_message, 500), utc_now()),
        )


def tool_job_id(arguments: dict[str, Any], session: dict[str, Any]) -> int | None:
    value = arguments.get("job_id") or session.get("active_job_id")
    if isinstance(value, bool):
        return None
    try:
        job_id = int(value)
    except (TypeError, ValueError):
        return None
    return job_id if job_id > 0 else None


def local_job_detail(job_id: int) -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM job_postings WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return {"status": "未找到", "job_id": job_id}
    item = row_dict(row) or {}
    extracted = loads(item.get("extracted_json"), {})
    scoring = extracted.get("scoring") if isinstance(extracted, dict) else {}
    return {
        "status": "已完成",
        "job": job_card(item),
        "required_skills": list((extracted.get("extracted") or {}).get("required_skills") or [])[:12] if isinstance(extracted, dict) else [],
        "matched_skills": list((scoring or {}).get("matched_skills") or [])[:8],
        "missing_skills": list((scoring or {}).get("missing_skills") or [])[:8],
        "skip_reason": compact(item.get("skip_reason"), 300),
    }


async def execute_tool(
    session: dict[str, Any],
    user_message_id: int,
    name: str,
    arguments: dict[str, Any],
    selected_client: OpenAICompatibleClient,
    user_request: str,
) -> dict[str, Any]:
    app_main = current_main()
    permission = TOOL_PERMISSIONS[name]
    try:
        if name == "list_jobs":
            limit = max(1, min(int(arguments.get("limit") or 8), 20))
            cards = canvas_items(120)
            for key in ("status", "recommendation", "risk_level"):
                wanted = compact(arguments.get(key), 30)
                if wanted:
                    cards = [card for card in cards if card.get(key) == wanted]
            result = {"status": "已完成", "jobs": cards[:limit]}
        elif name == "select_job":
            job_id = tool_job_id(arguments, session)
            if not job_id or local_job_detail(job_id).get("status") != "已完成":
                result = {"status": "未找到", "note": "没有找到可选择的本地岗位。"}
            else:
                with connect() as conn:
                    conn.execute("UPDATE agent_sessions SET active_job_id = ?, updated_at = ? WHERE id = ?", (job_id, utc_now(), session["id"]))
                session["active_job_id"] = job_id
                result = {"status": "已完成", "active_job": local_job_detail(job_id)["job"]}
        elif name == "inspect_job":
            job_id = tool_job_id(arguments, session)
            result = local_job_detail(job_id) if job_id else {"status": "需要选择岗位", "note": "请先从岗位画布选择岗位，或在请求中指定岗位编号。"}
        elif name == "search_jobs":
            role = compact(arguments.get("role"), 60)
            city = compact(arguments.get("city"), 40)
            salary = arguments.get("min_salary_per_day")
            if role and role not in ROLE_OPTIONS:
                raise ValueError("岗位方向不在允许范围内。")
            if city and city not in CITY_OPTIONS:
                raise ValueError("城市不在允许范围内。")
            minimum = int(salary) if salary not in (None, "") and not isinstance(salary, bool) else None
            if minimum is not None and not 0 <= minimum <= 10000:
                raise ValueError("最低日薪范围无效。")
            filters = {"role": role, "city": city, "min_salary_per_day": minimum}
            task_id = await asyncio.to_thread(app_main.create_controlled_job_discovery_task, filters)
            app_main.schedule_discovery_task(task_id)
            result = {"status": "已启动", "task_id": task_id, "task_url": f"/job-discovery/tasks/{task_id}", "filters": filters}
        elif name == "inspect_current_page":
            full_page = bool(arguments.get("full_page"))
            visual = await asyncio.to_thread(
                app_main.run_visual_page_review,
                "full_page" if full_page else "viewport",
                user_request,
                client_override=selected_client,
            )
            result = {"status": str(visual.get("status") or "未完成"), "note": compact(visual.get("note"), 400), "review": visual.get("review") or {}}
        elif name == "prepare_application":
            job_id = tool_job_id(arguments, session)
            if not job_id:
                result = {"status": "需要选择岗位", "note": "投递准备需要当前岗位。"}
            else:
                with connect() as conn:
                    prepared = app_main.ensure_application_preparation_for_job(conn, job_id, trigger_type="desktop_agent")
                result = {"status": "已完成" if prepared.get("preparation_id") else "已阻止", **prepared}
        elif name == "prepare_communication":
            job_id = tool_job_id(arguments, session)
            if not job_id:
                result = {"status": "需要选择岗位", "note": "沟通准备需要当前岗位。"}
            elif bool(session.get("auto_communication")):
                started = await asyncio.to_thread(
                    app_main.run_desktop_autonomous_first_contact,
                    job_id,
                    session_id=int(session["id"]),
                )
                result = {**started, "auto_communication": True}
            else:
                with connect() as conn:
                    prepared = app_main.ensure_communication_preparation_for_job(conn, job_id, trigger_type="desktop_agent")
                result = {"status": "已完成" if prepared.get("draft_id") else "已阻止", **prepared, "auto_communication": bool(session.get("auto_communication"))}
        elif name == "prepare_interview":
            job_id = tool_job_id(arguments, session)
            if not job_id:
                result = {"status": "需要选择岗位", "note": "面试准备需要当前岗位。"}
            else:
                with connect() as conn:
                    prepared = app_main.ensure_interview_preparation_for_job(conn, job_id, trigger_type="desktop_agent")
                result = {"status": "已完成" if prepared.get("interview_id") else "等待人工确认", **prepared}
        elif name == "check_resume":
            job_id = tool_job_id(arguments, session)
            with connect() as conn:
                result = app_main.local_resume_readiness(conn, job_id)
        elif name == "check_unread":
            request_words = ("未读", "消息", "回复", "沟通", "hr", "HR")
            if not any(word in user_request for word in request_words):
                result = {"status": "已阻止", "note": "未读会话扫描需要用户在本轮明确要求检查消息。"}
            else:
                result = await asyncio.to_thread(app_main.run_unread_conversation_scan, "desktop_agent")
        else:
            result = {"status": "已阻止", "note": "工具不在受限清单内。"}
        status = str(result.get("status") or "已完成")
        insert_tool_run(session["id"], user_message_id, name, permission, status, arguments, result)
        return {"tool": name, "permission": permission, "status": status, "result": result}
    except Exception as exc:
        error = str(exc)[:500]
        result = {"status": "失败", "note": error}
        insert_tool_run(session["id"], user_message_id, name, permission, "失败", arguments, result, error)
        return {"tool": name, "permission": permission, "status": "失败", "result": result}


def event(kind: str, status: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "status": status, "summary": compact(summary, 500), **extra}


async def process_turn(
    session_id: int,
    message: str,
    *,
    model_profile_id: int | None = None,
    auto_communication: bool | None = None,
) -> dict[str, Any]:
    text = compact(message, 6000)
    if not text:
        raise ValueError("请输入任务内容。")
    session = get_session(session_id)
    if not session:
        raise ValueError("没有找到 Agent 任务会话。")
    if model_profile_id:
        with connect() as conn:
            exists = conn.execute("SELECT id FROM model_profiles WHERE id = ?", (model_profile_id,)).fetchone()
            if not exists:
                raise ValueError("选择的模型档案不存在。")
            conn.execute("UPDATE agent_sessions SET model_profile_id = ?, updated_at = ? WHERE id = ?", (model_profile_id, utc_now(), session_id))
        session = get_session(session_id) or session
    if auto_communication is not None:
        with connect() as conn:
            conn.execute("UPDATE agent_sessions SET auto_communication = ?, updated_at = ? WHERE id = ?", (int(auto_communication), utc_now(), session_id))
        session["auto_communication"] = bool(auto_communication)
        if auto_communication:
            app_main = current_main()
            control = app_main.automation_control()
            if bool(control.get("paused")):
                set_setting("automation_control", {"paused": False, "pause_reason": "", "updated_at": utc_now()})
                with connect() as conn:
                    app_main.log_agent_action(
                        conn,
                        action_type="desktop_agent_auto_communication",
                        status="已授权",
                        summary="用户在桌面 Agent 会话开启自动沟通；未启用旧定时巡检或历史草稿执行器。",
                        decision={
                            "session_id": session_id,
                            "session_auto_communication": True,
                            "old_global_paused": True,
                            "new_global_paused": False,
                            "legacy_patrol_enabled": bool(app_main.message_patrol_policy()["enabled"]),
                            "legacy_policy_mode": app_main.communication_policy()["mode"],
                            "application_submitted": False,
                        },
                    )

    client, profile = load_client(session.get("model_profile_id"))
    user_events: list[dict[str, Any]] = []
    user_message_id = insert_message(session_id, "user", text, user_events, session.get("model_profile_id"))
    if not client or not profile:
        response = "当前选择的模型尚未配置完成。请在设置中补充服务地址、模型和 API Key 后再使用主 Agent。"
        assistant_events = [event("模型", "未配置", response)]
        insert_message(session_id, "assistant", response, assistant_events, session.get("model_profile_id"))
        return conversation_payload(session_id)

    history = session_messages(session_id, MAX_HISTORY_MESSAGES + 1)[:-1]
    observations: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    response = ""
    task_summary = ""
    tool_call_count = 0
    for _round in range(MAX_PLANNING_ROUNDS):
        context = session_context(session_id)
        raw = await asyncio.to_thread(client.complete_json, planner_messages(text, context, history, observations))
        plan = normalize_plan(raw)
        if plan["plan"]:
            events.append(event("计划", "已生成", plan["plan"], model_profile=str(profile.get("name") or "")))
        task_summary = plan["task_summary"] or task_summary
        response = plan["response"] or response
        calls = plan["tool_calls"][: max(0, MAX_TOOL_CALLS - tool_call_count)]
        if not calls:
            break
        for call in calls:
            tool_call_count += 1
            events.append(event("工具调用", "执行中", f"{call['name']}（{TOOL_PERMISSIONS[call['name']]}）", tool=call["name"]))
            tool_result = await execute_tool(session, user_message_id, call["name"], call["arguments"], client, text)
            observations.append(tool_result)
            result_note = compact((tool_result.get("result") or {}).get("note"), 260)
            events.append(
                event(
                    "工具结果",
                    tool_result["status"],
                    result_note or f"{tool_result['tool']}：{tool_result['status']}",
                    tool=tool_result["tool"],
                    permission=tool_result["permission"],
                )
            )
        if tool_call_count >= MAX_TOOL_CALLS:
            events.append(event("执行边界", "已停止", f"本轮已达到 {MAX_TOOL_CALLS} 次工具调用上限，进度已保存。"))
            break

    if not response:
        if observations:
            response = "已完成本轮受限工具执行，结果已记录在下方。请根据工具结果决定下一步。"
        else:
            response = "我已经理解了这项任务，但当前不需要调用外部工具。请告诉我希望优先查看岗位、搜索机会还是准备沟通。"
    events.append(event("安全结论", "已检查", "本轮未选择简历、未上传文件、未点击投递或提交。"))
    if task_summary:
        update_session_summary(session_id, task_summary)
    insert_message(session_id, "assistant", response, events, int(profile["id"]))
    return conversation_payload(session_id)


def conversation_payload(session_id: int) -> dict[str, Any]:
    session = get_session(session_id)
    if not session:
        raise ValueError("没有找到 Agent 任务会话。")
    return {
        "session": session,
        "messages": session_messages(session_id),
        "state": session_context(session_id),
    }
