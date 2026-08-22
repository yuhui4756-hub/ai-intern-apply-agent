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

    assert routed_tasks == ["agent_chat"]
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


def test_visual_salary_review_targets_masked_salary_without_rejecting_the_candidate():
    result = SearchResult(
        platform="Boss 直聘",
        keyword="AI 应用开发实习",
        city="杭州",
        search_url="https://www.zhipin.com/web/geek/jobs?query=AI",
        browser_channel="msedge",
        candidates=[
            SearchCandidate(
                title="AI 应用开发实习生",
                company="薪资视觉测试科技",
                city="杭州",
                source_url="https://www.zhipin.com/job_detail/salary-visual.html",
                summary="AI 应用开发实习生\n\ue033\ue031\ue031-\ue033\ue036\ue031元/天\n5天/周\n3个月",
            ),
            SearchCandidate(
                title="AI Agent 实习生",
                company="正常薪资科技",
                city="杭州",
                source_url="https://www.zhipin.com/job_detail/salary-normal.html",
                summary="AI Agent 实习生\n200-300元/天\n5天/周",
            ),
        ],
    )

    targets = main.search_result_salary_visual_targets(result)

    assert main.search_result_needs_visual_review(result) is True
    assert targets == [{"company": "薪资视觉测试科技", "title": "AI 应用开发实习生", "city": "杭州"}]


def test_visual_reconciliation_merges_only_valid_missing_salary():
    result = SearchResult(
        platform="Boss 直聘",
        keyword="AI 应用开发实习",
        city="杭州",
        search_url="https://www.zhipin.com/web/geek/jobs?query=AI",
        browser_channel="msedge",
        candidates=[
            SearchCandidate(
                title="AI 应用开发实习生",
                company="薪资视觉测试科技",
                city="杭州",
                source_url="https://www.zhipin.com/job_detail/salary-visual.html",
                summary="AI 应用开发实习生\n\ue033\ue031\ue031-\ue033\ue036\ue031元/天\n5天/周",
            )
        ],
    )
    review = {
        "candidate_jobs": [
            {"company": "薪资视觉测试科技", "title": "AI 应用开发实习生", "salary_text": "180-260 元/天"},
            {"company": "薪资视觉测试科技", "title": "AI 应用开发实习生", "salary_text": "待遇优厚"},
        ]
    }

    reconciled = main.reconcile_search_result_with_visual_review(result, review)

    assert reconciled == 1
    assert "视觉薪资：180-260 元/天" in result.candidates[0].summary
    assert "待遇优厚" not in result.candidates[0].summary


def test_visual_job_detail_fallback_closes_temporary_target_and_returns_valid_jd(monkeypatch):
    target = {
        "id": "temporary-detail",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/visual-detail.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/temporary-detail",
    }
    closed = []

    class FakeClient:
        configured = True
        profile = {"name": "聊天视觉模型"}
        model = "vision-chat-model"

        def complete_json_with_image(self, _system_prompt, _user_prompt, image_data_url):
            assert image_data_url == "data:image/jpeg;base64,dGVzdA=="
            return {
                "company": "视觉详情科技",
                "title": "AI 应用开发实习生",
                "city": "杭州",
                "salary_text": "200-300 元/天",
                "experience_text": "经验不限",
                "internship_days": "5天/周",
                "internship_duration": "3个月",
                "jd_text": "参与 AI 应用和 RAG 检索流程开发，协助实现 FastAPI 接口、文档处理和本地评测脚本。要求具备 Python 基础，了解大模型 API 调用与 SQLite 数据处理，能够每周到岗五天并持续实习三个月。",
                "confidence": 0.86,
                "uncertainties": ["具体团队规模未显示"],
            }

        def log_error(self, _message):
            raise AssertionError("详情视觉复核不应记录错误")

    monkeypatch.setattr(main, "client_for_task", lambda task_type: FakeClient() if task_type == "agent_chat" else None)
    monkeypatch.setattr(main, "create_controlled_edge_target", lambda _url: target)
    monkeypatch.setattr(main, "wait_for_cdp_document_ready", lambda _target: None)
    monkeypatch.setattr(
        main,
        "evaluate_cdp_expression",
        lambda *_args, **_kwargs: {"url": target["url"], "title": "AI 应用开发实习生", "text": "页面文本太短"},
    )
    monkeypatch.setattr(
        main,
        "capture_visual_page_target",
        lambda _target, _mode: {
            "image_data_url": "data:image/jpeg;base64,dGVzdA==",
            "metadata": {"platform": "Boss 直聘", "image_persisted": False, "page_text_saved": False},
        },
    )
    monkeypatch.setattr(main, "close_controlled_edge_target", lambda item: closed.append(item) or True)
    monkeypatch.setattr(main, "control_history_for_model", lambda: [])

    result = main.run_visual_job_detail_fallback(
        target["url"], {"company": "", "title": "AI 应用开发实习生", "city": "杭州"}
    )

    assert result["status"] == "已完成"
    assert result["fetched"].fetch_mode == "controlled_edge_visual"
    assert len(result["fetched"].text) >= 80
    assert result["review"]["company"] == "视觉详情科技"
    assert result["capture"]["image_persisted"] is False
    assert closed == [target]


def test_visual_detail_fallback_is_used_for_short_dom_text_but_not_login(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "visual-detail-import.sqlite3"))
    from app.db import connect, init_db

    init_db()
    run_id = main.save_search_result(
        SearchResult(
            platform="Boss 直聘",
            keyword="AI 应用开发实习",
            city="杭州",
            search_url="https://example.com/search",
            browser_channel="msedge",
            candidates=[
                SearchCandidate(
                    title="AI 应用开发实习生",
                    company="候选测试科技",
                    city="杭州",
                    source_url="https://example.com/job_detail/visual-fallback",
                    summary="Python RAG FastAPI 实习岗位。",
                )
            ],
        )
    )
    with connect() as conn:
        candidate_id = conn.execute("SELECT id FROM job_candidates WHERE search_run_id = ?", (run_id,)).fetchone()["id"]

    fallback_calls = []
    monkeypatch.setattr(main, "fetch_job_from_controlled_edge", lambda _url: (_ for _ in ()).throw(ValueError("页面文本太短，可能需要浏览器渲染。")))
    monkeypatch.setattr(
        main,
        "run_visual_job_detail_fallback",
        lambda url, candidate: fallback_calls.append((url, candidate["title"])) or {
            "status": "已完成",
            "fetched": main.FetchResult(
                url=url,
                final_url=url,
                title="AI 应用开发实习生",
                text="参与 AI 应用开发和 RAG 流程实现，协助处理文档数据并开发 FastAPI 接口。要求 Python 基础、了解大模型 API 和 SQLite，每周到岗五天并持续实习三个月。",
                fetch_mode="controlled_edge_visual",
                note="DOM 岗位详情文本不足，已用临时页面视觉复核补充；截图未保存。",
            ),
            "review": {"confidence": 0.84},
        },
    )
    monkeypatch.setattr(main, "try_llm_jd_extract", lambda _text: ({}, ""))
    monkeypatch.setattr(main, "search_company", lambda *_args, **_kwargs: [])

    result = main.import_discovery_candidate(candidate_id, resume_id=1)
    with connect() as conn:
        candidate = conn.execute("SELECT status, job_id FROM job_candidates WHERE id = ?", (candidate_id,)).fetchone()
        job = conn.execute("SELECT jd_text, analysis_source FROM job_postings WHERE id = ?", (candidate["job_id"],)).fetchone()

    assert result["status"] == "已导入"
    assert result["visual_detail_fallback"] is True
    assert fallback_calls == [("https://example.com/job_detail/visual-fallback", "AI 应用开发实习生")]
    assert candidate["status"] == "已导入"
    assert "参与 AI 应用开发" in job["jd_text"]

    assert main.visual_detail_fallback_allowed(ValueError("需要登录")) is False
    assert main.visual_detail_fallback_allowed(ValueError("页面文本太短")) is True


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
