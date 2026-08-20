import base64

import pytest

from app import main
from app.services.job_searcher import SearchCandidate, SearchResult
from app.services import visual_page


def test_capture_visual_page_uses_viewport_without_persisting_image(monkeypatch):
    target = {
        "id": "job-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/visual.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/job-page",
    }
    calls = []
    image = base64.b64encode(b"visual-page-bytes").decode("ascii")

    monkeypatch.setattr(visual_page, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(visual_page, "read_controlled_edge_targets", lambda: [target])

    def fake_send(_target, method, params=None, **_kwargs):
        calls.append((method, params))
        assert method == "Page.captureScreenshot"
        return {"data": image}

    monkeypatch.setattr(visual_page, "send_cdp_command", fake_send)
    captured = visual_page.capture_controlled_edge_visual_page("viewport")

    assert captured["image_data_url"] == f"data:image/jpeg;base64,{image}"
    metadata = captured["metadata"]
    assert metadata["mode"] == "viewport"
    assert metadata["platform"] == "Boss 直聘"
    assert metadata["image_persisted"] is False
    assert metadata["page_text_saved"] is False
    assert "image_data_url" not in metadata
    assert calls == [("Page.captureScreenshot", {"format": "jpeg", "quality": 78, "fromSurface": True, "captureBeyondViewport": False})]


def test_capture_visual_page_scales_full_page_and_rejects_ambiguous_targets(monkeypatch):
    target = {
        "id": "job-page",
        "type": "page",
        "url": "https://www.liepin.com/job/visual.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/job-page",
    }
    image = base64.b64encode(b"full-page-bytes").decode("ascii")
    monkeypatch.setattr(visual_page, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(visual_page, "read_controlled_edge_targets", lambda: [target])

    def fake_send(_target, method, params=None, **_kwargs):
        if method == "Page.getLayoutMetrics":
            return {"contentSize": {"width": 3000, "height": 6000}}
        assert method == "Page.captureScreenshot"
        return {"data": image}

    monkeypatch.setattr(visual_page, "send_cdp_command", fake_send)
    captured = visual_page.capture_controlled_edge_visual_page("full_page")

    metadata = captured["metadata"]
    assert metadata["mode"] == "full_page"
    assert metadata["content_width"] == 3000
    assert metadata["content_height"] == 6000
    assert metadata["scaled"] is True
    assert 0 < metadata["scale"] < 1

    monkeypatch.setattr(visual_page, "read_controlled_edge_targets", lambda: [target, {**target, "id": "other"}])
    with pytest.raises(ValueError, match="多个招聘岗位或搜索页面"):
        visual_page.capture_controlled_edge_visual_page("viewport")


def test_visual_page_rejects_recruitment_security_interstitial(monkeypatch):
    security_target = {
        "id": "security-page",
        "type": "page",
        "url": "https://www.zhipin.com/web/passport/zp/security.html?callbackUrl=jobs",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/security-page",
    }
    monkeypatch.setattr(visual_page, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(visual_page, "read_controlled_edge_targets", lambda: [security_target])

    with pytest.raises(ValueError, match="没有匹配当前搜索任务"):
        visual_page.capture_controlled_edge_visual_page("viewport")


def test_visual_review_reuses_task_chat_model_and_history(monkeypatch):
    from app import main

    calls = []

    class FakeClient:
        configured = True
        profile = {"name": "聊天视觉模型"}
        model = "vision-chat-model"

        def complete_json_with_image(self, system_prompt, user_prompt, image_data_url):
            calls.append((system_prompt, user_prompt, image_data_url))
            return {
                "page_type": "job_detail",
                "company": "测试科技",
                "title": "AI 应用开发实习生",
                "city": "杭州",
                "salary_text": "200-300 元/天",
                "summary": "要求 Python 和 RAG。",
                "candidate_jobs": [],
                "confidence": 0.8,
                "uncertainties": [],
            }

        def log_error(self, _message):
            raise AssertionError("视觉复核不应记录错误")

    routed_tasks = []
    monkeypatch.setattr(main, "client_for_task", lambda task_type: routed_tasks.append(task_type) or FakeClient())
    monkeypatch.setattr(
        main,
        "capture_controlled_edge_visual_page",
        lambda _mode, **_kwargs: {
            "image_data_url": "data:image/jpeg;base64,dGVzdA==",
            "metadata": {"mode": "viewport", "platform": "Boss 直聘", "image_persisted": False},
        },
    )
    monkeypatch.setattr(main, "control_history_for_model", lambda: [{"user": "找杭州 AI 应用开发实习", "assistant": "已保存搜索条件。"}])

    result = main.run_visual_page_review("viewport", "识图分析当前页面")

    assert routed_tasks == ["control_intent"]
    assert result["status"] == "已完成"
    assert result["model_profile"] == "聊天视觉模型"
    assert result["review"]["company"] == "测试科技"
    assert calls[0][2] == "data:image/jpeg;base64,dGVzdA=="
    assert "找杭州 AI 应用开发实习" in calls[0][1]
    assert "识图分析当前页面" in calls[0][1]


def test_visual_reconciliation_only_enriches_a_unique_dom_candidate_and_screens_senior_experience():
    result = SearchResult(
        platform="Boss 直聘",
        keyword="AI 应用开发",
        city="杭州",
        search_url="https://www.zhipin.com/web/geek/jobs?query=AI",
        browser_channel="msedge",
        candidates=[
            SearchCandidate(
                title="AI 应用开发工程师",
                company="",
                city="",
                source_url="https://www.zhipin.com/job_detail/visual.html",
                summary="AI 应用开发岗位，RAG。",
            )
        ],
    )
    review = {
        "candidate_jobs": [
            {
                "company": "视觉测试科技",
                "title": "AI 应用开发工程师",
                "city": "杭州",
                "salary_text": "200-300 元/天",
                "experience_text": "5-10 年经验",
            }
        ]
    }

    reconciled = main.reconcile_search_result_with_visual_review(result, review)
    candidate = result.candidates[0]
    accepted, reason = main.discovery_candidate_screening(
        {"title": candidate.title, "summary": candidate.summary}
    )

    assert reconciled == 1
    assert candidate.company == "视觉测试科技"
    assert candidate.city == "杭州"
    assert "视觉薪资：200-300 元/天" in candidate.summary
    assert "视觉经验：5-10 年经验" in candidate.summary
    assert accepted is False
    assert "5-10 年经验" in reason


def test_visual_review_is_requested_for_complete_results_without_internship_signal():
    complete_social_result = SearchResult(
        platform="Boss 直聘",
        keyword="AI 应用开发实习",
        city="杭州",
        search_url="https://www.zhipin.com/web/geek/jobs?query=AI",
        browser_channel="msedge",
        candidates=[
            SearchCandidate(
                title="AI 应用开发工程师",
                company="测试科技",
                city="杭州",
                source_url="https://www.zhipin.com/job_detail/social.html",
                summary="负责 Agent 系统和 RAG 应用开发。",
            )
        ],
    )
    complete_internship_result = SearchResult(
        platform="Boss 直聘",
        keyword="AI 应用开发实习",
        city="杭州",
        search_url="https://www.zhipin.com/web/geek/jobs?query=AI",
        browser_channel="msedge",
        candidates=[
            SearchCandidate(
                title="AI 应用开发实习生",
                company="测试科技",
                city="杭州",
                source_url="https://www.zhipin.com/job_detail/intern.html",
                summary="面向在校生，参与 RAG 应用开发。",
            )
        ],
    )

    assert main.search_result_needs_visual_review(complete_social_result) is True
    assert main.search_result_needs_visual_review(complete_internship_result) is False


def test_controlled_discovery_uses_visual_fallback_before_detail_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "visual-discovery.sqlite3"))
    from app.db import connect, init_db

    init_db()
    visual_calls = []
    fetch_calls = []

    def fake_search(platform, keyword, city, limit):
        suffix = {"Boss 直聘": "boss", "猎聘": "liepin", "实习僧": "shixiseng"}[platform]
        return SearchResult(
            platform=platform,
            keyword=keyword,
            city=city,
            search_url=f"https://example.com/{suffix}/search",
            browser_channel="msedge",
            candidates=[
                SearchCandidate(
                    title="AI 应用开发工程师",
                    company="",
                    city=city,
                    source_url=f"https://example.com/{suffix}/job-detail",
                    summary="AI 应用开发与 RAG 岗位。",
                )
            ],
        )

    def fake_visual(mode, user_message, **kwargs):
        visual_calls.append((mode, kwargs["expected_url"], kwargs["platform"]))
        return {
            "status": "已完成",
            "review": {
                "candidate_jobs": [
                    {
                        "company": "视觉补全科技",
                        "title": "AI 应用开发工程师",
                        "city": "杭州",
                        "salary_text": "200-300 元/天",
                        "experience_text": "5-10 年经验",
                    }
                ]
            },
        }

    monkeypatch.setattr(main, "search_jobs_in_controlled_edge", fake_search)
    monkeypatch.setattr(main, "run_visual_page_review", fake_visual)
    monkeypatch.setattr(main, "fetch_job_from_controlled_edge", lambda url: fetch_calls.append(url))

    result = main.run_controlled_job_discovery()
    with connect() as conn:
        candidates = conn.execute("SELECT company, summary, status, error_message FROM job_candidates ORDER BY id").fetchall()

    assert result["status"] == "完成"
    assert result["visual_review_count"] == 3
    assert result["visual_reconciled_count"] == 3
    assert result["screened_out_count"] == 3
    assert len(visual_calls) == 3
    assert fetch_calls == []
    assert all(row["company"] == "视觉补全科技" for row in candidates)
    assert all("视觉经验：5-10 年经验" in row["summary"] for row in candidates)
    assert all(row["status"] == "初筛待确认" for row in candidates)
    assert all("5-10 年经验" in row["error_message"] for row in candidates)
