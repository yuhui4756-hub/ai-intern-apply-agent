from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urljoin, urlparse

from ..config import data_dir
from .job_fetcher import ensure_public_http_url, normalize_browser_channel, normalize_text, normalize_visible_text


JOB_KEYWORDS = ["AI", "Agent", "大模型", "RAG", "Python", "开发", "后端", "实习", "算法", "LLM"]
SKIP_URL_PARTS = ["login", "passport", "signup", "register", "javascript:", "mailto:", "tel:"]
CITY_CODES = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "重庆": "101040100",
    "成都": "101270100",
}


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
            anchors = page.locator("a").evaluate_all(
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
        if not href or href in seen_urls or not looks_like_job_link(href):
            continue
        text = normalize_visible_text(str(anchor.get("text") or anchor.get("title") or ""))
        context = normalize_visible_text(str(anchor.get("context") or ""))
        combined = "\n".join(item for item in [text, context] if item)
        if not has_job_signal(combined, href):
            continue
        title = infer_title(combined) or "候选岗位"
        company = infer_company(combined)
        candidates.append(
            SearchCandidate(
                title=title[:120],
                company=company[:80],
                city=city,
                source_url=href,
                summary=combined[:300],
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


def looks_like_job_link(href: str) -> bool:
    lowered = href.lower()
    return any(token in lowered for token in ["job", "jobs", "job_detail", "intern", "zhaopin", "zhiwei", "zw", "position"])


def has_job_signal(text: str, href: str) -> bool:
    if any(keyword.lower() in text.lower() for keyword in JOB_KEYWORDS):
        return True
    parsed = urlparse(href)
    return any(keyword in parsed.path.lower() for keyword in ["job", "intern", "position"])


def infer_title(text: str) -> str:
    for line in text.splitlines():
        clean = normalize_text(line)
        if 4 <= len(clean) <= 80 and any(keyword.lower() in clean.lower() for keyword in JOB_KEYWORDS):
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
            return normalize_text(match.group(1)).strip(" -:：,，。；;")
    for line in text.splitlines()[1:6]:
        clean = normalize_text(line)
        if 2 <= len(clean) <= 40 and not any(keyword.lower() in clean.lower() for keyword in JOB_KEYWORDS):
            return clean
    return ""
