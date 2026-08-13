from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

from .job_fetcher import normalize_browser_channel
from .job_searcher import (
    controlled_edge_dom_snapshot_expression,
    evaluate_cdp_expression,
    read_controlled_edge_targets,
    target_url,
    wait_for_debug_endpoint,
)


APPLICATION_PLATFORM_STRATEGIES: dict[str, dict[str, object]] = {
    "Boss 直聘": {
        "domains": ["zhipin.com"],
        "application_button_selectors": [
            "button:has-text('立即沟通')",
            "button:has-text('投递简历')",
            "button:has-text('立即投递')",
            "[role='button']:has-text('立即沟通')",
        ],
        "resume_control_selectors": [
            "input[type='file']",
            "button:has-text('上传简历')",
            "button:has-text('选择简历')",
        ],
        "application_note_selectors": [
            "textarea[placeholder*='自我介绍']",
            "textarea[placeholder*='附言']",
            "textarea[placeholder*='留言']",
            "textarea[name*='cover']",
            "textarea[aria-label*='自我介绍']",
            "[contenteditable='true'][aria-label*='自我介绍']",
        ],
    },
    "猎聘": {
        "domains": ["liepin.com"],
        "application_button_selectors": [
            "button:has-text('投递简历')",
            "button:has-text('立即投递')",
            "[role='button']:has-text('投递简历')",
        ],
        "resume_control_selectors": [
            "input[type='file']",
            "button:has-text('上传简历')",
            "button:has-text('选择简历')",
        ],
        "application_note_selectors": [
            "textarea[placeholder*='自我介绍']",
            "textarea[placeholder*='附言']",
            "textarea[placeholder*='留言']",
            "textarea[name*='cover']",
            "textarea[aria-label*='自我介绍']",
            "[contenteditable='true'][aria-label*='自我介绍']",
        ],
    },
    "实习僧": {
        "domains": ["shixiseng.com"],
        "application_button_selectors": [
            "button:has-text('投递')",
            "button:has-text('立即投递')",
            "[role='button']:has-text('投递')",
        ],
        "resume_control_selectors": [
            "input[type='file']",
            "button:has-text('上传简历')",
            "button:has-text('选择简历')",
        ],
        "application_note_selectors": [
            "textarea[placeholder*='自我介绍']",
            "textarea[placeholder*='附言']",
            "textarea[placeholder*='留言']",
            "textarea[name*='cover']",
            "textarea[aria-label*='自我介绍']",
            "[contenteditable='true'][aria-label*='自我介绍']",
        ],
    },
    "智联招聘": {
        "domains": ["zhaopin.com"],
        "application_button_selectors": [
            "button:has-text('立即投递')",
            "button:has-text('投递简历')",
            "[role='button']:has-text('立即投递')",
        ],
        "resume_control_selectors": [
            "input[type='file']",
            "button:has-text('上传简历')",
            "button:has-text('选择简历')",
        ],
        "application_note_selectors": [
            "textarea[placeholder*='自我介绍']",
            "textarea[placeholder*='附言']",
            "textarea[placeholder*='留言']",
            "textarea[name*='cover']",
            "textarea[aria-label*='自我介绍']",
            "[contenteditable='true'][aria-label*='自我介绍']",
        ],
    },
    "前程无忧": {
        "domains": ["51job.com"],
        "application_button_selectors": [
            "button:has-text('立即投递')",
            "button:has-text('投递简历')",
            "[role='button']:has-text('立即投递')",
        ],
        "resume_control_selectors": [
            "input[type='file']",
            "button:has-text('上传简历')",
            "button:has-text('选择简历')",
        ],
        "application_note_selectors": [
            "textarea[placeholder*='自我介绍']",
            "textarea[placeholder*='附言']",
            "textarea[placeholder*='留言']",
            "textarea[name*='cover']",
            "textarea[aria-label*='自我介绍']",
            "[contenteditable='true'][aria-label*='自我介绍']",
        ],
    },
}

APPLICATION_FILL_BLOCKING_SIGNALS = (
    "验证码",
    "人脸识别",
    "身份证",
    "银行卡",
    "培训费",
    "押金",
    "贷款",
    "付费",
    "支付",
    "收费",
)


def build_application_browser_plan(item: dict[str, object]) -> dict[str, object]:
    base = {
        "preparation_id": item.get("preparation_id"),
        "job_id": item.get("job_id"),
        "platform": str(item.get("platform") or ""),
        "company": str(item.get("company") or ""),
        "job_title": str(item.get("job_title") or ""),
        "source_url": str(item.get("source_url") or ""),
        "resume_id": item.get("resume_id"),
        "resume_name": str(item.get("resume_name") or ""),
        "application_message_length": len(str(item.get("application_message") or "").strip()),
        "browser_filled": False,
        "browser_clicked": False,
        "resume_uploaded": False,
    }
    if item.get("preparation_status") != "已确认" or item.get("job_status") != "待投递":
        return {
            **base,
            "browser_action": "blocked",
            "reason": "只有已确认且状态为待投递的岗位可以进入浏览器演练。",
        }
    if not base["source_url"]:
        return {
            **base,
            "browser_action": "blocked",
            "reason": "岗位没有可打开的来源链接，无法进入浏览器演练。",
        }
    strategy = APPLICATION_PLATFORM_STRATEGIES.get(base["platform"])
    if not strategy:
        return {
            **base,
            "browser_action": "manual_locate",
            "reason": "该平台尚未配置投递控件定位策略，可先在受控 Edge 中人工打开岗位页。",
        }
    return {
        **base,
        "browser_action": "dry_run_ready",
        "reason": "已生成投递页探测策略；投递附言需用户明确确认后才允许填入，当前不会上传、点击或提交。",
        "page_match": {
            "domains": strategy["domains"],
            "source_url_host": url_host(base["source_url"]),
        },
        "selector_candidates": {
            "application_button": strategy["application_button_selectors"],
            "resume_control": strategy["resume_control_selectors"],
            "application_note": strategy["application_note_selectors"],
        },
        "dry_run_steps": [
            "连接应用打开的专用 Edge，并读取当前已打开页面的可见结构。",
            "以平台域名、公司名和岗位名称确认页面身份。",
            "统计投递按钮、简历控件和投递附言输入框候选，不读取或保存页面正文。",
            "不填写字段、不上传简历、不点击投递按钮。",
        ],
        "safety_checks": [
            "岗位、公司或平台不匹配时停止。",
            "页面出现验证码、身份信息、付费或风险提示时停止并转人工。",
            "附言填入需要用户输入确认短语，并且只允许唯一可填写字段。",
            "不会上传简历、点击投递或提交表单。",
        ],
    }


def probe_application_browser_plan(
    plan: dict[str, object],
    *,
    browser_channel: str = "msedge",
    page_snapshots: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    if plan.get("browser_action") != "dry_run_ready":
        return {
            **plan,
            "status": "未进入探测",
            "note": str(plan.get("reason") or "当前计划不满足浏览器演练条件。"),
            "browser_probe_dry_run": True,
            "browser_connected": False,
            "probe_result": None,
        }
    if page_snapshots is None:
        page_snapshots = capture_application_browser_snapshots(plan, browser_channel=browser_channel)
    matched = [score_application_page(plan, snapshot) for snapshot in page_snapshots]
    candidates = [item for item in matched if item["page_match_score"] > 0]
    if not candidates:
        result = {
            "probe_status": "not_found",
            "reason": "受控 Edge 当前页面未找到匹配的平台、公司或岗位线索。",
            "checked_page_count": len(page_snapshots),
        }
    else:
        best = max(candidates, key=lambda item: item["page_match_score"])
        identity_ok = bool(best["domain_match"] and (best["company_match"] or best["job_title_match"] or best["source_host_match"]))
        control_found = bool(best["application_button_count"] or best["resume_control_count"])
        result = {
            **best,
            "probe_status": "probe_ready" if identity_ok and control_found else "probe_partial",
            "reason": (
                "页面身份匹配，且已找到投递或简历控件候选。"
                if identity_ok and control_found
                else "页面有部分匹配，但岗位身份或投递控件仍不完整。"
            ),
            "checked_page_count": len(page_snapshots),
        }
    status = "探测完成" if result["probe_status"] != "not_found" else "未找到页面"
    return {
        **plan,
        "status": status,
        "note": str(result["reason"]),
        "browser_probe_dry_run": True,
        "browser_connected": True,
        "page_count": len(page_snapshots),
        "probe_result": result,
    }


def capture_application_browser_snapshots(plan: dict[str, object], *, browser_channel: str = "msedge") -> list[dict[str, object]]:
    if normalize_browser_channel(browser_channel) != "msedge":
        raise ValueError("当前投递页面演练先支持 Microsoft Edge。")
    try:
        if not wait_for_debug_endpoint(timeout_seconds=3):
            raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先打开投递岗位页。")
        snapshots: list[dict[str, object]] = []
        for target in read_controlled_edge_targets():
            if not target_url(target).startswith(("http://", "https://")):
                continue
            try:
                snapshots.append(capture_application_target_snapshot(target, plan))
            except Exception:
                continue
        return snapshots
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法连接当前 Edge 页面做投递演练：{message[:160]}。") from exc


def capture_application_target_snapshot(target: dict, plan: dict[str, object]) -> dict[str, object]:
    snapshot = evaluate_cdp_expression(target, controlled_edge_dom_snapshot_expression(application_selector_list(plan)))
    if not isinstance(snapshot, dict):
        raise ValueError("受控 Edge 投递页面没有返回可读取的内容。")
    selector_counts = snapshot.get("selectors") if isinstance(snapshot.get("selectors"), dict) else {}
    return application_snapshot_from_values(
        str(snapshot.get("url") or target_url(target)),
        str(snapshot.get("title") or ""),
        str(snapshot.get("text") or ""),
        {str(selector): int(count or 0) for selector, count in selector_counts.items()},
    )


def application_selector_list(plan: dict[str, object]) -> list[str]:
    selector_candidates = plan.get("selector_candidates") if isinstance(plan.get("selector_candidates"), dict) else {}
    return sorted(
        {
            str(selector)
            for group in selector_candidates.values()
            if isinstance(group, list)
            for selector in group
            if str(selector).strip()
        }
    )


def snapshot_application_page(page, plan: dict[str, object]) -> dict[str, object]:
    text = safe_page_text(page)
    url = str(getattr(page, "url", "") or "")
    selectors = application_selector_list(plan)
    return application_snapshot_from_values(
        url,
        safe_page_title(page),
        text,
        {selector: safe_locator_count(page, selector) for selector in selectors},
    )


def application_snapshot_from_values(
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
        "normalized_text": normalize_text(f"{title}\n{text}"),
        "selectors": selector_counts,
    }


def score_application_page(plan: dict[str, object], snapshot: dict[str, object]) -> dict[str, object]:
    page_match = plan.get("page_match") if isinstance(plan.get("page_match"), dict) else {}
    selectors = plan.get("selector_candidates") if isinstance(plan.get("selector_candidates"), dict) else {}
    text = str(snapshot.get("normalized_text") or "")
    host = str(snapshot.get("host") or "")
    domains = [str(item).lower() for item in list(page_match.get("domains") or [])]
    source_host = str(page_match.get("source_url_host") or "")
    domain_match = any(host == domain or host.endswith("." + domain) for domain in domains)
    source_host_match = bool(source_host and host == source_host)
    company_match = contains_normalized(text, str(plan.get("company") or ""))
    job_title_match = any(contains_normalized(text, token) for token in job_title_tokens(str(plan.get("job_title") or "")))
    application_button_count = selector_count(snapshot, selectors.get("application_button"))
    resume_control_count = selector_count(snapshot, selectors.get("resume_control"))
    score = 20 if domain_match else 0
    score += 10 if source_host_match else 0
    score += 20 if company_match else 0
    score += 12 if job_title_match else 0
    score += 4 if application_button_count else 0
    return {
        "host": host,
        "text_length": int(snapshot.get("text_length") or 0),
        "text_digest": str(snapshot.get("text_digest") or ""),
        "domain_match": domain_match,
        "source_host_match": source_host_match,
        "company_match": company_match,
        "job_title_match": job_title_match,
        "application_button_count": application_button_count,
        "resume_control_count": resume_control_count,
        "application_note_control_count": selector_count(snapshot, selectors.get("application_note")),
        "page_match_score": score,
    }


def fill_application_note_in_controlled_edge(
    plan: dict[str, object],
    application_message: str,
    *,
    browser_channel: str = "msedge",
) -> dict[str, object]:
    """Fill one verified application-note field without uploading or submitting."""
    if plan.get("browser_action") != "dry_run_ready":
        raise ValueError(str(plan.get("reason") or "当前岗位不能进入投递附言填入流程。"))
    message = str(application_message or "").strip()
    if not message:
        raise ValueError("投递附言为空，不能填入浏览器。")
    if normalize_browser_channel(browser_channel) != "msedge":
        raise ValueError("当前投递附言填入先支持 Microsoft Edge。")

    try:
        target, probe = find_unique_verified_application_target(plan)
        action_result = evaluate_cdp_expression(target, fill_application_note_expression(plan, message))
        selector = require_application_action_selector(action_result, action="填入投递附言")
        return {
            "status": "已填入",
            "note": "已填入当前 Edge 的投递附言，未上传简历、未点击投递或提交。",
            "filled_selector": selector,
            "matched_page": probe,
            "application_message_filled": True,
            "browser_clicked": False,
            "resume_uploaded": False,
            "application_message_saved": False,
        }
    except ValueError:
        raise
    except Exception as exc:
        detail = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法填入当前 Edge 的投递附言：{detail[:180]}。") from exc


def find_unique_verified_application_target(plan: dict[str, object]) -> tuple[dict, dict[str, object]]:
    if not wait_for_debug_endpoint(timeout_seconds=3):
        raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先打开岗位页并完成只读演练。")

    candidates: list[tuple[dict, dict[str, object]]] = []
    for target in read_controlled_edge_targets():
        if not target_url(target).startswith(("http://", "https://")):
            continue
        try:
            snapshot = capture_application_target_snapshot(target, plan)
        except Exception:
            continue
        if application_fill_blocking_signals(snapshot):
            continue
        probe = score_application_page(plan, snapshot)
        identity_ok = bool(
            probe["domain_match"]
            and (probe["company_match"] or probe["job_title_match"] or probe["source_host_match"])
        )
        if identity_ok and int(probe["application_note_control_count"] or 0) > 0:
            candidates.append((target, probe))

    if not candidates:
        raise ValueError("未找到身份匹配且有投递附言输入框的安全页面；请先在受控 Edge 手动打开投递弹窗后重试。")
    if len(candidates) != 1:
        raise ValueError("找到多个可填写的投递页面，已停止操作；请只保留当前岗位页面后重试。")
    return candidates[0]


def application_fill_blocking_signals(snapshot: dict[str, object]) -> list[str]:
    text = str(snapshot.get("normalized_text") or "")
    return [signal for signal in APPLICATION_FILL_BLOCKING_SIGNALS if normalize_text(signal) in text]


def require_application_action_selector(result: object, *, action: str) -> str:
    if not isinstance(result, dict) or not result.get("ok"):
        reason = str(result.get("reason") or "页面未找到唯一可填写附言控件。") if isinstance(result, dict) else "页面没有返回动作结果。"
        raise ValueError(f"{action}失败：{reason}")
    selector = str(result.get("selector") or "")
    if not selector:
        raise ValueError(f"{action}失败：页面没有返回实际控件。")
    return selector


def fill_application_note_expression(plan: dict[str, object], application_message: str) -> str:
    selector_payload = json.dumps(application_note_selectors(plan), ensure_ascii=False)
    message_payload = json.dumps(application_message, ensure_ascii=False)
    return f"""(() => {{
        const selectors = {selector_payload};
        const message = {message_payload};
        const visible = element => {{
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        }};
        const editableNote = element =>
            (element instanceof HTMLTextAreaElement && !element.disabled && !element.readOnly)
            || element.isContentEditable;
        const fields = new Map();
        for (const selector of selectors) {{
            try {{
                for (const element of document.querySelectorAll(selector)) {{
                    if (visible(element) && editableNote(element) && !fields.has(element)) fields.set(element, selector);
                }}
            }} catch (_error) {{}}
        }}
        if (fields.size !== 1) return {{ok: false, reason: fields.size ? '找到多个可填写投递附言输入框，已停止填入。' : '未找到唯一可填写投递附言输入框。'}};
        const [[field, selector]] = fields.entries();
        field.focus();
        if (field instanceof HTMLTextAreaElement) {{
            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
            if (setter) setter.call(field, message);
            else field.value = message;
        }} else {{
            field.textContent = message;
        }}
        field.dispatchEvent(new InputEvent('input', {{bubbles: true, inputType: 'insertText', data: message}}));
        field.dispatchEvent(new Event('change', {{bubbles: true}}));
        return {{ok: true, selector}};
    }})()"""


def application_note_selectors(plan: dict[str, object]) -> list[str]:
    candidates = plan.get("selector_candidates")
    values = candidates.get("application_note") if isinstance(candidates, dict) else []
    return [str(selector) for selector in values if str(selector).strip()] if isinstance(values, list) else []


def job_title_tokens(title: str) -> list[str]:
    compact = re.sub(r"\s+", "", title or "")
    tokens = [compact]
    for suffix in ("实习生", "实习", "岗位", "职位"):
        if compact.endswith(suffix):
            tokens.append(compact[: -len(suffix)])
    return [token for token in tokens if len(token) >= 3]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def contains_normalized(text: str, value: str) -> bool:
    target = normalize_text(value)
    return bool(target and target in text)


def selector_count(snapshot: dict[str, object], selectors: object) -> int:
    selector_map = snapshot.get("selectors") if isinstance(snapshot.get("selectors"), dict) else {}
    if not isinstance(selectors, list):
        return 0
    return sum(int(selector_map.get(selector, 0) or 0) for selector in selectors)


def url_host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def text_digest(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]}|len:{len(normalized)}"


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
