from __future__ import annotations

import subprocess
from urllib.parse import urlparse

from .job_fetcher import ensure_public_http_url, normalize_browser_channel
from .job_searcher import (
    EDGE_DEBUG_PORT,
    browser_profile_dir,
    find_edge_executable,
    evaluate_cdp_expression,
    is_debug_endpoint_ready,
    open_url_in_debug_browser,
    read_controlled_edge_targets,
    target_url,
    wait_for_debug_endpoint,
)


CHAT_URL_TOKENS = [
    "chat",
    "im",
    "message",
    "msg",
    "conversation",
    "communicate",
    "talk",
]
CHAT_TITLE_TOKENS = ["聊天", "沟通", "消息", "私信", "HR"]
CHAT_TEXT_TOKENS = ["请输入文字", "按Enter键发送", "发送", "发简历", "交换手机号", "交换微信号"]
RECRUITMENT_DOMAINS = {
    "zhipin.com": "Boss 直聘",
    "liepin.com": "猎聘",
    "shixiseng.com": "实习僧",
    "zhaopin.com": "智联招聘",
    "51job.com": "前程无忧",
}
PC_MESSAGE_AUTOMATION_PLATFORMS = {"Boss 直聘", "猎聘", "实习僧"}


def open_message_patrol_browser(start_url: str = "") -> str:
    target_url = normalize_start_url(start_url)
    edge_path = find_edge_executable()
    if not edge_path:
        raise ValueError("未找到 Microsoft Edge。")

    if is_debug_endpoint_ready():
        if target_url != "about:blank" and not open_url_in_debug_browser(target_url):
            raise ValueError("无法在受控 Edge 中打开起始页面，请确认 9222 调试端口仍可用。")
        return target_url

    user_data_dir = browser_profile_dir("manual-msedge")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    launch_args = [
        str(edge_path),
        f"--remote-debugging-port={EDGE_DEBUG_PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--new-window",
        "about:blank",
    ]
    subprocess.Popen(
        launch_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    if not wait_for_debug_endpoint():
        raise ValueError("Edge 已尝试打开，但 9222 调试端口没有响应。请关闭刚打开的专用 Edge 窗口后再试。")
    if target_url != "about:blank" and not open_url_in_debug_browser(target_url):
        raise ValueError("受控 Edge 已启动，但无法打开起始页面。请稍后重试。")
    return target_url


def normalize_start_url(value: str = "") -> str:
    raw = (value or "").strip()
    if not raw:
        return "about:blank"
    return ensure_public_http_url(raw)


def capture_browser_patrol_observations(
    browser_channel: str = "msedge",
    limit: int = 20,
) -> list[dict[str, str]]:
    channel = normalize_browser_channel(browser_channel)
    if channel != "msedge":
        raise ValueError("当前巡检执行器先支持 Microsoft Edge。")
    try:
        if not wait_for_debug_endpoint(timeout_seconds=3):
            raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先点击“打开 Edge 巡检窗口”。")
        observations: list[dict[str, str]] = []
        for target in read_controlled_edge_targets():
            url = target_url(target)
            if not url.startswith(("http://", "https://")):
                continue
            if infer_recruitment_platform(url) not in PC_MESSAGE_AUTOMATION_PLATFORMS:
                continue
            try:
                snapshot = evaluate_cdp_expression(target, browser_patrol_snapshot_expression())
            except Exception:
                continue
            observation = capture_snapshot_observation(snapshot)
            if observation:
                observations.append(observation)
            if len(observations) >= limit:
                break
        return observations
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法连接当前 Edge 巡检页面：{message[:160]}。请先点击“打开 Edge 巡检窗口”，完成登录并打开 HR 对话页。") from exc


def capture_page_observation(page) -> dict[str, str] | None:
    raw_url = str(page.url or "")
    title = safe_page_title(page)
    panel_text = safe_conversation_panel_text(page)
    body_text = ""
    try:
        url = ensure_public_http_url(raw_url)
    except ValueError:
        return None
    url_or_title_matches = looks_like_conversation_url(url) or looks_like_conversation_title(title)
    if not panel_text and (url_or_title_matches or needs_text_probe(url)):
        body_text = safe_body_text(page)
    return capture_observation_from_content(raw_url, title, panel_text, body_text)


def capture_snapshot_observation(snapshot: object) -> dict[str, str] | None:
    if not isinstance(snapshot, dict):
        return None
    return capture_observation_from_content(
        str(snapshot.get("url") or ""),
        str(snapshot.get("title") or ""),
        str(snapshot.get("conversation_text") or ""),
        str(snapshot.get("body_text") or ""),
    )


def capture_observation_from_content(
    raw_url: str,
    title: str,
    conversation_text: str = "",
    body_text: str = "",
) -> dict[str, str] | None:
    try:
        url = ensure_public_http_url(raw_url)
    except ValueError:
        return None
    platform = infer_recruitment_platform(url)
    if not platform or platform not in PC_MESSAGE_AUTOMATION_PLATFORMS:
        return None

    title = title.strip()[:300]
    url_or_title_matches = looks_like_conversation_url(url) or looks_like_conversation_title(title)
    text = conversation_text
    text_scope = "conversation_panel" if text else ""
    if not text and (url_or_title_matches or needs_text_probe(url)):
        if body_text and not is_broad_recruitment_page(url, title):
            text = body_text
            text_scope = "page_body"
    if not text:
        return None
    if text_scope != "conversation_panel" and not url_or_title_matches and not looks_like_conversation_text(text):
        return None
    return {
        "url": url,
        "title": title,
        "platform": platform,
        "text": text[:20000],
        "text_scope": text_scope,
    }


def browser_patrol_snapshot_expression() -> str:
    return f"""(() => {{
        const conversationText = ({CONVERSATION_PANEL_SCRIPT.strip()})();
        return {{
            url: location.href,
            title: document.title || '',
            conversation_text: conversationText || '',
            body_text: document.body ? document.body.innerText || '' : ''
        }};
    }})()"""


def safe_page_title(page) -> str:
    try:
        return str(page.title() or "").strip()[:300]
    except Exception:
        return ""


def safe_body_text(page) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=5000) or "")
    except Exception:
        return ""


def safe_conversation_panel_text(page) -> str:
    try:
        return str(page.evaluate(CONVERSATION_PANEL_SCRIPT) or "")
    except Exception:
        return ""


def infer_recruitment_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    for domain, platform in RECRUITMENT_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return ""


def looks_like_conversation_url(url: str) -> bool:
    parsed = urlparse(url)
    path_and_query = f"{parsed.path}?{parsed.query}".lower()
    return any(token in path_and_query for token in CHAT_URL_TOKENS)


def looks_like_conversation_title(title: str) -> bool:
    return any(token.lower() in (title or "").lower() for token in CHAT_TITLE_TOKENS)


def looks_like_conversation_text(text: str) -> bool:
    return any(token in (text or "") for token in CHAT_TEXT_TOKENS)


def needs_text_probe(url: str) -> bool:
    return bool(infer_recruitment_platform(url))


def is_broad_recruitment_page(url: str, title: str = "") -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "/").rstrip("/") or "/"
    lowered = f"{path}?{parsed.query}".lower()
    title_text = (title or "").lower()
    if "首页" in (title or "") or "home" in title_text:
        return True
    if path == "/":
        return True
    broad_tokens = [
        "search",
        "jobs",
        "joblist",
        "job-list",
        "web/geek/jobs",
        "interns",
        "position",
    ]
    return any(token in lowered for token in broad_tokens) and not looks_like_conversation_url(url)


CONVERSATION_PANEL_SCRIPT = """
() => {
  const controlTerms = ['请输入文字', '按Enter键发送', '发简历', '交换手机号', '交换微信号', '再考虑一下'];
  const messageTerms = ['HR', '先生', '女士', '您好', '招聘', '投递', '岗位', '职位', '面试'];
  const skipTags = new Set(['HTML', 'BODY', 'SCRIPT', 'STYLE', 'NOSCRIPT']);

  function visible(el) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width >= 240 && rect.height >= 180 && style.visibility !== 'hidden' && style.display !== 'none';
  }

  function classText(el) {
    return String(el.className || '').toLowerCase();
  }

  function isContainerLike(el) {
    if (skipTags.has(el.tagName)) return false;
    const cls = classText(el);
    const role = String(el.getAttribute('role') || '').toLowerCase();
    const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
    return (
      role === 'dialog' ||
      el.getAttribute('aria-modal') === 'true' ||
      cls.includes('chat') ||
      cls.includes('im') ||
      cls.includes('message') ||
      cls.includes('conversation') ||
      cls.includes('communicat') ||
      cls.includes('dialog') ||
      cls.includes('modal') ||
      cls.includes('drawer') ||
      cls.includes('pop') ||
      cls.includes('talk') ||
      aria.includes('chat') ||
      aria.includes('message')
    );
  }

  function score(el, text) {
    const rect = el.getBoundingClientRect();
    const controlHits = controlTerms.filter(term => text.includes(term)).length;
    const messageHits = messageTerms.filter(term => text.includes(term)).length;
    let value = controlHits * 20 + messageHits * 4;
    if (isContainerLike(el)) value += 12;
    if (rect.width <= window.innerWidth * 0.75) value += 6;
    if (rect.height <= window.innerHeight * 0.95) value += 4;
    if (text.length > 120 && text.length < 9000) value += 6;
    if (text.includes('我的沟通') && text.length > 5000) value -= 20;
    return value;
  }

  const seeds = [];
  for (const el of Array.from(document.querySelectorAll('body *'))) {
    const text = (el.innerText || '').trim();
    if (!text || text.length < 20) continue;
    if (!visible(el)) continue;
    if (isContainerLike(el) || controlTerms.some(term => text.includes(term))) {
      seeds.push(el);
    }
  }

  const candidates = new Map();
  for (const seed of seeds) {
    let current = seed;
    for (let depth = 0; current && current !== document.body && depth < 6; depth += 1) {
      const text = (current.innerText || '').trim();
      if (text.length >= 40 && text.length <= 10000 && visible(current)) {
        candidates.set(current, text);
      }
      current = current.parentElement;
    }
  }

  let bestText = '';
  let bestScore = 0;
  for (const [el, text] of candidates.entries()) {
    const currentScore = score(el, text);
    if (currentScore > bestScore) {
      bestScore = currentScore;
      bestText = text;
    }
  }
  return bestScore >= 24 ? bestText : '';
}
"""
