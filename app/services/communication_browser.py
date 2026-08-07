from __future__ import annotations

from urllib.parse import urlparse


PLATFORM_STRATEGIES: dict[str, dict[str, object]] = {
    "Boss 直聘": {
        "domains": ["zhipin.com"],
        "chat_url_tokens": ["chat", "geek/chat", "job_detail"],
        "chat_text_hints": ["请输入", "发送", "交换微信", "交换电话", "发简历"],
        "conversation_panel_selectors": [
            "[class*='chat']",
            "[class*='im']",
            "[class*='dialog']",
            "[class*='message']",
        ],
        "message_input_selectors": [
            "textarea",
            "[contenteditable='true']",
            "[placeholder*='请输入']",
            "[class*='input'] textarea",
            "[class*='editor']",
        ],
        "send_button_selectors": [
            "button:has-text('发送')",
            "[role='button']:has-text('发送')",
            ".btn-send",
            "[class*='send']",
        ],
    },
    "猎聘": {
        "domains": ["liepin.com"],
        "chat_url_tokens": ["im", "chat", "message", "communicate", "c.liepin.com"],
        "chat_text_hints": ["请输入文字", "发送", "发简历", "交换手机号", "交换微信号"],
        "conversation_panel_selectors": [
            "[class*='im']",
            "[class*='chat']",
            "[class*='message']",
            "[class*='dialog']",
        ],
        "message_input_selectors": [
            "textarea",
            "[contenteditable='true']",
            "[placeholder*='请输入文字']",
            "[class*='input'] textarea",
            "[class*='editor']",
        ],
        "send_button_selectors": [
            "button:has-text('发送')",
            "[role='button']:has-text('发送')",
            "[class*='send']",
        ],
    },
    "实习僧": {
        "domains": ["shixiseng.com"],
        "chat_url_tokens": ["message", "chat", "im"],
        "chat_text_hints": ["请输入", "发送", "投递", "沟通"],
        "conversation_panel_selectors": [
            "[class*='chat']",
            "[class*='message']",
            "[class*='dialog']",
        ],
        "message_input_selectors": [
            "textarea",
            "[contenteditable='true']",
            "[placeholder*='请输入']",
            "[class*='input'] textarea",
        ],
        "send_button_selectors": [
            "button:has-text('发送')",
            "[role='button']:has-text('发送')",
            "[class*='send']",
        ],
    },
    "智联招聘": {
        "domains": ["zhaopin.com"],
        "chat_url_tokens": ["chat", "message", "im"],
        "chat_text_hints": ["请输入", "发送", "沟通", "消息"],
        "conversation_panel_selectors": [
            "[class*='chat']",
            "[class*='message']",
            "[class*='im']",
        ],
        "message_input_selectors": [
            "textarea",
            "[contenteditable='true']",
            "[placeholder*='请输入']",
            "[class*='input'] textarea",
        ],
        "send_button_selectors": [
            "button:has-text('发送')",
            "[role='button']:has-text('发送')",
            "[class*='send']",
        ],
    },
    "前程无忧": {
        "domains": ["51job.com"],
        "chat_url_tokens": ["chat", "message", "im"],
        "chat_text_hints": ["请输入", "发送", "沟通", "消息"],
        "conversation_panel_selectors": [
            "[class*='chat']",
            "[class*='message']",
            "[class*='im']",
        ],
        "message_input_selectors": [
            "textarea",
            "[contenteditable='true']",
            "[placeholder*='请输入']",
            "[class*='input'] textarea",
        ],
        "send_button_selectors": [
            "button:has-text('发送')",
            "[role='button']:has-text('发送')",
            "[class*='send']",
        ],
    },
}


def build_browser_send_adapter_plan(execution_plan: dict[str, object]) -> dict[str, object]:
    plans = list(execution_plan.get("plans") or [])
    browser_plans = [browser_plan_for_item(item) for item in plans if isinstance(item, dict)]
    ready_count = sum(1 for item in browser_plans if item.get("browser_action") == "dry_run_ready")
    manual_count = sum(1 for item in browser_plans if item.get("browser_action") == "manual_locate")
    skipped_count = sum(1 for item in browser_plans if item.get("browser_action") == "skip")
    if execution_plan.get("status") == "已关闭":
        status = "已关闭"
        note = "沟通模式为关闭，浏览器发送适配层未生成定位策略。"
    elif not browser_plans:
        status = "无候选"
        note = "没有可映射到浏览器定位策略的候选草稿。"
    else:
        status = "映射完成"
        note = f"映射 {len(browser_plans)} 条候选：可浏览器定位 {ready_count} 条，需人工定位 {manual_count} 条，跳过 {skipped_count} 条。"
    return {
        **execution_plan,
        "status": status,
        "note": note,
        "browser_dry_run": True,
        "browser_ready_count": ready_count,
        "browser_manual_count": manual_count,
        "browser_skipped_count": skipped_count,
        "browser_plans": browser_plans,
        "message_text_saved": False,
    }


def browser_plan_for_item(item: dict[str, object]) -> dict[str, object]:
    base = {
        "draft_id": item.get("draft_id"),
        "job_id": item.get("job_id"),
        "platform": item.get("platform") or "",
        "company": item.get("company") or "",
        "job_title": item.get("job_title") or "",
        "source_url": item.get("source_url") or "",
        "message_length": item.get("message_length") or 0,
        "gate_allowed": bool(item.get("gate_allowed")),
        "message_text_included": False,
    }
    if not item.get("gate_allowed"):
        return {
            **base,
            "browser_action": "skip",
            "supported": False,
            "reason": "发送闸门未通过，不进入浏览器定位。",
            "gate_reasons": item.get("gate_reasons") or [],
        }

    platform = str(item.get("platform") or "")
    strategy = PLATFORM_STRATEGIES.get(platform)
    if not strategy:
        return {
            **base,
            "browser_action": "manual_locate",
            "supported": False,
            "reason": "平台暂未配置浏览器定位策略，需要人工定位聊天页。",
            "gate_reasons": [],
        }

    source_url = str(item.get("source_url") or "")
    return {
        **base,
        "browser_action": "dry_run_ready",
        "supported": True,
        "reason": "已生成浏览器 dry-run 定位策略；当前版本不会点击发送。",
        "page_match": {
            "domains": strategy["domains"],
            "chat_url_tokens": strategy["chat_url_tokens"],
            "chat_text_hints": strategy["chat_text_hints"],
            "source_url_host": url_host(source_url),
        },
        "selector_candidates": {
            "conversation_panel": strategy["conversation_panel_selectors"],
            "message_input": strategy["message_input_selectors"],
            "send_button": strategy["send_button_selectors"],
        },
        "dry_run_steps": [
            "确认 Edge 调试窗口已打开并停留在对应 HR 聊天页。",
            "按平台域名、岗位公司、岗位名称和聊天页文本提示匹配当前页面。",
            "定位聊天面板和输入框候选元素。",
            "仅输出将要填充的草稿长度和候选发送按钮，不填充、不点击。",
        ],
        "safety_checks": [
            "发送前重新执行发送闸门。",
            "页面岗位或公司不匹配时停止。",
            "页面出现面试、联系方式、简历附件、押金、培训费等敏感内容时停止。",
            "真实发送必须由用户显式开启，并保留二次确认。",
        ],
    }


def url_host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()
