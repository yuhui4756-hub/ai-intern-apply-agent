from __future__ import annotations

import hashlib
import json
from urllib.parse import urlparse

from .job_fetcher import normalize_browser_channel
from .job_searcher import (
    controlled_edge_dom_snapshot_expression,
    send_cdp_command,
    evaluate_cdp_expression,
    read_controlled_edge_targets,
    target_url,
    wait_for_debug_endpoint,
)


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
            "button[aria-label*='发送']",
            "[role='button'][aria-label*='发送']",
            "button[title*='发送']",
            "[role='button'][title*='发送']",
            "button[type='submit']",
            ".btn-send",
            "[class*='send']",
            "[class*='submit']",
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
            "button[aria-label*='发送']",
            "[role='button'][aria-label*='发送']",
            "button[title*='发送']",
            "[role='button'][title*='发送']",
            "button[type='submit']",
            "[class*='send']",
            "[class*='submit']",
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
            "button[aria-label*='发送']",
            "[role='button'][aria-label*='发送']",
            "button[title*='发送']",
            "[role='button'][title*='发送']",
            "button[type='submit']",
            "[class*='send']",
            "[class*='submit']",
        ],
    },
}

RECRUITMENT_PLATFORM_DOMAINS = {
    "Boss 直聘": ("zhipin.com",),
    "猎聘": ("liepin.com",),
    "实习僧": ("shixiseng.com",),
    "智联招聘": ("zhaopin.com",),
    "前程无忧": ("51job.com",),
}

# 实习僧的 PC 端消息窗目前只能查看，不能可靠地输入或发送。保留它的
# 页面识别与岗位能力，但不让任何 PC 沟通自动化路径进入该平台。
PC_MESSAGE_AUTOMATION_PLATFORMS = frozenset({"Boss 直聘", "猎聘"})

# Static resume controls are common in legitimate chat UIs. Conversation content
# asking for a resume is still routed to a human by the separate send gate.
HARD_PAGE_BLOCKING_TEXT = (
    "身份证",
    "银行卡",
    "培训费",
    "押金",
    "贷款",
    "付费",
    "扫码",
    "请先上传简历",
)


def calibrate_controlled_edge_chat_pages(
    *,
    browser_channel: str = "msedge",
) -> dict[str, object]:
    """Read structural chat-page signals without returning page text, title, or URL."""
    channel = normalize_browser_channel(browser_channel)
    if channel != "msedge":
        raise ValueError("当前聊天页结构校准先支持 Microsoft Edge。")
    if not wait_for_debug_endpoint(timeout_seconds=3):
        raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先点击“打开受控 Edge 巡检窗口”。")

    results: list[dict[str, object]] = []
    for target in read_controlled_edge_targets():
        url = target_url(target)
        if not url.startswith(("http://", "https://")):
            continue
        platform = recruitment_platform_for_url(url)
        if not platform:
            continue
        if not is_pc_message_automation_platform(platform):
            results.append(unsupported_platform_calibration_result(platform))
            continue
        strategy = PLATFORM_STRATEGIES.get(platform)
        if not strategy:
            results.append(unsupported_platform_calibration_result(platform))
            continue
        try:
            snapshot = evaluate_cdp_expression(target, chat_page_calibration_expression(strategy))
        except Exception:
            results.append({"platform": platform, "status": "读取失败"})
            continue
        results.append(chat_page_calibration_result(platform, snapshot))

    candidate_chat_count = sum(1 for item in results if item.get("chat_page_candidate"))
    structure_ready_count = sum(1 for item in results if item.get("status") == "结构可校准")
    review_count = sum(1 for item in results if item.get("status") == "需人工校准")
    sensitive_count = sum(1 for item in results if item.get("status") == "含敏感提示")
    unsupported_count = sum(1 for item in results if item.get("status") == "PC 消息自动化未启用")
    if not results:
        status = "未发现招聘平台页面"
        note = "已连接受控 Edge，但没有发现已打开的招聘平台页面。"
    else:
        status = "校准完成"
        note = (
            f"已只读校准 {len(results)} 个招聘平台页面：聊天页候选 {candidate_chat_count} 个，"
            f"结构可校准 {structure_ready_count} 个，需人工校准 {review_count} 个，"
            f"PC 消息自动化未启用 {unsupported_count} 个。"
        )
    return {
        "status": status,
        "note": note,
        "browser_connected": True,
        "checked_page_count": len(results),
        "candidate_chat_count": candidate_chat_count,
        "structure_ready_count": structure_ready_count,
        "review_count": review_count,
        "sensitive_count": sensitive_count,
        "unsupported_count": unsupported_count,
        "results": results,
        "page_text_saved": False,
        "page_url_saved": False,
        "page_title_saved": False,
        "browser_clicked": False,
        "message_filled": False,
    }


def platform_strategy_for_url(url: str) -> tuple[str, dict[str, object]]:
    platform = recruitment_platform_for_url(url)
    return platform, PLATFORM_STRATEGIES.get(platform, {})


def recruitment_platform_for_url(url: str) -> str:
    host = url_host(url)
    for platform, domains in RECRUITMENT_PLATFORM_DOMAINS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return ""


def is_pc_message_automation_platform(platform: str) -> bool:
    return str(platform or "") in PC_MESSAGE_AUTOMATION_PLATFORMS


def unsupported_platform_calibration_result(platform: str) -> dict[str, object]:
    return {
        "platform": platform,
        "status": "PC 消息自动化未启用",
        "chat_page_candidate": False,
        "chat_url_match": False,
        "chat_text_hint_match": False,
        "conversation_panel_count": 0,
        "message_input_count": 0,
        "send_button_count": 0,
        "generic_send_control_count": 0,
        "sensitive_signal_count": 0,
    }


def chat_page_calibration_expression(strategy: dict[str, object]) -> str:
    panel_selectors = json.dumps(list(strategy.get("conversation_panel_selectors") or []), ensure_ascii=False)
    input_selectors = json.dumps(list(strategy.get("message_input_selectors") or []), ensure_ascii=False)
    send_selectors = json.dumps(list(strategy.get("send_button_selectors") or []), ensure_ascii=False)
    chat_url_tokens = json.dumps([str(token).lower() for token in list(strategy.get("chat_url_tokens") or [])], ensure_ascii=False)
    chat_text_hints = json.dumps(
        [normalize_probe_text(str(hint)) for hint in list(strategy.get("chat_text_hints") or [])],
        ensure_ascii=False,
    )
    blocking_text = json.dumps([normalize_probe_text(signal) for signal in HARD_PAGE_BLOCKING_TEXT], ensure_ascii=False)
    return f"""(() => {{
        const panelSelectors = {panel_selectors};
        const inputSelectors = {input_selectors};
        const sendSelectors = {send_selectors};
        const chatUrlTokens = {chat_url_tokens};
        const chatTextHints = {chat_text_hints};
        const blockingText = {blocking_text};
        const textSelector = /^(.*):has-text\\((['\"])(.*?)\\2\\)$/;
        const visible = element => {{
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        }};
        const controlLabel = element => (element.getAttribute('aria-label') || element.getAttribute('title') || element.innerText || element.textContent || '').trim().replace(/\\s+/g, '');
        const isSendLabel = label => label === '发送' || label === '发送消息';
        const matches = (selectors, predicate = () => true) => {{
            const found = new Set();
            for (const selector of selectors) {{
                const match = selector.match(textSelector);
                const baseSelector = match ? match[1] : selector;
                const expectedText = match ? match[3] : '';
                try {{
                    for (const element of document.querySelectorAll(baseSelector)) {{
                        const label = controlLabel(element);
                        if (visible(element) && (!expectedText || label.includes(expectedText)) && predicate(element, label)) found.add(element);
                    }}
                }} catch (_error) {{}}
            }}
            return Math.min(found.size, 20);
        }};
        const editable = element => {{
            if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) return !element.disabled && !element.readOnly;
            return element.isContentEditable;
        }};
        const bodyText = document.body ? (document.body.innerText || '') : '';
        const normalizedText = bodyText.toLowerCase().replace(/\\s+/g, '');
        const href = location.href.toLowerCase();
        return {{
            chat_url_match: chatUrlTokens.some(token => href.includes(token)),
            chat_text_hint_match: chatTextHints.some(hint => normalizedText.includes(hint)),
            conversation_panel_count: matches(panelSelectors),
            message_input_count: matches(inputSelectors, editable),
            send_button_count: matches(sendSelectors, (_element, label) => isSendLabel(label)),
            generic_send_control_count: Array.from(document.querySelectorAll('button, [role="button"], [aria-label], [title]'))
                .filter(element => visible(element) && isSendLabel(controlLabel(element))).slice(0, 20).length,
            sensitive_signal_count: blockingText.filter(token => normalizedText.includes(token)).length
        }};
    }})()"""


def chat_page_calibration_result(platform: str, snapshot: object) -> dict[str, object]:
    values = snapshot if isinstance(snapshot, dict) else {}
    chat_url_match = bool(values.get("chat_url_match"))
    chat_text_hint_match = bool(values.get("chat_text_hint_match"))
    panel_count = bounded_count(values.get("conversation_panel_count"))
    input_count = bounded_count(values.get("message_input_count"))
    send_count = bounded_count(values.get("send_button_count"))
    generic_send_count = bounded_count(values.get("generic_send_control_count"))
    sensitive_signal_count = bounded_count(values.get("sensitive_signal_count"))
    chat_page_candidate = bool(chat_url_match or chat_text_hint_match or panel_count)
    if not chat_page_candidate:
        status = "非聊天页"
    elif sensitive_signal_count:
        status = "含敏感提示"
    elif input_count == 1 and (send_count == 1 or generic_send_count == 1):
        status = "结构可校准"
    else:
        status = "需人工校准"
    return {
        "platform": platform,
        "status": status,
        "chat_page_candidate": chat_page_candidate,
        "chat_url_match": chat_url_match,
        "chat_text_hint_match": chat_text_hint_match,
        "conversation_panel_count": panel_count,
        "message_input_count": input_count,
        "send_button_count": send_count,
        "generic_send_control_count": generic_send_count,
        "sensitive_signal_count": sensitive_signal_count,
    }


def bounded_count(value: object) -> int:
    try:
        return max(0, min(int(value or 0), 20))
    except (TypeError, ValueError):
        return 0


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


def probe_browser_send_adapter_plan(
    browser_plan: dict[str, object],
    *,
    browser_channel: str = "msedge",
    page_snapshots: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if browser_plan.get("status") == "已关闭":
        return {
            **browser_plan,
            "status": "已关闭",
            "note": "沟通模式为关闭，未探测浏览器页面。",
            "browser_probe_dry_run": True,
            "browser_connected": False,
            "probe_results": [],
        }

    if page_snapshots is None:
        page_snapshots = capture_browser_probe_snapshots(browser_plan, browser_channel=browser_channel)

    results = [
        probe_browser_plan_against_snapshots(item, page_snapshots)
        for item in list(browser_plan.get("browser_plans") or [])
        if isinstance(item, dict)
    ]
    ready_count = sum(1 for item in results if item.get("probe_status") == "probe_ready")
    partial_count = sum(1 for item in results if item.get("probe_status") == "probe_partial")
    not_found_count = sum(1 for item in results if item.get("probe_status") == "not_found")
    skipped_count = sum(1 for item in results if item.get("probe_status") == "skip")
    if not results:
        status = "无候选"
        note = "没有可探测的浏览器定位计划。"
    else:
        status = "探测完成"
        note = (
            f"探测 {len(results)} 条浏览器定位计划：可定位 {ready_count} 条，"
            f"部分匹配 {partial_count} 条，未找到 {not_found_count} 条，跳过 {skipped_count} 条。"
        )
    return {
        **browser_plan,
        "status": status,
        "note": note,
        "browser_probe_dry_run": True,
        "browser_connected": True,
        "page_count": len(page_snapshots),
        "probe_ready_count": ready_count,
        "probe_partial_count": partial_count,
        "probe_not_found_count": not_found_count,
        "probe_skipped_count": skipped_count,
        "probe_results": results,
        "message_text_saved": False,
    }


def capture_browser_probe_snapshots(
    browser_plan: dict[str, object],
    *,
    browser_channel: str = "msedge",
) -> list[dict[str, object]]:
    channel = normalize_browser_channel(browser_channel)
    if channel != "msedge":
        raise ValueError("当前浏览器发送探测先支持 Microsoft Edge。")
    try:
        if not wait_for_debug_endpoint(timeout_seconds=3):
            raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先点击“打开 Edge 巡检窗口”。")
        snapshots: list[dict[str, object]] = []
        for target in read_controlled_edge_targets():
            if not target_url(target).startswith(("http://", "https://")):
                continue
            try:
                snapshots.append(capture_browser_probe_target_snapshot(target, browser_plan))
            except Exception:
                continue
        return snapshots
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法连接当前 Edge 页面做发送探测：{message[:160]}。请先打开 Edge 巡检窗口并停留在 HR 对话页。") from exc


def capture_browser_probe_target_snapshot(target: dict, browser_plan: dict[str, object]) -> dict[str, object]:
    snapshot = evaluate_cdp_expression(
        target,
        controlled_edge_dom_snapshot_expression(collect_selector_candidates(browser_plan)),
    )
    if not isinstance(snapshot, dict):
        raise ValueError("受控 Edge 聊天页没有返回可读取的内容。")
    selector_counts = snapshot.get("selectors") if isinstance(snapshot.get("selectors"), dict) else {}
    return browser_probe_snapshot_from_values(
        str(snapshot.get("url") or target_url(target)),
        str(snapshot.get("title") or ""),
        str(snapshot.get("text") or ""),
        {str(selector): int(count or 0) for selector, count in selector_counts.items()},
    )


def snapshot_page_for_browser_probe(page, browser_plan: dict[str, object]) -> dict[str, object]:
    text = safe_page_text(page)
    title = safe_page_title(page)
    url = str(getattr(page, "url", "") or "")
    selector_candidates = collect_selector_candidates(browser_plan)
    return browser_probe_snapshot_from_values(
        url,
        title,
        text,
        {selector: safe_locator_count(page, selector) for selector in selector_candidates},
    )


def browser_probe_snapshot_from_values(
    url: str,
    title: str,
    text: str,
    selector_counts: dict[str, int],
) -> dict[str, object]:
    return {
        "url": url,
        "title": title[:300],
        "host": url_host(url),
        "text_length": len(text),
        "text_digest": text_digest(text),
        "normalized_text": normalize_probe_text(f"{title}\n{text}"),
        "selectors": selector_counts,
    }


def collect_selector_candidates(browser_plan: dict[str, object]) -> list[str]:
    selectors: list[str] = []
    for item in list(browser_plan.get("browser_plans") or []):
        if not isinstance(item, dict):
            continue
        candidates = item.get("selector_candidates")
        if not isinstance(candidates, dict):
            continue
        for values in candidates.values():
            if isinstance(values, list):
                selectors.extend(str(selector) for selector in values if str(selector).strip())
    return sorted(set(selectors))


def probe_browser_plan_against_snapshots(
    browser_plan_item: dict[str, object],
    page_snapshots: list[dict[str, object]],
) -> dict[str, object]:
    base = {
        "draft_id": browser_plan_item.get("draft_id"),
        "job_id": browser_plan_item.get("job_id"),
        "platform": browser_plan_item.get("platform") or "",
        "company": browser_plan_item.get("company") or "",
        "job_title": browser_plan_item.get("job_title") or "",
        "source_url": browser_plan_item.get("source_url") or "",
        "message_length": browser_plan_item.get("message_length") or 0,
        "message_text_included": False,
    }
    if browser_plan_item.get("browser_action") != "dry_run_ready":
        return {
            **base,
            "probe_status": "skip",
            "reason": "该计划未进入浏览器定位阶段。",
        }

    scored = [score_snapshot_for_plan(browser_plan_item, snapshot) for snapshot in page_snapshots]
    matched = [item for item in scored if item["page_match_score"] > 0]
    if not matched:
        return {
            **base,
            "probe_status": "not_found",
            "reason": "当前 Edge 页面中未找到匹配平台、公司或岗位线索的聊天页。",
            "checked_page_count": len(page_snapshots),
        }

    best = max(matched, key=lambda item: item["page_match_score"])
    input_found = int(best["message_input_count"]) > 0
    send_found = int(best["send_button_count"]) > 0
    panel_found = int(best["conversation_panel_count"]) > 0
    page_identity_ok = bool(best["domain_match"] and (best["company_match"] or best["job_title_match"] or best["source_host_match"]))
    page_chat_ok = bool(best["chat_url_match"] or best["chat_text_hint_match"] or panel_found)
    if page_identity_ok and page_chat_ok and input_found and send_found:
        status = "probe_ready"
        reason = "当前页面命中平台/岗位线索，并找到输入框和发送按钮候选。"
    else:
        status = "probe_partial"
        reason = "当前页面有部分匹配，但页面身份、聊天线索、输入框或发送按钮仍不完整。"

    return {
        **base,
        "probe_status": status,
        "reason": reason,
        "matched_page": {
            "host": best["host"],
            "text_length": best["text_length"],
            "text_digest": best["text_digest"],
            "domain_match": best["domain_match"],
            "source_host_match": best["source_host_match"],
            "company_match": best["company_match"],
            "job_title_match": best["job_title_match"],
            "chat_url_match": best["chat_url_match"],
            "chat_text_hint_match": best["chat_text_hint_match"],
            "conversation_panel_count": best["conversation_panel_count"],
            "message_input_count": best["message_input_count"],
            "send_button_count": best["send_button_count"],
            "page_match_score": best["page_match_score"],
        },
    }


def score_snapshot_for_plan(plan: dict[str, object], snapshot: dict[str, object]) -> dict[str, object]:
    page_match = plan.get("page_match") if isinstance(plan.get("page_match"), dict) else {}
    selectors = plan.get("selector_candidates") if isinstance(plan.get("selector_candidates"), dict) else {}
    text = str(snapshot.get("normalized_text") or "")
    host = str(snapshot.get("host") or "")
    source_host = url_host(str(plan.get("source_url") or ""))
    domains = [str(item).lower() for item in list(page_match.get("domains") or [])]
    chat_url_tokens = [str(item).lower() for item in list(page_match.get("chat_url_tokens") or [])]
    chat_text_hints = [normalize_probe_text(str(item)) for item in list(page_match.get("chat_text_hints") or [])]
    url = str(snapshot.get("url") or "").lower()
    domain_match = any(host == domain or host.endswith("." + domain) for domain in domains)
    source_host_match = bool(source_host and host == source_host)
    company_match = contains_normalized(text, str(plan.get("company") or ""))
    job_title_match = any(contains_normalized(text, token) for token in job_title_tokens(str(plan.get("job_title") or "")))
    chat_url_match = any(token in url for token in chat_url_tokens if token)
    chat_text_hint_match = any(hint in text for hint in chat_text_hints if hint)
    conversation_panel_count = selector_count(snapshot, selectors.get("conversation_panel"))
    message_input_count = selector_count(snapshot, selectors.get("message_input"))
    send_button_count = selector_count(snapshot, selectors.get("send_button"))
    score = 0
    if domain_match:
        score += 20
    if source_host_match:
        score += 10
    if company_match:
        score += 20
    if job_title_match:
        score += 12
    if chat_url_match:
        score += 8
    if chat_text_hint_match:
        score += 8
    if conversation_panel_count:
        score += 4
    return {
        "host": host,
        "text_length": int(snapshot.get("text_length") or 0),
        "text_digest": str(snapshot.get("text_digest") or ""),
        "domain_match": domain_match,
        "source_host_match": source_host_match,
        "company_match": company_match,
        "job_title_match": job_title_match,
        "chat_url_match": chat_url_match,
        "chat_text_hint_match": chat_text_hint_match,
        "conversation_panel_count": conversation_panel_count,
        "message_input_count": message_input_count,
        "send_button_count": send_button_count,
        "page_match_score": score,
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
    if platform in RECRUITMENT_PLATFORM_DOMAINS and not is_pc_message_automation_platform(platform):
        return {
            **base,
            "browser_action": "manual_locate",
            "supported": False,
            "reason": f"{platform} 当前没有可用的 PC 消息界面，暂不进入消息自动化；岗位发现、JD 分析和投递准备仍可使用。",
            "gate_reasons": [],
        }
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
        "reason": "已生成受控浏览器定位策略；只有自主沟通工作流的全部闸门通过时才允许发送。",
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
            "页面出现身份证、银行卡、付费、押金、培训费等高风险提示时停止。",
            "对话中出现简历附件、联系方式或面试安排时仍由发送闸门转人工。",
            "真实发送必须由用户显式启动自主沟通工作流，并保留审计日志。",
        ],
    }


def fill_message_in_controlled_edge(
    browser_plan_item: dict[str, object],
    message: str,
    *,
    browser_channel: str = "msedge",
) -> dict[str, object]:
    """Fill one verified chat input without clicking any platform control."""
    if browser_plan_item.get("browser_action") != "dry_run_ready":
        raise ValueError(str(browser_plan_item.get("reason") or "当前草稿不能进入浏览器填入流程。"))
    if not str(message or "").strip():
        raise ValueError("草稿内容为空，不能填入浏览器。")
    if normalize_browser_channel(browser_channel) != "msedge":
        raise ValueError("当前浏览器填入先支持 Microsoft Edge。")
    try:
        target, probe = find_unique_verified_cdp_chat_target(browser_plan_item, action="填入草稿")
        action_result = evaluate_cdp_expression(target, fill_message_expression(browser_plan_item, message))
        selector = require_cdp_action_selector(action_result, action="填入草稿")
        return {
            "status": "已填入",
            "note": "已填入当前 Edge 聊天输入框，未点击发送。",
            "filled_selector": selector,
            "matched_page": probe.get("matched_page") or {},
            "message_filled": True,
            "browser_clicked": False,
            "message_text_saved": False,
        }
    except ValueError:
        raise
    except Exception as exc:
        message_text = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法填入当前 Edge 聊天输入框：{message_text[:180]}。") from exc
def send_message_in_controlled_edge(
    browser_plan_item: dict[str, object],
    message: str,
    *,
    browser_channel: str = "msedge",
) -> dict[str, object]:
    """Fill and send one message only after the chat page is re-verified."""
    if browser_plan_item.get("browser_action") != "dry_run_ready":
        raise ValueError(str(browser_plan_item.get("reason") or "当前草稿不能进入浏览器发送流程。"))
    if not str(message or "").strip():
        raise ValueError("草稿内容为空，不能发送。")
    if normalize_browser_channel(browser_channel) != "msedge":
        raise ValueError("当前浏览器发送先支持 Microsoft Edge。")
    try:
        target, probe = find_unique_verified_cdp_chat_target(browser_plan_item, action="发送草稿")
        filled_selector = require_cdp_action_selector(
            evaluate_cdp_expression(target, fill_message_expression(browser_plan_item, message)),
            action="填入草稿",
        )

        # Re-check after filling. The page can change while the action is in progress.
        post_fill_snapshot = capture_browser_probe_target_snapshot(target, {"browser_plans": [browser_plan_item]})
        post_fill_probe = probe_browser_plan_against_snapshots(browser_plan_item, [post_fill_snapshot])
        blocked_signals = find_fill_blocking_signals(post_fill_snapshot)
        if post_fill_probe.get("probe_status") != "probe_ready" or blocked_signals:
            raise ValueError("填写后页面状态发生变化，已停止点击发送按钮。")

        send_selector = click_send_button_in_cdp(target, browser_plan_item)
        return {
            "status": "已发送",
            "note": "已点击当前 Edge 聊天页的发送按钮。",
            "filled_selector": filled_selector,
            "send_selector": send_selector,
            "matched_page": probe.get("matched_page") or {},
            "message_filled": True,
            "browser_clicked": True,
            "message_text_saved": False,
        }
    except ValueError:
        raise
    except Exception as exc:
        message_text = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法发送当前 Edge 聊天草稿：{message_text[:180]}。") from exc
def find_fill_blocking_signals(snapshot: dict[str, object]) -> list[str]:
    text = str(snapshot.get("normalized_text") or "")
    return [signal for signal in HARD_PAGE_BLOCKING_TEXT if normalize_probe_text(signal) in text]


def find_unique_verified_cdp_chat_target(
    browser_plan_item: dict[str, object],
    *,
    action: str,
) -> tuple[dict, dict[str, object]]:
    if not wait_for_debug_endpoint(timeout_seconds=3):
        raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先打开 Edge 巡检窗口并停留在对应 HR 对话页。")
    candidates: list[tuple[int, dict, dict[str, object]]] = []
    for target in read_controlled_edge_targets():
        if not target_url(target).startswith(("http://", "https://")):
            continue
        try:
            snapshot = capture_browser_probe_target_snapshot(target, {"browser_plans": [browser_plan_item]})
        except Exception:
            continue
        blocked_signals = find_fill_blocking_signals(snapshot)
        probe = probe_browser_plan_against_snapshots(browser_plan_item, [snapshot])
        if probe.get("probe_status") == "probe_ready" and not blocked_signals:
            score = int((probe.get("matched_page") or {}).get("page_match_score") or 0)
            candidates.append((score, target, probe))
    _, target, probe = select_unique_verified_page(candidates, action=action)
    return target, probe


def require_cdp_action_selector(result: object, *, action: str) -> str:
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str(result.get("reason") or "页面未找到唯一可用控件。") if isinstance(result, dict) else "页面没有返回动作结果。"
        raise ValueError(f"{action}失败：{reason}")
    selector = str(result.get("selector") or "")
    if not selector:
        raise ValueError(f"{action}失败：页面没有返回实际控件。")
    return selector


def action_selector_list(browser_plan_item: dict[str, object], name: str) -> list[str]:
    candidates = browser_plan_item.get("selector_candidates")
    values = candidates.get(name) if isinstance(candidates, dict) else []
    return [str(selector) for selector in values if str(selector).strip()] if isinstance(values, list) else []


def fill_message_expression(browser_plan_item: dict[str, object], message: str) -> str:
    selector_payload = json.dumps(action_selector_list(browser_plan_item, "message_input"), ensure_ascii=False)
    message_payload = json.dumps(str(message), ensure_ascii=False)
    return f"""(() => {{
        const selectors = {selector_payload};
        const message = {message_payload};
        const visible = element => {{
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        }};
        const editable = element => {{
            if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) return !element.disabled && !element.readOnly;
            return element.isContentEditable;
        }};
        const fields = new Map();
        for (const selector of selectors) {{
            try {{
                for (const element of document.querySelectorAll(selector)) {{
                    if (visible(element) && editable(element) && !fields.has(element)) fields.set(element, selector);
                }}
            }} catch (_error) {{}}
        }}
        if (fields.size !== 1) return {{ok: false, reason: fields.size ? '找到多个可填写输入框，已停止填入。' : '未找到唯一可填写输入框。'}};
        const [[field, selector]] = fields.entries();
        field.focus();
        if (field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement) {{
            const prototype = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
            if (setter) setter.call(field, message);
            else field.value = message;
        }} else {{
            field.textContent = message;
        }}
        field.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: message}}));
        field.dispatchEvent(new Event('change', {{bubbles: true}}));
        return {{ok: true, selector}};
    }})()"""


def click_send_button_in_cdp(target: dict, browser_plan_item: dict[str, object]) -> str:
    result = evaluate_cdp_expression(target, send_button_location_expression(browser_plan_item))
    selector = require_cdp_action_selector(result, action="发送草稿")
    if not isinstance(result, dict):
        raise ValueError("发送草稿失败：页面没有返回发送按钮位置。")
    try:
        x = float(result["x"])
        y = float(result["y"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("发送草稿失败：页面没有返回有效发送按钮位置。") from exc
    for event_type, button_count in (("mousePressed", 1), ("mouseReleased", 1)):
        send_cdp_command(
            target,
            "Input.dispatchMouseEvent",
            {"type": event_type, "x": x, "y": y, "button": "left", "clickCount": button_count},
        )
    return selector


def send_button_location_expression(browser_plan_item: dict[str, object]) -> str:
    selector_payload = json.dumps(action_selector_list(browser_plan_item, "send_button"), ensure_ascii=False)
    return f"""(() => {{
        const selectors = {selector_payload};
        const visible = element => {{
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        }};
        const controlLabel = element => (element.getAttribute('aria-label') || element.getAttribute('title') || element.innerText || element.textContent || '').trim().replace(/\\s+/g, '');
        const buttons = new Map();
        for (const selector of selectors) {{
            const textSelector = /^(.*):has-text\\((['\"])(.*?)\\2\\)$/;
            const match = selector.match(textSelector);
            const baseSelector = match ? match[1] : selector;
            const text = match ? match[3] : '';
            try {{
                for (const element of document.querySelectorAll(baseSelector)) {{
                    const label = controlLabel(element);
                    if (visible(element) && !element.disabled && (!text || label.includes(text)) && (label === '发送' || label === '发送消息')) {{
                        if (!buttons.has(element)) buttons.set(element, selector);
                    }}
                }}
            }} catch (_error) {{}}
        }}
        if (buttons.size !== 1) return {{ok: false, reason: buttons.size ? '找到多个可用发送按钮，已停止发送。' : '未找到唯一可用发送按钮。'}};
        const [[button, selector]] = buttons.entries();
        const rect = button.getBoundingClientRect();
        return {{ok: true, selector, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2}};
    }})()"""


def select_unique_verified_page(candidates: list[tuple[int, object, dict[str, object]]], *, action: str):
    if not candidates:
        raise ValueError(f"未找到可安全{action}的对应聊天页，请先确认页面身份唯一且没有高风险或敏感提示。")
    best_score = max(item[0] for item in candidates)
    best_matches = [item for item in candidates if item[0] == best_score]
    if len(best_matches) != 1:
        raise ValueError(f"找到多个同等匹配的聊天页，已停止{action}。")
    return best_matches[0]


def url_host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def safe_page_title(page) -> str:
    try:
        return str(page.title() or "").strip()[:300]
    except Exception:
        return ""


def safe_page_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5000) or "")[:20000]
    except Exception:
        return ""


def safe_locator_count(page, selector: str) -> int:
    try:
        locator = page.locator(selector)
        count = min(int(locator.count()), 20)
        visible_count = 0
        for index in range(count):
            try:
                if locator.nth(index).is_visible(timeout=300):
                    visible_count += 1
            except Exception:
                continue
        return visible_count
    except Exception:
        return 0


def selector_count(snapshot: dict[str, object], selectors: object) -> int:
    selector_map = snapshot.get("selectors") if isinstance(snapshot.get("selectors"), dict) else {}
    if not isinstance(selectors, list):
        return 0
    return sum(int(selector_map.get(selector, 0) or 0) for selector in selectors)


def normalize_probe_text(value: str) -> str:
    return "".join(str(value or "").lower().split())


def contains_normalized(text: str, value: str) -> bool:
    token = normalize_probe_text(value)
    return bool(token and token in text)


def job_title_tokens(title: str) -> list[str]:
    normalized = normalize_probe_text(title)
    tokens = [normalized] if normalized else []
    for token in ["ai", "人工智能", "大模型", "agent", "智能体", "应用开发", "后端", "python", "rag", "实习"]:
        if token in normalized:
            tokens.append(token)
    return sorted(set(tokens), key=len, reverse=True)


def text_digest(value: str) -> str:
    text = normalize_probe_text(value)
    if not text:
        return ""
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}|len:{len(text)}"
