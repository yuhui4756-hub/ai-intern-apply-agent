from __future__ import annotations

import base64
import hashlib
from math import sqrt
from urllib.parse import urlparse

from .job_searcher import (
    read_controlled_edge_targets,
    send_cdp_command,
    target_url,
    wait_for_debug_endpoint,
)


VISUAL_RECRUITMENT_DOMAINS = {
    "zhipin.com": "Boss 直聘",
    "liepin.com": "猎聘",
    "shixiseng.com": "实习僧",
    "zhaopin.com": "智联招聘",
    "51job.com": "前程无忧",
}
CHAT_PATH_TOKENS = ("chat", "message", "conversation", "communicate", "talk", "im")
MAX_FULL_PAGE_PIXELS = 7_500_000


def capture_controlled_edge_visual_page(
    mode: str = "viewport",
    *,
    expected_url: str = "",
    platform: str = "",
) -> dict[str, object]:
    """Capture one controlled recruitment page without writing the image to disk."""
    if mode not in {"viewport", "full_page"}:
        raise ValueError("截图模式无效。")
    if not wait_for_debug_endpoint(timeout_seconds=3):
        raise ValueError("没有检测到应用打开的 Edge 调试窗口，请先打开受控 Edge。")
    targets = [target for target in read_controlled_edge_targets() if visual_page_target(target)]
    if expected_url:
        targets = [target for target in targets if target_matches_expected_url(target_url(target), expected_url)]
    elif platform:
        targets = [target for target in targets if platform_for_url(target_url(target)) == platform]
    if not targets:
        raise ValueError("受控 Edge 中没有匹配当前搜索任务的可视觉复核页面。消息页请继续使用未读扫描或当前对话采集。")
    if len(targets) != 1:
        raise ValueError("受控 Edge 中检测到多个招聘岗位或搜索页面。请暂时只保留需要复核的一个页面后重试。")

    target = targets[0]
    params: dict[str, object] = {"format": "jpeg", "quality": 78, "fromSurface": True}
    capture_metadata: dict[str, object] = {"mode": mode, "scaled": False}
    if mode == "full_page":
        layout = send_cdp_command(target, "Page.getLayoutMetrics", timeout_seconds=8)
        content_size = layout.get("contentSize") if isinstance(layout.get("contentSize"), dict) else {}
        width = max(1, int(float(content_size.get("width") or 1)))
        height = max(1, int(float(content_size.get("height") or 1)))
        scale = min(1.0, sqrt(MAX_FULL_PAGE_PIXELS / float(width * height)))
        params["captureBeyondViewport"] = True
        params["clip"] = {"x": 0, "y": 0, "width": width, "height": height, "scale": scale}
        capture_metadata.update({"content_width": width, "content_height": height, "scale": round(scale, 4), "scaled": scale < 1.0})
    else:
        params["captureBeyondViewport"] = False

    result = send_cdp_command(target, "Page.captureScreenshot", params, timeout_seconds=15)
    encoded = str(result.get("data") or "")
    if not encoded:
        raise ValueError("受控 Edge 没有返回页面截图。")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("受控 Edge 返回的截图数据无效。") from exc
    if not raw:
        raise ValueError("受控 Edge 返回了空截图。")
    capture_metadata.update(
        {
            "platform": platform_for_url(target_url(target)),
            "image_bytes": len(raw),
            "image_sha256": hashlib.sha256(raw).hexdigest(),
            "image_persisted": False,
            "page_text_saved": False,
        }
    )
    return {"image_data_url": f"data:image/jpeg;base64,{encoded}", "metadata": capture_metadata}


def visual_page_target(target: object) -> bool:
    url = target_url(target)
    if not url.startswith(("http://", "https://")) or not platform_for_url(url):
        return False
    path = (urlparse(url).path or "").lower()
    return not any(token in path for token in CHAT_PATH_TOKENS)


def platform_for_url(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    for domain, platform in VISUAL_RECRUITMENT_DOMAINS.items():
        if host == domain or host.endswith(f".{domain}"):
            return platform
    return ""


def target_matches_expected_url(url: str, expected_url: str) -> bool:
    current = urlparse(url or "")
    expected = urlparse(expected_url or "")
    if not current.hostname or not expected.hostname:
        return False
    if current.hostname.lower() != expected.hostname.lower():
        return False
    if (current.path or "").rstrip("/") != (expected.path or "").rstrip("/"):
        return False
    return current.query == expected.query
