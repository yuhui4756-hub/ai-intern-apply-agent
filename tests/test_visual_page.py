import base64

import pytest

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
        lambda _mode: {
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
