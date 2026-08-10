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
        open_url_in_debug_browser(search_url)
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
        search_url,
    ]
    subprocess.Popen(
        launch_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    if not wait_for_debug_endpoint():
        raise ValueError("Edge 已尝试打开，但 9222 调试端口没有响应。请关闭刚打开的专用 Edge 窗口后再试。")
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
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("当前页面采集需要安装 Playwright：pip install playwright。") from exc

    browser = None
    with sync_playwright() as playwright:
        try:
            if not wait_for_debug_endpoint(timeout_seconds=3):
                raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先点击“打开 Edge 搜索页”。")
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{EDGE_DEBUG_PORT}", timeout=8000)
            pages = [page for context in browser.contexts for page in context.pages if page.url and page.url != "about:blank"]
            if not pages:
                raise ValueError("没有找到可采集的 Edge 页面，请先从应用打开搜索页。")
            page = pick_search_page(pages, platform, expected_url=expected_url)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(1500)
            return ensure_public_http_url(page.url), extract_anchor_dicts_from_page(page)
        finally:
            close_safely(browser)


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
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ValueError("受控 Edge 抓取需要安装 Playwright：pip install playwright。") from exc

    browser = None
    page = None
    with sync_playwright() as playwright:
        try:
            if not wait_for_debug_endpoint(timeout_seconds=3):
                raise ValueError("没有检测到受控 Edge，请先启动岗位发现或自主沟通。")
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{EDGE_DEBUG_PORT}", timeout=8000)
            if not browser.contexts:
                raise ValueError("受控 Edge 没有可用浏览器上下文。")
            page = browser.contexts[0].new_page()
            page.goto(safe_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1000)
            return (
                ensure_public_http_url(page.url),
                normalize_text(page.title())[:120],
                page.locator("body").inner_text(timeout=8000),
            )
        finally:
            close_safely(page)
            close_safely(browser)


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


def close_safely(resource) -> None:
    if resource is None:
        return
    try:
        resource.close()
    except Exception:
        pass


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


def open_url_in_debug_browser(url: str) -> None:
    request = urllib.request.Request(debug_endpoint_url(f"/json/new?{quote_plus(url)}"), method="PUT")
    try:
        urllib.request.urlopen(request, timeout=2).close()
    except (OSError, urllib.error.URLError, TimeoutError):
        pass


def pick_search_page(pages: list, platform: str, expected_url: str = ""):
    expected = urlparse(expected_url)
    expected_host = (expected.hostname or "").lower()
    expected_path = (expected.path or "").rstrip("/")
    if expected_host:
        for page in reversed(pages):
            parsed = urlparse(page.url or "")
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").rstrip("/")
            if host == expected_host and path == expected_path:
                return page
    signals = platform_url_signals(platform)
    for page in reversed(pages):
        lowered = (page.url or "").lower()
        if any(signal in lowered for signal in signals):
            return page
    return pages[-1]


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
