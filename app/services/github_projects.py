from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


GITHUB_API = "https://api.github.com"
USER_AGENT = "ai-intern-apply-agent"


class GitHubProjectError(ValueError):
    pass


def normalize_repo_url(url: str) -> str:
    owner_repo = parse_github_repo_url(url)
    if not owner_repo:
        raise GitHubProjectError("请填写具体 GitHub 仓库链接，例如 https://github.com/owner/repo。")
    owner, repo = owner_repo
    return f"https://github.com/{owner}/{repo}"


def parse_github_repo_url(url: str) -> tuple[str, str] | None:
    value = (url or "").strip()
    if not value:
        return None
    if value.startswith("git@github.com:"):
        value = "https://github.com/" + value.removeprefix("git@github.com:").removesuffix(".git")
    parsed = urllib.parse.urlparse(value)
    host = parsed.netloc.lower()
    if host not in {"github.com", "www.github.com"}:
        return None
    segments = [item for item in parsed.path.strip("/").split("/") if item]
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1].removesuffix(".git")
    if owner.lower() in {"topics", "marketplace", "features", "about", "orgs"}:
        return None
    return owner, repo


def repo_key(url: str) -> str:
    owner_repo = parse_github_repo_url(url)
    if not owner_repo:
        return ""
    owner, repo = owner_repo
    return f"{owner.lower()}/{repo.lower()}"


def github_repo_urls_from_projects(projects: list[dict[str, Any]]) -> list[str]:
    urls = []
    for project in projects:
        url = str(project.get("url") or project.get("repo_url") or "").strip()
        if repo_key(url):
            urls.append(normalize_repo_url(url))
    return dedupe_urls(urls)


def dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        key = repo_key(url)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(normalize_repo_url(url))
    return result


def fetch_json(url: str, timeout: int = 12) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        message = exc.reason or f"HTTP {exc.code}"
        raise GitHubProjectError(f"GitHub 请求失败：{message}") from exc
    except urllib.error.URLError as exc:
        raise GitHubProjectError(f"无法连接 GitHub：{exc.reason}") from exc


def fetch_github_repo_snapshot(
    repo_url: str,
    *,
    json_fetcher: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    owner_repo = parse_github_repo_url(repo_url)
    if not owner_repo:
        raise GitHubProjectError("只支持具体 GitHub 仓库链接。")
    owner, repo = owner_repo
    api = json_fetcher or fetch_json
    repo_api = f"{GITHUB_API}/repos/{owner}/{repo}"
    repo_data = api(repo_api)
    languages = api(f"{repo_api}/languages")
    commits = api(f"{repo_api}/commits?per_page=5")
    readme_text = ""
    try:
        readme = api(f"{repo_api}/readme")
        content = str(readme.get("content") or "")
        if content:
            readme_text = base64.b64decode(content.encode("utf-8"), validate=False).decode("utf-8", errors="replace")
    except GitHubProjectError:
        readme_text = ""

    return {
        "name": str(repo_data.get("name") or repo),
        "full_name": str(repo_data.get("full_name") or f"{owner}/{repo}"),
        "url": str(repo_data.get("html_url") or normalize_repo_url(repo_url)),
        "description": str(repo_data.get("description") or ""),
        "primary_language": str(repo_data.get("language") or ""),
        "languages": sorted(languages.keys(), key=lambda key: int(languages.get(key) or 0), reverse=True)[:6]
        if isinstance(languages, dict)
        else [],
        "topics": list(repo_data.get("topics") or [])[:8],
        "stars": int(repo_data.get("stargazers_count") or 0),
        "forks": int(repo_data.get("forks_count") or 0),
        "pushed_at": str(repo_data.get("pushed_at") or ""),
        "readme_excerpt": summarize_readme(readme_text),
        "recent_commits": summarize_commits(commits),
    }


def summarize_readme(text: str, limit: int = 600) -> str:
    lines = []
    for line in (text or "").splitlines():
        cleaned = re.sub(r"\s+", " ", line.strip())
        if not cleaned or cleaned.startswith(("!", "[!", "<!--")):
            continue
        cleaned = cleaned.strip("#`*_ ")
        if cleaned:
            lines.append(cleaned)
        if len(" ".join(lines)) >= limit:
            break
    return " ".join(lines)[:limit]


def summarize_commits(commits: Any) -> list[str]:
    result: list[str] = []
    if not isinstance(commits, list):
        return result
    for item in commits[:5]:
        commit = item.get("commit") if isinstance(item, dict) else {}
        message = str((commit or {}).get("message") or "").splitlines()[0].strip()
        if message:
            result.append(message[:120])
    return result


def project_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    languages = list(snapshot.get("languages") or [])
    highlights = []
    description = str(snapshot.get("description") or "").strip()
    if description:
        highlights.append(description[:160])
    if languages:
        highlights.append("主要语言/技术：" + "、".join(languages[:6]))
    readme_excerpt = str(snapshot.get("readme_excerpt") or "").strip()
    if readme_excerpt:
        highlights.append("README 摘要：" + readme_excerpt[:220])
    commits = list(snapshot.get("recent_commits") or [])
    if commits:
        highlights.append("近期提交：" + "；".join(commits[:3]))
    return {
        "name": str(snapshot.get("name") or snapshot.get("full_name") or "GitHub 项目"),
        "url": str(snapshot.get("url") or ""),
        "source": "github",
        "description": description,
        "languages": languages,
        "topics": list(snapshot.get("topics") or []),
        "highlights": highlights[:5],
        "recent_commits": commits,
        "readme_excerpt": readme_excerpt,
        "updated_from_github_at": str(snapshot.get("pushed_at") or ""),
    }


def merge_project_facts(existing: list[dict[str, Any]], refreshed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for project in existing:
        key = repo_key(str(project.get("url") or "")) or str(project.get("name") or "").strip().lower()
        if not key:
            continue
        by_key[key] = dict(project)
        order.append(key)
    for project in refreshed:
        key = repo_key(str(project.get("url") or "")) or str(project.get("name") or "").strip().lower()
        if not key:
            continue
        if key not in by_key:
            order.append(key)
        merged = {**by_key.get(key, {}), **project}
        by_key[key] = merged
    return [by_key[key] for key in order if key in by_key][:20]
