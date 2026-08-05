from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx


MAX_HTML_BYTES = 1_200_000
MIN_JD_TEXT_CHARS = 80


@dataclass
class FetchResult:
    url: str
    final_url: str
    title: str
    text: str


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.ignored_stack: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg", "canvas", "head"}:
            self.ignored_stack.append(lowered)
        if lowered == "title":
            self.in_title = True
        if lowered in {"p", "div", "section", "article", "li", "br", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if self.ignored_stack and self.ignored_stack[-1] == lowered:
            self.ignored_stack.pop()
        if lowered == "title":
            self.in_title = False
        if lowered in {"p", "div", "section", "article", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.ignored_stack or self.in_title:
            return
        clean = data.strip()
        if clean:
            self.parts.append(clean)

    @property
    def title(self) -> str:
        return normalize_text(" ".join(self.title_parts))[:120]

    @property
    def text(self) -> str:
        return normalize_visible_text("\n".join(self.parts))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value or "")).strip()


def normalize_visible_text(value: str) -> str:
    text = unescape(value or "")
    lines = [normalize_text(line) for line in text.splitlines()]
    useful_lines = [line for line in lines if len(line) >= 2 and not is_navigation_line(line)]
    compact = "\n".join(useful_lines)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()


def is_navigation_line(line: str) -> bool:
    lowered = line.lower()
    if lowered in {"登录", "注册", "首页", "搜索", "职位", "公司", "我的", "app", "下载app"}:
        return True
    if lowered in {"login", "register", "home", "search", "jobs", "companies"}:
        return True
    return len(line) <= 8 and any(token in line for token in ["登录", "注册", "下载", "菜单"])


def ensure_public_http_url(url: str) -> str:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("只支持公开 http/https 岗位链接。")
    hostname = parsed.hostname or ""
    if hostname.lower() in {"localhost"} or hostname.endswith(".local"):
        raise ValueError("不抓取 localhost 或本地域名链接。")
    if is_private_hostname(hostname):
        raise ValueError("不抓取内网、回环或链路本地地址。")
    return parsed.geturl()


def is_private_hostname(hostname: str) -> bool:
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        except socket.gaierror:
            return False
        addresses = []
        for info in infos:
            address = info[4][0]
            try:
                addresses.append(ipaddress.ip_address(address))
            except ValueError:
                continue

    return any(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        for address in addresses
    )


def extract_visible_text(html: str) -> tuple[str, str]:
    parser = VisibleTextParser()
    parser.feed(html)
    parser.close()
    return parser.title, parser.text


def fetch_job_from_url(url: str) -> FetchResult:
    safe_url = ensure_public_http_url(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AIInternApplyAgent/0.1; +https://github.com/yuhui4756-hub/ai-intern-apply-agent)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        response = client.get(safe_url)
        response.raise_for_status()
        final_url = ensure_public_http_url(str(response.url))
        content_type = response.headers.get("content-type", "")
        if content_type and "text/html" not in content_type and "application/xhtml" not in content_type and "text/plain" not in content_type:
            raise ValueError("链接返回的不是可解析网页文本。")
        raw = response.content[:MAX_HTML_BYTES]
        html = raw.decode(response.encoding or "utf-8", errors="replace")

    if "text/plain" in content_type:
        title = ""
        text = normalize_visible_text(html)
    else:
        title, text = extract_visible_text(html)
    if len(text) < MIN_JD_TEXT_CHARS:
        raise ValueError("页面文本太短，可能需要登录、验证码或浏览器渲染。")
    return FetchResult(url=safe_url, final_url=final_url, title=title, text=text[:20000])
