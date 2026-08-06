from __future__ import annotations

import subprocess
from urllib.parse import urlparse

from .job_fetcher import ensure_public_http_url, normalize_browser_channel
from .job_searcher import (
    EDGE_DEBUG_PORT,
    browser_profile_dir,
    find_edge_executable,
    is_debug_endpoint_ready,
    open_url_in_debug_browser,
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


def open_message_patrol_browser(start_url: str = "") -> str:
    target_url = normalize_start_url(start_url)
    edge_path = find_edge_executable()
    if not edge_path:
        raise ValueError("未找到 Microsoft Edge。")

    if is_debug_endpoint_ready():
        open_url_in_debug_browser(target_url)
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
        target_url,
    ]
    subprocess.Popen(
        launch_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    if not wait_for_debug_endpoint():
        raise ValueError("Edge 已尝试打开，但 9222 调试端口没有响应。请关闭刚打开的专用 Edge 窗口后再试。")
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
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("浏览器巡检需要安装 Playwright：pip install playwright。") from exc

    browser = None
    try:
        with sync_playwright() as playwright:
            if not wait_for_debug_endpoint(timeout_seconds=3):
                raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先点击“打开 Edge 巡检窗口”。")
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{EDGE_DEBUG_PORT}", timeout=8000)
            pages = [page for context in browser.contexts for page in context.pages if page.url and page.url != "about:blank"]
            observations: list[dict[str, str]] = []
            for page in pages:
                observation = capture_page_observation(page)
                if observation:
                    observations.append(observation)
                if len(observations) >= limit:
                    break
            browser.close()
            browser = None
            return observations
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法连接当前 Edge 巡检页面：{message[:160]}。请先点击“打开 Edge 巡检窗口”，完成登录并打开 HR 对话页。") from exc
    finally:
        if browser is not None:
            browser.close()


def capture_page_observation(page) -> dict[str, str] | None:
    raw_url = str(page.url or "")
    try:
        url = ensure_public_http_url(raw_url)
    except ValueError:
        return None
    platform = infer_recruitment_platform(url)
    if not platform:
        return None

    title = safe_page_title(page)
    url_or_title_matches = looks_like_conversation_url(url) or looks_like_conversation_title(title)
    text = ""
    if url_or_title_matches or needs_text_probe(url):
        text = safe_body_text(page)
    if not text:
        return None
    if not url_or_title_matches and not looks_like_conversation_text(text):
        return None
    return {
        "url": url,
        "title": title,
        "platform": platform,
        "text": text[:20000],
    }


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
