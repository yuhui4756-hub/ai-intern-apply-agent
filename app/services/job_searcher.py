from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse

from ..config import data_dir
from .analyzer import extract_salary
from .job_fetcher import (
    FetchResult,
    ensure_public_http_url,
    normalize_browser_channel,
    normalize_text,
    normalize_visible_text,
    validate_fetched_text,
)


JOB_KEYWORDS = ["AI", "Agent", "大模型", "RAG", "Python", "开发", "后端", "实习", "算法", "LLM"]
SKIP_URL_PARTS = ["login", "passport", "signup", "register", "javascript:", "mailto:", "tel:"]
DEGREE_WORDS = ["本科", "大专", "硕士", "博士", "学历不限", "不限学历"]
TIME_WORDS = ["天/周", "周", "个月", "月", "长期", "实习"]
COMPANY_HINTS = ["公司", "科技", "智能", "网络", "信息", "软件", "数据", "集团", "字节", "华为", "腾讯", "阿里", "百度"]
CITY_WORDS = ["北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京", "苏州", "厦门", "武汉", "长沙"]
MASKED_SALARY_RE = re.compile(r"[□�]{2,}\s*(?:-|~|～|至|到|—|–|－)\s*[□�]{2,}\s*元\s*/?\s*[天日]")
CITY_CODES = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "重庆": "101040100",
    "成都": "101270100",
}
EDGE_DEBUG_PORT = 9222


@dataclass
class SearchCandidate:
    title: str
    company: str
    city: str
    source_url: str
    summary: str


@dataclass
class SearchResult:
    platform: str
    keyword: str
    city: str
    search_url: str
    browser_channel: str
    candidates: list[SearchCandidate]
    note: str = ""
    retry_count: int = 0


def build_search_url(platform: str, keyword: str, city: str = "") -> str:
    query = quote_plus((keyword or "").strip())
    city_text = (city or "").strip()
    platform_key = (platform or "").strip().lower()
    if platform_key in {"boss", "boss直聘", "zhipin", "boss 直聘"}:
        city_code = CITY_CODES.get(city_text, "100010000")
        return f"https://www.zhipin.com/web/geek/job?query={query}&city={city_code}"
    if platform_key in {"实习僧", "shixiseng"}:
        city_part = quote_plus(city_text)
        return f"https://www.shixiseng.com/interns?keyword={query}&city={city_part}"
    if platform_key in {"猎聘", "liepin"}:
        return f"https://www.liepin.com/zhaopin/?key={query}&city={quote_plus(city_text)}"
    if platform_key in {"智联招聘", "zhaopin"}:
        return f"https://sou.zhaopin.com/?kw={query}&jl={quote_plus(city_text)}"
    if platform_key in {"前程无忧", "51job"}:
        return f"https://we.51job.com/pc/search?keyword={query}&searchType=2&jobArea={quote_plus(city_text)}"
    return f"https://www.bing.com/search?q={quote_plus((platform + ' ' + keyword + ' ' + city_text).strip())}"


def search_jobs_with_browser(
    platform: str,
    keyword: str,
    city: str = "",
    browser_channel: str = "msedge",
    limit: int = 30,
) -> SearchResult:
    search_url = ensure_public_http_url(build_search_url(platform, keyword, city))
    channel = normalize_browser_channel(browser_channel)
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("岗位搜索采集需要安装 Playwright：pip install playwright。") from exc

    context = None
    try:
        with sync_playwright() as playwright:
            user_data_dir = data_dir() / "browser" / channel
            launch_kwargs = {
                "user_data_dir": str(user_data_dir),
                "headless": False,
                "viewport": {"width": 1366, "height": 900},
                "user_agent": "Mozilla/5.0 (compatible; AIInternApplyAgent/0.1; +https://github.com/yuhui4756-hub/ai-intern-apply-agent)",
            }
            if channel == "msedge":
                launch_kwargs["channel"] = "msedge"
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(3000)
            final_url = ensure_public_http_url(page.url)
            anchors = extract_anchor_dicts_from_page(page)
            context.close()
            context = None
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc)
        if "Executable doesn't exist" in message or ("channel" in message and "msedge" in message):
            raise ValueError("未找到 Microsoft Edge，请确认已安装 Edge，或后续改选 Chromium。") from exc
        raise ValueError(f"岗位搜索采集失败：{message[:180]}") from exc
    finally:
        if context is not None:
            context.close()

    candidates = extract_candidates_from_anchors(anchors, platform, city, final_url, limit=limit)
    note = "" if candidates else "没有从当前页面识别到岗位候选，可能需要登录、调整筛选条件或手动打开搜索结果。"
    return SearchResult(platform=platform, keyword=keyword, city=city, search_url=final_url, browser_channel=channel, candidates=candidates, note=note)


def open_manual_search_in_edge(platform: str, keyword: str, city: str = "") -> str:
    search_url = ensure_public_http_url(build_search_url(platform, keyword, city))
    edge_path = find_edge_executable()
    if not edge_path:
        raise ValueError("未找到 Microsoft Edge。")

    if is_debug_endpoint_ready():
        if not open_url_in_debug_browser(search_url):
            raise ValueError("无法在受控 Edge 中打开搜索页，请确认 9222 调试端口仍可用。")
        return search_url

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
    if not open_url_in_debug_browser(search_url):
        raise ValueError("受控 Edge 已启动，但无法打开搜索页。请稍后重试。")
    return search_url


def capture_current_search_page(
    platform: str,
    keyword: str,
    city: str = "",
    browser_channel: str = "msedge",
    limit: int = 30,
    expected_url: str = "",
) -> SearchResult:
    channel = normalize_browser_channel(browser_channel)
    if channel != "msedge":
        raise ValueError("当前页面采集先支持 Microsoft Edge。")
    try:
        (final_url, anchors), retry_count = retry_controlled_edge_read(
            lambda: _capture_current_search_page_once(platform, expected_url)
        )
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法连接当前 Edge 页面：{message[:160]}。请先点击“打开 Edge 搜索页”，完成登录或筛选后再采集。") from exc

    candidates = extract_candidates_from_anchors(anchors, platform, city, final_url, limit=limit)
    note = "" if candidates else "没有从当前页面识别到岗位候选，可尝试打开搜索结果页或岗位列表页后再采集。"
    if retry_count:
        note = append_controlled_edge_retry_note(note, retry_count)
    return SearchResult(
        platform=platform,
        keyword=keyword,
        city=city,
        search_url=final_url,
        browser_channel=channel,
        candidates=candidates,
        note=note,
        retry_count=retry_count,
    )


def _capture_current_search_page_once(platform: str, expected_url: str) -> tuple[str, list[dict]]:
    if not wait_for_debug_endpoint(timeout_seconds=3):
        raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先点击“打开 Edge 搜索页”。")
    target = wait_for_controlled_search_page(platform, expected_url=expected_url)
    time.sleep(1.0)
    snapshot = evaluate_cdp_expression(target, controlled_search_snapshot_expression())
    if not isinstance(snapshot, dict):
        raise ValueError("受控 Edge 搜索页没有返回可读取的页面内容。")
    final_url = ensure_public_http_url(str(snapshot.get("url") or target_url(target)))
    anchors = snapshot.get("anchors")
    if not isinstance(anchors, list):
        raise ValueError("受控 Edge 搜索页没有返回岗位链接。")
    return final_url, [anchor for anchor in anchors if isinstance(anchor, dict)]


def search_jobs_in_controlled_edge(
    platform: str,
    keyword: str,
    city: str = "",
    limit: int = 30,
) -> SearchResult:
    """Open a search in the shared controlled Edge profile and capture its current results."""
    search_url = open_manual_search_in_edge(platform, keyword, city)
    return capture_current_search_page(
        platform,
        keyword,
        city,
        browser_channel="msedge",
        limit=limit,
        expected_url=search_url,
    )


def fetch_job_from_controlled_edge(url: str) -> FetchResult:
    """Read one job-detail page through the controlled Edge login session without submitting anything."""
    safe_url = ensure_public_http_url(url)
    try:
        (final_url, title, text), retry_count = retry_controlled_edge_read(
            lambda: _fetch_job_from_controlled_edge_once(safe_url)
        )
    except ValueError:
        raise
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise ValueError(f"无法通过受控 Edge 抓取岗位详情：{message[:180]}。") from exc

    return FetchResult(
        url=safe_url,
        final_url=final_url,
        title=title,
        text=validate_fetched_text(text),
        fetch_mode="controlled_edge",
        note=append_controlled_edge_retry_note("", retry_count) if retry_count else "",
        retry_count=retry_count,
    )


def _fetch_job_from_controlled_edge_once(safe_url: str) -> tuple[str, str, str]:
    if not wait_for_debug_endpoint(timeout_seconds=3):
        raise ValueError("没有检测到受控 Edge，请先启动岗位发现或自主沟通。")
    target = create_controlled_edge_target(safe_url)
    try:
        wait_for_cdp_document_ready(target)
        time.sleep(0.8)
        snapshot = evaluate_cdp_expression(target, controlled_job_snapshot_expression())
        if not isinstance(snapshot, dict):
            raise ValueError("受控 Edge 岗位详情页没有返回可读取的页面内容。")
        return (
            ensure_public_http_url(str(snapshot.get("url") or safe_url)),
            normalize_text(str(snapshot.get("title") or ""))[:120],
            str(snapshot.get("text") or ""),
        )
    finally:
        close_controlled_edge_target(target)


def retry_controlled_edge_read(operation) -> tuple[object, int]:
    """Retry one transient, read-only CDP operation without touching platform actions."""
    for attempt in range(2):
        try:
            return operation(), attempt
        except ValueError:
            raise
        except Exception as exc:
            if attempt or not is_transient_controlled_edge_error(exc):
                raise
            time.sleep(0.4)
    raise RuntimeError("受控 Edge 读取重试未能完成。")


def is_transient_controlled_edge_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        signal in message
        for signal in (
            "event loop is closed",
            "playwright already stopped",
            "is playwright already stopped",
            "connection refused",
            "econnrefused",
            "connection reset",
            "websocket error",
            "cdp session closed",
            "target page, context or browser has been closed",
        )
    )


def append_controlled_edge_retry_note(note: str, retry_count: int) -> str:
    retry_note = f"受控 Edge 连接短暂中断后已自动重试 {retry_count} 次并成功。"
    return f"{note} {retry_note}".strip()


def browser_profile_dir(name: str) -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "AIInternApplyAgent" / "browser" / name
    return data_dir() / "browser" / name


def debug_endpoint_url(path: str = "/json/version") -> str:
    return f"http://127.0.0.1:{EDGE_DEBUG_PORT}{path}"


def controlled_edge_status() -> dict[str, object]:
    """Inspect only local CDP metadata; never read page text or browser session data."""
    if not find_edge_executable():
        return {
            "status": "未检测到 Edge",
            "edge_available": False,
            "connected": False,
            "page_count": 0,
            "platforms": [],
            "note": "未检测到 Microsoft Edge，无法启动受控岗位发现。",
        }

    try:
        with urllib.request.urlopen(debug_endpoint_url("/json/list"), timeout=0.8) as response:
            targets = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "未连接",
            "edge_available": True,
            "connected": False,
            "page_count": 0,
            "platforms": [],
            "note": "未检测到应用启动的受控 Edge。普通 Edge 不会被读取；开始岗位发现时会打开专用窗口。",
        }

    pages = [target for target in targets if isinstance(target, dict) and target.get("type") == "page"]
    platforms: list[str] = []
    for target in pages:
        platform = platform_name_from_url(str(target.get("url") or ""))
        if platform and platform not in platforms:
            platforms.append(platform)
    note = f"受控 Edge 已连接，检测到 {len(pages)} 个页面。"
    if platforms:
        note += f" 当前招聘平台：{'、'.join(platforms)}。"
    else:
        note += " 尚未检测到招聘平台页面。"
    return {
        "status": "已连接",
        "edge_available": True,
        "connected": True,
        "page_count": len(pages),
        "platforms": platforms,
        "note": note,
    }


def platform_name_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "zhipin.com" in host:
        return "Boss 直聘"
    if "liepin.com" in host:
        return "猎聘"
    if "shixiseng.com" in host:
        return "实习僧"
    if "zhaopin.com" in host:
        return "智联招聘"
    if "51job.com" in host:
        return "前程无忧"
    return ""


def is_debug_endpoint_ready(timeout_seconds: float = 1) -> bool:
    try:
        with urllib.request.urlopen(debug_endpoint_url(), timeout=timeout_seconds) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def wait_for_debug_endpoint(timeout_seconds: float = 8) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_debug_endpoint_ready(timeout_seconds=0.5):
            return True
        time.sleep(0.25)
    return False


def open_url_in_debug_browser(url: str) -> bool:
    try:
        create_controlled_edge_target(url)
        return True
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return False


def read_controlled_edge_targets(timeout_seconds: float = 1) -> list[dict]:
    try:
        with urllib.request.urlopen(debug_endpoint_url("/json/list"), timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取受控 Edge 页面列表。") from exc
    if not isinstance(payload, list):
        raise ValueError("受控 Edge 页面列表格式无效。")
    return [target for target in payload if isinstance(target, dict) and target.get("type") == "page"]


def create_controlled_edge_target(url: str) -> dict:
    safe_url = ensure_public_http_url(url)
    request = urllib.request.Request(debug_endpoint_url(f"/json/new?{quote_plus(safe_url)}"), method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("无法在受控 Edge 中打开页面。") from exc
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("webSocketDebuggerUrl"):
        raise ValueError("受控 Edge 没有返回新页面的调试地址。")
    return payload


def close_controlled_edge_target(target: dict) -> bool:
    target_id = str(target.get("id") or "")
    if not target_id:
        return False
    request = urllib.request.Request(debug_endpoint_url(f"/json/close/{quote_plus(target_id)}"), method="PUT")
    try:
        urllib.request.urlopen(request, timeout=2).close()
        return True
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def evaluate_cdp_expression(target: dict, expression: str, timeout_seconds: float = 8):
    websocket_url = str(target.get("webSocketDebuggerUrl") or "")
    if not websocket_url:
        raise ValueError("受控 Edge 页面缺少调试地址。")
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise ValueError("受控 Edge 读取需要安装 websockets：pip install websockets。") from exc

    request_id = 1
    request = {
        "id": request_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    }
    try:
        with connect(websocket_url, open_timeout=timeout_seconds, close_timeout=1) as websocket:
            websocket.send(json.dumps(request))
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                message = websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
                response = json.loads(message)
                if response.get("id") != request_id:
                    continue
                if response.get("error"):
                    raise RuntimeError(str(response["error"].get("message") or "CDP 请求失败"))
                result = response.get("result") if isinstance(response.get("result"), dict) else {}
                if result.get("exceptionDetails"):
                    details = result["exceptionDetails"]
                    raise RuntimeError(str(details.get("text") or "页面脚本执行失败"))
                remote = result.get("result") if isinstance(result.get("result"), dict) else {}
                return remote.get("value")
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"受控 Edge CDP 读取失败：{exc}") from exc
    raise RuntimeError("受控 Edge CDP 读取超时。")


def controlled_edge_dom_snapshot_expression(selectors: list[str], text_limit: int = 20000) -> str:
    selector_payload = json.dumps(selectors, ensure_ascii=False)
    limit = max(0, min(int(text_limit), 50000))
    return f"""(() => {{
        const selectors = {selector_payload};
        const textSelector = /^(.*):has-text\\((['\"])(.*?)\\2\\)$/;
        const visible = element => {{
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
        }};
        const countSelector = selector => {{
            const match = selector.match(textSelector);
            const baseSelector = match ? match[1] : selector;
            const text = match ? match[3] : '';
            try {{
                return Array.from(document.querySelectorAll(baseSelector))
                    .filter(element => visible(element) && (!text || (element.innerText || element.textContent || '').includes(text)))
                    .slice(0, 20).length;
            }} catch (_error) {{
                return 0;
            }}
        }};
        return {{
            url: location.href,
            title: document.title || '',
            text: document.body ? (document.body.innerText || '').slice(0, {limit}) : '',
            selectors: Object.fromEntries(selectors.map(selector => [selector, countSelector(selector)]))
        }};
    }})()"""


def wait_for_cdp_document_ready(target: dict, timeout_seconds: float = 20) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        ready_state = evaluate_cdp_expression(target, "document.readyState", timeout_seconds=4)
        if ready_state in {"interactive", "complete"}:
            return
        time.sleep(0.25)
    raise ValueError("等待岗位详情页加载超时。")


def target_url(page: object) -> str:
    if isinstance(page, dict):
        return str(page.get("url") or "")
    return str(getattr(page, "url", "") or "")


def wait_for_controlled_search_page(platform: str, expected_url: str = "", timeout_seconds: float = 8) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        target = find_controlled_search_page(read_controlled_edge_targets(), platform, expected_url=expected_url)
        if isinstance(target, dict):
            return target
        time.sleep(0.25)
    platform_label = (platform or "招聘平台").strip()
    raise ValueError(f"等待 {platform_label} 搜索结果页超时。请确认受控 Edge 已打开对应搜索页后重试。")


def pick_search_page(pages: list, platform: str, expected_url: str = ""):
    return find_controlled_search_page(pages, platform, expected_url=expected_url)


def find_controlled_search_page(pages: list, platform: str, expected_url: str = ""):
    expected = urlparse(expected_url)
    expected_host = (expected.hostname or "").lower()
    expected_path = (expected.path or "").rstrip("/")
    if expected_host:
        for page in reversed(pages):
            current_url = target_url(page)
            parsed = urlparse(current_url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").rstrip("/")
            if host == expected_host and (path == expected_path or is_platform_search_page_url(current_url, platform)):
                return page
    signals = platform_url_signals(platform)
    for page in reversed(pages):
        current_url = target_url(page)
        lowered = current_url.lower()
        if any(signal in lowered for signal in signals) and is_platform_search_page_url(current_url, platform):
            return page
    return None


def is_platform_search_page_url(url: str, platform: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower().rstrip("/")
    platform_key = (platform or "").strip().lower()
    if platform_key in {"boss", "boss直聘", "zhipin", "boss 直聘"}:
        return "zhipin.com" in host and path in {"/web/geek/job", "/web/geek/jobs"}
    if platform_key in {"liepin", "猎聘"}:
        return "liepin.com" in host and path.startswith("/zhaopin")
    if platform_key in {"shixiseng", "实习僧"}:
        return "shixiseng.com" in host and path.startswith("/interns")
    if platform_key in {"zhaopin", "智联招聘"}:
        return "zhaopin.com" in host and (host.startswith("sou.") or "search" in path)
    if platform_key in {"51job", "前程无忧"}:
        return "51job.com" in host and path.startswith("/pc/search")
    return bool(url and not url.startswith(("about:", "edge:")))


def platform_url_signals(platform: str) -> list[str]:
    platform_key = (platform or "").strip().lower()
    if platform_key in {"boss", "boss直聘", "zhipin", "boss 直聘"}:
        return ["zhipin.com"]
    if platform_key in {"实习僧", "shixiseng"}:
        return ["shixiseng.com"]
    if platform_key in {"猎聘", "liepin"}:
        return ["liepin.com"]
    if platform_key in {"智联招聘", "zhaopin"}:
        return ["zhaopin.com"]
    if platform_key in {"前程无忧", "51job"}:
        return ["51job.com"]
    return []


def controlled_search_snapshot_expression() -> str:
    return """(() => ({
        url: location.href,
        anchors: Array.from(document.querySelectorAll('a')).slice(0, 400).map(a => {
            const container = a.closest('[class*="job-card"], [class*="jobCard"], [class*="job-item"], [class*="jobItem"], li, article, section') || a.parentElement;
            return {
                href: a.href || '',
                text: a.innerText || a.textContent || '',
                title: a.title || '',
                context: container ? (container.innerText || '') : ''
            };
        })
    }))()"""


def controlled_job_snapshot_expression() -> str:
    return """(() => ({
        url: location.href,
        title: document.title || '',
        text: document.body ? document.body.innerText || '' : ''
    }))()"""


def extract_anchor_dicts_from_page(page) -> list[dict]:
    return page.locator("a").evaluate_all(
        """els => els.slice(0, 400).map(a => {
            const container = a.closest('li, article, section, div');
            return {
                href: a.href || '',
                text: a.innerText || a.textContent || '',
                title: a.title || '',
                context: container ? (container.innerText || '') : ''
            };
        })"""
    )


def find_edge_executable() -> Path | None:
    candidates = [
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    ]
    local_app_data = Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    candidates.append(local_app_data)
    for path in candidates:
        if path.exists():
            return path
    return None


def extract_candidates_from_anchors(
    anchors: list[dict],
    platform: str,
    city: str = "",
    base_url: str = "",
    limit: int = 30,
) -> list[SearchCandidate]:
    candidates: list[SearchCandidate] = []
    seen_urls: set[str] = set()
    for anchor in anchors:
        href = normalize_href(str(anchor.get("href") or ""), base_url)
        if (
            not href
            or href in seen_urls
            or looks_like_search_page_url(href)
            or not looks_like_job_link(href, platform)
        ):
            continue
        text = normalize_visible_text(str(anchor.get("text") or anchor.get("title") or ""))
        context = normalize_visible_text(str(anchor.get("context") or ""))
        # Search pages often put global navigation text in a broad ancestor.  The
        # direct link must therefore look like a job before its card context is used.
        if not has_job_signal(text, href, platform):
            continue
        combined = "\n".join(item for item in [text, context] if item)
        title = infer_direct_title(text) or infer_title(context) or "候选岗位"
        company = infer_company(combined)
        summary = build_candidate_summary(combined, title, company)
        candidates.append(
            SearchCandidate(
                title=title[:120],
                company=company[:80],
                city=city,
                source_url=href,
                summary=summary,
            )
        )
        seen_urls.add(href)
        if len(candidates) >= limit:
            break
    return candidates


def normalize_href(href: str, base_url: str = "") -> str:
    raw = (href or "").strip()
    if not raw or any(part in raw.lower() for part in SKIP_URL_PARTS):
        return ""
    if base_url and raw.startswith("/"):
        raw = urljoin(base_url, raw)
    try:
        return ensure_public_http_url(raw)
    except ValueError:
        return ""


def looks_like_job_link(href: str, platform: str = "") -> bool:
    lowered = href.lower()
    parsed = urlparse(href)
    path = (parsed.path or "").lower()
    platform_key = (platform or "").strip().lower()
    if platform_key in {"liepin", "猎聘"}:
        return "/lptjob/" in path
    if platform_key in {"shixiseng", "实习僧"}:
        return bool(re.search(r"/intern/inn_[a-z0-9]+", path))
    if platform_key in {"boss", "boss直聘", "zhipin", "boss 直聘"}:
        return "/job_detail/" in path
    return any(token in lowered for token in ["job", "jobs", "job_detail", "intern", "zhaopin", "zhiwei", "zw", "position"])


def looks_like_search_page_url(href: str) -> bool:
    parsed = urlparse(href)
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower().rstrip("/")
    if "zhipin.com" in host and path == "/web/geek/job":
        return True
    if "zhaopin.com" in host and host.startswith("sou."):
        return True
    if "51job.com" in host and path.startswith("/pc/search"):
        return True
    if "liepin.com" in host and path.startswith("/zhaopin"):
        return True
    if "shixiseng.com" in host and path.startswith("/interns"):
        return True
    return False


def has_job_signal(text: str, href: str, platform: str = "") -> bool:
    if any(keyword.lower() in text.lower() for keyword in JOB_KEYWORDS):
        return True
    return looks_like_job_link(href, platform) and bool(normalize_text(text))


def infer_title(text: str) -> str:
    for line in candidate_lines(text):
        clean = normalize_text(line)
        if 4 <= len(clean) <= 80 and any(keyword.lower() in clean.lower() for keyword in JOB_KEYWORDS):
            return clean
    return ""


def infer_direct_title(text: str) -> str:
    for line in candidate_lines(text):
        clean = normalize_text(line)
        if 2 <= len(clean) <= 80 and not extract_salary(clean) and not any(word in clean for word in TIME_WORDS):
            return clean
    return ""


def infer_company(text: str) -> str:
    patterns = [
        r"(?:公司名称|公司|企业)[:：]\s*([^\n\r，,。；;]{2,40})",
        r"([^\n\r，,。；;]{2,40})(?:招聘|直聘|校招)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = clean_company_name(match.group(1))
            if is_company_candidate(value, prefer_hint=False):
                return value
    lines = candidate_lines(text)
    for line in lines:
        match = re.search(
            r"(?:本科|大专|硕士|博士|学历不限|不限学历)\s+([^\s\n]{2,30}?)(?=\s+(?:北京|上海|广州|深圳|杭州|重庆|成都|南京|苏州|厦门|武汉|长沙|远程)(?:[·\s]|$)|$)",
            normalize_text(line),
        )
        if match:
            value = clean_company_name(match.group(1))
            if is_company_candidate(value, prefer_hint=False):
                return value
    for index, line in enumerate(lines):
        clean = normalize_text(line)
        if any(word in clean for word in DEGREE_WORDS):
            for nearby in lines[index + 1 : index + 3]:
                value = clean_company_name(nearby)
                if is_company_candidate(value, prefer_hint=False):
                    return value
    for line in lines[1:8]:
        value = clean_company_name(line)
        if is_company_candidate(value, prefer_hint=True):
            return value
    for line in lines[1:8]:
        value = clean_company_name(line)
        if is_company_candidate(value, prefer_hint=False):
            return value
    return ""


def candidate_lines(text: str) -> list[str]:
    normalized = normalize_visible_text(sanitize_masked_salary(text))
    lines = [normalize_text(line).strip(" -:：,，。；;") for line in normalized.splitlines()]
    cleaned: list[str] = []
    for line in lines:
        if line and line not in cleaned:
            cleaned.append(line)
    return cleaned


def sanitize_masked_salary(text: str) -> str:
    return MASKED_SALARY_RE.sub("薪资数字未能从页面文本读取", text or "")


def clean_company_name(value: str) -> str:
    return normalize_text(value).strip(" -:：,，。；;")


def is_company_candidate(value: str, *, prefer_hint: bool) -> bool:
    text = clean_company_name(value)
    lowered = text.lower()
    if not 2 <= len(text) <= 40:
        return False
    if any(keyword.lower() in lowered for keyword in JOB_KEYWORDS):
        return False
    if extract_salary(text) or "元/天" in text or "薪资" in text or "补贴" in text:
        return False
    if any(word in text for word in TIME_WORDS) and not any(hint in text for hint in COMPANY_HINTS):
        return False
    if text in DEGREE_WORDS or re.fullmatch(r"(?:\d+\s*)?(?:天/周|个月|月|周|本科|大专|硕士|博士)", text):
        return False
    if any(city in text for city in CITY_WORDS) and ("·" in text or "-" in text) and not any(hint in text for hint in COMPANY_HINTS):
        return False
    if prefer_hint and not any(hint in text for hint in COMPANY_HINTS):
        return False
    return True


def build_candidate_summary(text: str, title: str, company: str) -> str:
    lines = candidate_lines(text)
    summary_lines: list[str] = []
    for line in lines:
        if len(line) > 140:
            continue
        if title and line == title and summary_lines:
            continue
        if company and line == company and company in summary_lines:
            continue
        summary_lines.append(line)
        if len(summary_lines) >= 6:
            break
    summary = "\n".join(summary_lines) if summary_lines else sanitize_masked_salary(normalize_visible_text(text))
    return summary[:260]
