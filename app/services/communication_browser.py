from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from .job_fetcher import normalize_browser_channel
from .job_searcher import EDGE_DEBUG_PORT, wait_for_debug_endpoint


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

FILL_BLOCKING_TEXT = (
    "身份证",
    "银行卡",
    "培训费",
    "押金",
    "贷款",
    "付费",
    "扫码",
    "上传简历",
    "发简历",
    "附件简历",
)


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
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("浏览器发送探测需要安装 Playwright：pip install playwright。") from exc

    browser = None
    try:
        with sync_playwright() as playwright:
            if not wait_for_debug_endpoint(timeout_seconds=3):
                raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先点击“打开 Edge 巡检窗口”。")
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{EDGE_DEBUG_PORT}", timeout=8000)
            pages = [page for context in browser.contexts for page in context.pages if page.url and page.url != "about:blank"]
            snapshots: list[dict[str, object]] = []
            for page in pages:
                snapshots.append(snapshot_page_for_browser_probe(page, browser_plan))
            browser.close()
            browser = None
            return snapshots
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法连接当前 Edge 页面做发送探测：{message[:160]}。请先打开 Edge 巡检窗口并停留在 HR 对话页。") from exc
    finally:
        if browser is not None:
            browser.close()


def snapshot_page_for_browser_probe(page, browser_plan: dict[str, object]) -> dict[str, object]:
    text = safe_page_text(page)
    title = safe_page_title(page)
    url = str(getattr(page, "url", "") or "")
    selector_candidates = collect_selector_candidates(browser_plan)
    return {
        "url": url,
        "title": title,
        "host": url_host(url),
        "text_length": len(text),
        "text_digest": text_digest(text),
        "normalized_text": normalize_probe_text(f"{title}\n{text}"),
        "selectors": {
            selector: safe_locator_count(page, selector)
            for selector in selector_candidates
        },
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
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("浏览器填入需要安装 Playwright：pip install playwright。") from exc

    browser = None
    try:
        with sync_playwright() as playwright:
            if not wait_for_debug_endpoint(timeout_seconds=3):
                raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先打开 Edge 巡检窗口并停留在对应 HR 对话页。")
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{EDGE_DEBUG_PORT}", timeout=8000)
            pages = [page for context in browser.contexts for page in context.pages if page.url and page.url != "about:blank"]
            candidates = []
            for page in pages:
                snapshot = snapshot_page_for_browser_probe(page, {"browser_plans": [browser_plan_item]})
                blocked_signals = find_fill_blocking_signals(snapshot)
                probe = probe_browser_plan_against_snapshots(browser_plan_item, [snapshot])
                if probe.get("probe_status") == "probe_ready" and not blocked_signals:
                    candidates.append((int((probe.get("matched_page") or {}).get("page_match_score") or 0), page, probe))
            if not candidates:
                raise ValueError("未找到可安全填入的对应聊天页，请先完成只读探测并确认页面没有简历、联系方式或付费等敏感提示。")
            _, page, probe = max(candidates, key=lambda item: item[0])
            selector = fill_first_visible_message_input(page, browser_plan_item, message)
            result = {
                "status": "已填入",
                "note": "已填入当前 Edge 聊天输入框，未点击发送。",
                "filled_selector": selector,
                "matched_page": probe.get("matched_page") or {},
                "message_filled": True,
                "browser_clicked": False,
                "message_text_saved": False,
            }
            browser.close()
            browser = None
            return result
    except ValueError:
        raise
    except Exception as exc:
        message_text = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法填入当前 Edge 聊天输入框：{message_text[:180]}。") from exc
    finally:
        if browser is not None:
            browser.close()


def find_fill_blocking_signals(snapshot: dict[str, object]) -> list[str]:
    text = str(snapshot.get("normalized_text") or "")
    return [signal for signal in FILL_BLOCKING_TEXT if normalize_probe_text(signal) in text]


def fill_first_visible_message_input(page, browser_plan_item: dict[str, object], message: str) -> str:
    candidates = browser_plan_item.get("selector_candidates")
    selectors = candidates.get("message_input") if isinstance(candidates, dict) else []
    for selector in selectors if isinstance(selectors, list) else []:
        locator = page.locator(str(selector))
        count = min(int(locator.count()), 20)
        for index in range(count):
            field = locator.nth(index)
            try:
                if not field.is_visible(timeout=500) or not field.is_enabled(timeout=500):
                    continue
                field.fill(message, timeout=5000)
                return str(selector)
            except Exception:
                continue
    raise ValueError("已确认聊天页，但未能向任何可见输入框填入草稿。")


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
