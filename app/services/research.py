from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote_plus, urlparse, parse_qs

import httpx


@dataclass
class SearchResult:
    title: str
    url: str
    summary: str


def search_company(company: str, title: str = "", city: str = "", depth: str = "auto") -> list[SearchResult]:
    query = " ".join(part for part in [company, title, city, "公司", "招聘", "风险"] if part).strip()
    if not query:
        return []

    limit = {"quick": 3, "standard": 5, "deep": 8}.get(depth, 5)
    url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
    try:
        with httpx.Client(timeout=12, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
            response = client.get(url)
            response.raise_for_status()
    except Exception:
        return []

    return parse_duckduckgo_html(response.text, limit)


def parse_duckduckgo_html(page: str, limit: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(?P<summary>.*?)</a>',
        re.S,
    )
    for match in pattern.finditer(page):
        href = html.unescape(match.group("href"))
        parsed = urlparse(href)
        if parsed.path == "/l/":
            href = parse_qs(parsed.query).get("uddg", [href])[0]
        title = clean_html(match.group("title"))
        summary = clean_html(match.group("summary"))
        if title and href:
            results.append(SearchResult(title=title, url=href, summary=summary))
        if len(results) >= limit:
            break
    return results


def clean_html(value: str) -> str:
    text = re.sub(r"<.*?>", "", value or "")
    return html.unescape(re.sub(r"\s+", " ", text)).strip()
