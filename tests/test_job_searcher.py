import pytest

from app.services import job_searcher
from app.services.job_searcher import (
    build_search_url,
    capture_current_search_page,
    controlled_edge_status,
    extract_candidates_from_anchors,
    fetch_job_from_controlled_edge,
    find_controlled_search_page,
    open_manual_search_in_edge,
    pick_search_page,
)


def test_build_search_url_for_boss_city_keyword():
    url = build_search_url("Boss 直聘", "AI Agent 实习", "杭州")

    assert "zhipin.com" in url
    assert "AI+Agent" in url
    assert "101210100" in url


def test_extract_candidates_from_anchor_context():
    anchors = [
        {
            "href": "https://www.example.com/job_detail/123",
            "text": "AI Agent 开发实习生",
            "context": "AI Agent 开发实习生\n杭州搜索智能科技有限公司\nPython RAG FastAPI",
        },
        {
            "href": "https://www.example.com/login",
            "text": "登录",
            "context": "登录",
        },
    ]

    candidates = extract_candidates_from_anchors(anchors, "测试平台", "杭州")

    assert len(candidates) == 1
    assert candidates[0].title == "AI Agent 开发实习生"
    assert candidates[0].company == "杭州搜索智能科技有限公司"


def test_extract_candidates_filters_mixed_card_fields():
    anchors = [
        {
            "href": "https://www.example.com/job_detail/byte-1",
            "text": "AI应用开发实习生-财经业务",
            "context": "\n".join(
                [
                    "AI应用开发实习生-财经业务",
                    "□□□-□□□元/天",
                    "5天/周",
                    "3个月",
                    "本科 北京字节跳动 杭州·余杭区·仓前",
                    "Python RAG FastAPI",
                ]
            ),
        },
        {
            "href": "https://www.example.com/job_detail/qunhe-2",
            "text": "AI应用开发实习生-27届校招实习生",
            "context": "\n".join(
                [
                    "AI应用开发实习生-27届校招实习生",
                    "150-250元/天",
                    "4天/周",
                    "3个月",
                    "本科 群核科技 杭州·拱墅区·文一路",
                ]
            ),
        },
    ]

    candidates = extract_candidates_from_anchors(anchors, "测试平台", "杭州")

    assert len(candidates) == 2
    assert candidates[0].company == "北京字节跳动"
    assert candidates[1].company == "群核科技"
    assert "薪资数字未能从页面文本读取" in candidates[0].summary
    assert "群核科技" in candidates[1].summary
    assert "北京字节跳动" not in candidates[1].summary


def test_extract_candidates_rejects_liepin_career_navigation_with_global_search_context():
    anchors = [
        {
            "href": "https://www.liepin.com/career/qtfawuzw/",
            "text": "其他法务职位招聘",
            "context": "Agent 开发实习\n其他法务职位招聘\n本期新增 2800 个职位",
        },
        {
            "href": "https://www.liepin.com/lptjob/80798955",
            "text": "AI Agent 应用开发工程师【实习+应届】",
            "context": "AI Agent 应用开发工程师【实习+应届】\n彩讯科技股份有限公司\n北京-朝阳区\n20-35k",
        },
    ]

    candidates = extract_candidates_from_anchors(anchors, "猎聘", "北京")

    assert len(candidates) == 1
    assert candidates[0].title == "AI Agent 应用开发工程师【实习+应届】"
    assert candidates[0].company == "彩讯科技股份有限公司"


def test_extract_candidates_keeps_detail_anchor_title_when_context_contains_search_keyword():
    anchors = [
        {
            "href": "https://www.liepin.com/lptjob/12345678",
            "text": "海外专利诉讼专家",
            "context": "Agent 开发实习\n海外专利诉讼专家\n某新能源公司\n上海",
        }
    ]

    candidates = extract_candidates_from_anchors(anchors, "猎聘", "北京")

    assert len(candidates) == 1
    assert candidates[0].title == "海外专利诉讼专家"


def test_pick_search_page_prefers_expected_controlled_search_url():
    class DummyPage:
        def __init__(self, url: str):
            self.url = url

    older_chat = DummyPage("https://www.zhipin.com/web/geek/chat")
    intended_search = DummyPage("https://www.zhipin.com/web/geek/job?query=AI+Agent&city=101210100")
    later_unrelated_page = DummyPage("https://example.com/dashboard")

    selected = pick_search_page(
        [older_chat, intended_search, later_unrelated_page],
        "Boss 直聘",
        expected_url="https://www.zhipin.com/web/geek/job?query=AI+Agent&city=101210100",
    )

    assert selected is intended_search


def test_find_controlled_search_page_accepts_boss_search_redirect_and_rejects_new_tab():
    class DummyPage:
        def __init__(self, url: str):
            self.url = url

    redirected_search = DummyPage("https://www.zhipin.com/web/geek/jobs?query=AI+Agent&city=101210100")
    new_tab = DummyPage("edge://newtab/")

    selected = find_controlled_search_page(
        [new_tab, redirected_search],
        "Boss 直聘",
        expected_url="https://www.zhipin.com/web/geek/job?query=AI+Agent&city=101210100",
    )

    assert selected is redirected_search
    assert find_controlled_search_page([new_tab], "Boss 直聘") is None


def test_find_controlled_search_page_accepts_cdp_target_dict():
    target = {
        "id": "boss-search",
        "type": "page",
        "url": "https://www.zhipin.com/web/geek/jobs?query=AI+Agent&city=101210100",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/boss-search",
    }

    selected = find_controlled_search_page(
        [{"id": "new-tab", "type": "page", "url": "edge://newtab/"}, target],
        "Boss 直聘",
        expected_url="https://www.zhipin.com/web/geek/job?query=AI+Agent&city=101210100",
    )

    assert selected == target


def test_capture_controlled_search_reads_cdp_target_without_playwright(monkeypatch):
    target = {
        "id": "boss-search",
        "type": "page",
        "url": "https://www.zhipin.com/web/geek/jobs?query=AI+Agent",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/boss-search",
    }
    snapshot = {
        "url": target["url"],
        "anchors": [
            {
                "href": "https://www.zhipin.com/job_detail/agent-1.html",
                "text": "AI Agent 开发实习生",
                "context": "AI Agent 开发实习生\n测试智能科技\n杭州\n200-250元/天",
            }
        ],
    }

    monkeypatch.setattr(job_searcher, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(job_searcher, "wait_for_controlled_search_page", lambda *_args, **_kwargs: target)
    monkeypatch.setattr(job_searcher, "evaluate_cdp_expression", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr(job_searcher.time, "sleep", lambda _seconds: None)

    result = capture_current_search_page("Boss 直聘", "AI Agent", "杭州")

    assert result.search_url == target["url"]
    assert result.candidates[0].company == "测试智能科技"


def test_controlled_search_snapshot_prefers_job_card_over_nearest_div():
    expression = job_searcher.controlled_search_snapshot_expression()

    assert "[class*=\"job-card\"]" in expression
    assert "a.parentElement" in expression
    assert "closest('li, article, section, div')" not in expression


def test_fetch_controlled_edge_closes_only_created_detail_target(monkeypatch):
    target = {
        "id": "detail-target",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/agent-1.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/detail-target",
    }
    closed_targets = []

    monkeypatch.setattr(job_searcher, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(job_searcher, "create_controlled_edge_target", lambda _url: target)
    monkeypatch.setattr(job_searcher, "wait_for_cdp_document_ready", lambda _target: None)
    monkeypatch.setattr(
        job_searcher,
        "evaluate_cdp_expression",
        lambda *_args, **_kwargs: {
            "url": target["url"],
            "title": "AI Agent 开发实习生",
            "text": "岗位要求 Python、FastAPI、RAG。" * 5,
        },
    )
    monkeypatch.setattr(job_searcher, "close_controlled_edge_target", lambda value: closed_targets.append(value) or True)
    monkeypatch.setattr(job_searcher.time, "sleep", lambda _seconds: None)

    result = fetch_job_from_controlled_edge(target["url"])

    assert result.fetch_mode == "controlled_edge"
    assert closed_targets == [target]


def test_open_manual_search_opens_target_after_controlled_edge_starts(tmp_path, monkeypatch):
    opened_urls = []
    launch_args = []

    monkeypatch.setattr(job_searcher, "find_edge_executable", lambda: tmp_path / "msedge.exe")
    monkeypatch.setattr(job_searcher, "browser_profile_dir", lambda _name: tmp_path / "profile")
    monkeypatch.setattr(job_searcher, "is_debug_endpoint_ready", lambda: False)
    monkeypatch.setattr(job_searcher, "wait_for_debug_endpoint", lambda: True)
    monkeypatch.setattr(job_searcher, "open_url_in_debug_browser", lambda url: opened_urls.append(url) or True)
    monkeypatch.setattr(job_searcher.subprocess, "Popen", lambda args, **_kwargs: launch_args.extend(args))

    search_url = open_manual_search_in_edge("Boss 直聘", "AI Agent", "杭州")

    assert opened_urls == [search_url]
    assert launch_args[-1] == "about:blank"


def test_capture_current_search_page_retries_transient_playwright_shutdown(monkeypatch):
    calls = []

    def fake_read_once(_platform, _expected_url):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Event loop is closed! Is Playwright already stopped?")
        return (
            "https://www.zhipin.com/web/geek/job?query=AI+Agent",
            [
                {
                    "href": "https://www.zhipin.com/job_detail/retry-1.html",
                    "text": "AI Agent 开发实习生",
                    "context": "AI Agent 开发实习生\n测试智能科技\n杭州\n200-250元/天",
                }
            ],
        )

    monkeypatch.setattr(job_searcher, "_capture_current_search_page_once", fake_read_once)
    monkeypatch.setattr(job_searcher.time, "sleep", lambda _seconds: None)

    result = capture_current_search_page("Boss 直聘", "AI Agent", "杭州")

    assert len(calls) == 2
    assert result.retry_count == 1
    assert "自动重试 1 次" in result.note
    assert result.candidates[0].title == "AI Agent 开发实习生"


def test_fetch_controlled_edge_retries_only_transient_read_failure(monkeypatch):
    calls = []

    def fake_read_once(url):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError("Event loop is closed! Is Playwright already stopped?")
        return (url, "AI Agent 开发实习生", "岗位要求 Python、FastAPI、RAG。" * 5)

    monkeypatch.setattr(job_searcher, "_fetch_job_from_controlled_edge_once", fake_read_once)
    monkeypatch.setattr(job_searcher.time, "sleep", lambda _seconds: None)

    result = fetch_job_from_controlled_edge("https://jobs.example.com/detail/1")

    assert calls == ["https://jobs.example.com/detail/1", "https://jobs.example.com/detail/1"]
    assert result.retry_count == 1
    assert "自动重试 1 次" in result.note


def test_controlled_edge_does_not_retry_non_transient_error(monkeypatch):
    calls = []

    def fake_read_once(_platform, _expected_url):
        calls.append(1)
        raise RuntimeError("招聘平台要求完成验证码")

    monkeypatch.setattr(job_searcher, "_capture_current_search_page_once", fake_read_once)

    with pytest.raises(ValueError, match="招聘平台要求完成验证码"):
        capture_current_search_page("Boss 直聘", "AI Agent", "杭州")

    assert len(calls) == 1


def test_controlled_edge_status_reports_platforms_without_page_content(monkeypatch):
    class DummyResponse:
        def read(self):
            return b'[{"type":"page","url":"https://www.zhipin.com/web/geek/job"},{"type":"page","url":"https://www.liepin.com/zhaopin/"},{"type":"service_worker","url":"https://www.zhipin.com/worker.js"}]'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(job_searcher, "find_edge_executable", lambda: object())
    monkeypatch.setattr(job_searcher.urllib.request, "urlopen", lambda *_args, **_kwargs: DummyResponse())

    status = controlled_edge_status()

    assert status["status"] == "已连接"
    assert status["page_count"] == 2
    assert status["platforms"] == ["Boss 直聘", "猎聘"]
    assert "zhipin.com" not in status["note"]
    assert "liepin.com" not in status["note"]


def test_controlled_edge_status_distinguishes_missing_controlled_window(monkeypatch):
    monkeypatch.setattr(job_searcher, "find_edge_executable", lambda: object())
    monkeypatch.setattr(job_searcher.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("refused")))

    status = controlled_edge_status()

    assert status["status"] == "未连接"
    assert status["connected"] is False
    assert "普通 Edge 不会被读取" in status["note"]
