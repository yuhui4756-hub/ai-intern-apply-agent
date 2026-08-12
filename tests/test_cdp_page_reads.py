from app.services import application_browser, browser_patrol, communication_browser


def test_browser_patrol_reads_cdp_snapshot_without_browser_takeover(monkeypatch):
    target = {
        "id": "chat-page",
        "type": "page",
        "url": "https://c.liepin.com/im/chat",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/chat-page",
    }
    expressions = []

    monkeypatch.setattr(browser_patrol, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(browser_patrol, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        browser_patrol,
        "evaluate_cdp_expression",
        lambda _target, expression: expressions.append(expression)
        or {
            "url": target["url"],
            "title": "猎聘聊天",
            "conversation_text": "王女士\nAI 应用开发实习生\n您好，想和您沟通岗位情况。\n发简历",
            "body_text": "",
        },
    )

    observations = browser_patrol.capture_browser_patrol_observations()

    assert len(observations) == 1
    assert observations[0]["platform"] == "猎聘"
    assert observations[0]["text_scope"] == "conversation_panel"
    assert "conversationText" in expressions[0]


def test_application_probe_reads_cdp_snapshot_and_selector_counts(monkeypatch):
    target = {
        "id": "job-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/example.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/job-page",
    }
    plan = {
        "selector_candidates": {
            "application_button": ["button:has-text('立即沟通')"],
            "resume_control": ["input[type='file']"],
        }
    }
    expressions = []

    monkeypatch.setattr(application_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(application_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        application_browser,
        "evaluate_cdp_expression",
        lambda _target, expression: expressions.append(expression)
        or {
            "url": target["url"],
            "title": "AI Agent 开发实习生 - 测试智能科技",
            "text": "测试智能科技\nAI Agent 开发实习生",
            "selectors": {
                "button:has-text('立即沟通')": 1,
                "input[type='file']": 0,
            },
        },
    )

    snapshots = application_browser.capture_application_browser_snapshots(plan)

    assert len(snapshots) == 1
    assert snapshots[0]["host"] == "www.zhipin.com"
    assert snapshots[0]["selectors"]["button:has-text('立即沟通')"] == 1
    assert "countSelector" in expressions[0]
    assert "立即沟通" in expressions[0]


def test_message_probe_reads_cdp_snapshot_and_keeps_only_counts(monkeypatch):
    target = {
        "id": "chat-page",
        "type": "page",
        "url": "https://c.liepin.com/im/chat",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/chat-page",
    }
    browser_plan = {
        "browser_plans": [
            {
                "selector_candidates": {
                    "conversation_panel": ["[class*='chat']"],
                    "message_input": ["textarea"],
                    "send_button": ["button:has-text('发送')"],
                }
            }
        ]
    }
    expressions = []

    monkeypatch.setattr(communication_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(communication_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        communication_browser,
        "evaluate_cdp_expression",
        lambda _target, expression: expressions.append(expression)
        or {
            "url": target["url"],
            "title": "猎聘聊天",
            "text": "测试智能科技\nAI Agent 开发实习生\n请输入文字",
            "selectors": {
                "[class*='chat']": 1,
                "textarea": 1,
                "button:has-text('发送')": 1,
            },
        },
    )

    snapshots = communication_browser.capture_browser_probe_snapshots(browser_plan)

    assert len(snapshots) == 1
    assert snapshots[0]["selectors"]["textarea"] == 1
    assert "countSelector" in expressions[0]
    assert "发送" in expressions[0]


def test_chat_page_calibration_keeps_only_structural_signals(monkeypatch):
    targets = [
        {
            "id": "liepin-chat",
            "type": "page",
            "url": "https://c.liepin.com/im/chat",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/liepin-chat",
        },
        {
            "id": "boss-job",
            "type": "page",
            "url": "https://www.zhipin.com/web/geek/job",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/boss-job",
        },
    ]
    expressions = []

    monkeypatch.setattr(communication_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(communication_browser, "read_controlled_edge_targets", lambda: targets)
    monkeypatch.setattr(
        communication_browser,
        "evaluate_cdp_expression",
        lambda target, expression: expressions.append(expression)
        or (
            {
                "chat_url_match": True,
                "chat_text_hint_match": True,
                "conversation_panel_count": 1,
                "message_input_count": 1,
                "send_button_count": 1,
                "sensitive_signal_count": 0,
            }
            if target["id"] == "liepin-chat"
            else {
                "chat_url_match": False,
                "chat_text_hint_match": False,
                "conversation_panel_count": 0,
                "message_input_count": 0,
                "send_button_count": 0,
                "sensitive_signal_count": 0,
            }
        ),
    )

    result = communication_browser.calibrate_controlled_edge_chat_pages()

    assert result["status"] == "校准完成"
    assert result["checked_page_count"] == 2
    assert result["candidate_chat_count"] == 1
    assert result["structure_ready_count"] == 1
    assert result["results"][0]["platform"] == "猎聘"
    assert result["results"][0]["status"] == "结构可校准"
    assert result["results"][1]["status"] == "非聊天页"
    assert result["page_text_saved"] is False
    assert result["page_url_saved"] is False
    assert result["browser_clicked"] is False
    assert all("button.click" not in expression for expression in expressions)
    assert all("location.href" not in str(item) for item in result["results"])


def test_chat_page_calibration_marks_sensitive_candidate_for_manual_review(monkeypatch):
    target = {
        "id": "boss-chat",
        "type": "page",
        "url": "https://www.zhipin.com/geek/chat/example",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/boss-chat",
    }

    monkeypatch.setattr(communication_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(communication_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(
        communication_browser,
        "evaluate_cdp_expression",
        lambda *_args: {
            "chat_url_match": True,
            "chat_text_hint_match": True,
            "conversation_panel_count": 1,
            "message_input_count": 1,
            "send_button_count": 1,
            "sensitive_signal_count": 1,
        },
    )

    result = communication_browser.calibrate_controlled_edge_chat_pages()

    assert result["sensitive_count"] == 1
    assert result["structure_ready_count"] == 0
    assert result["results"][0]["status"] == "含敏感提示"


def test_cdp_fill_requires_one_verified_safe_target(monkeypatch):
    target = {
        "id": "chat-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/chat-ready.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/chat-page",
    }
    plan = cdp_chat_plan()
    snapshot = cdp_chat_snapshot(target["url"])
    expressions = []

    monkeypatch.setattr(communication_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(communication_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(communication_browser, "capture_browser_probe_target_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(
        communication_browser,
        "evaluate_cdp_expression",
        lambda _target, expression: expressions.append(expression) or {"ok": True, "selector": "textarea"},
    )

    result = communication_browser.fill_message_in_controlled_edge(plan, "您好，想了解岗位主要工作内容。")

    assert result["status"] == "已填入"
    assert result["browser_clicked"] is False
    assert len(expressions) == 1
    assert "const message" in expressions[0]
    assert "button.click" not in expressions[0]


def test_cdp_send_rechecks_page_after_filling(monkeypatch):
    target = {
        "id": "chat-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/chat-ready.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/chat-page",
    }
    plan = cdp_chat_plan()
    snapshot = cdp_chat_snapshot(target["url"])
    snapshots = [snapshot, snapshot]
    expressions = []
    mouse_events = []

    monkeypatch.setattr(communication_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(communication_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(communication_browser, "capture_browser_probe_target_snapshot", lambda *_args: snapshots.pop(0))
    monkeypatch.setattr(
        communication_browser,
        "evaluate_cdp_expression",
        lambda _target, expression: expressions.append(expression)
        or (
            {"ok": True, "selector": "textarea"}
            if "const message" in expression
            else {"ok": True, "selector": "button:has-text('发送')", "x": 120, "y": 80}
        ),
    )
    monkeypatch.setattr(
        communication_browser,
        "send_cdp_command",
        lambda _target, method, params, **_kwargs: mouse_events.append((method, params)) or {},
    )

    result = communication_browser.send_message_in_controlled_edge(plan, "您好，想了解岗位主要工作内容。")

    assert result["status"] == "已发送"
    assert result["browser_clicked"] is True
    assert len(expressions) == 2
    assert "const message" in expressions[0]
    assert "button.click" not in expressions[1]
    assert [event[1]["type"] for event in mouse_events] == ["mousePressed", "mouseReleased"]


def test_cdp_fill_does_not_execute_when_sensitive_text_is_present(monkeypatch):
    target = {
        "id": "chat-page",
        "type": "page",
        "url": "https://www.zhipin.com/job_detail/chat-ready.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/chat-page",
    }
    plan = cdp_chat_plan()
    snapshot = cdp_chat_snapshot(target["url"], extra_text="请先上传简历")
    actions = []

    monkeypatch.setattr(communication_browser, "wait_for_debug_endpoint", lambda **_kwargs: True)
    monkeypatch.setattr(communication_browser, "read_controlled_edge_targets", lambda: [target])
    monkeypatch.setattr(communication_browser, "capture_browser_probe_target_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(communication_browser, "evaluate_cdp_expression", lambda *_args: actions.append(True))

    try:
        communication_browser.fill_message_in_controlled_edge(plan, "您好，想了解岗位主要工作内容。")
    except ValueError as exc:
        assert "未找到可安全填入草稿" in str(exc)
    else:
        raise AssertionError("敏感页面不能进入草稿填入")

    assert not actions


def cdp_chat_plan() -> dict[str, object]:
    return {
        "browser_action": "dry_run_ready",
        "platform": "Boss 直聘",
        "company": "测试智能科技",
        "job_title": "AI Agent 开发实习生",
        "source_url": "https://www.zhipin.com/job_detail/chat-ready.html",
        "page_match": {
            "domains": ["zhipin.com"],
            "chat_url_tokens": ["job_detail"],
            "chat_text_hints": ["请输入"],
        },
        "selector_candidates": {
            "conversation_panel": ["[class*='chat']"],
            "message_input": ["textarea", "[class*='input'] textarea"],
            "send_button": ["button:has-text('发送')"],
        },
    }


def cdp_chat_snapshot(url: str, extra_text: str = "") -> dict[str, object]:
    text = f"测试智能科技 AI Agent 开发实习生 请输入内容后发送 {extra_text}"
    return {
        "url": url,
        "title": "测试智能科技 HR 对话",
        "host": "www.zhipin.com",
        "text_length": len(text),
        "text_digest": communication_browser.text_digest(text),
        "normalized_text": communication_browser.normalize_probe_text(text),
        "selectors": {
            "[class*='chat']": 1,
            "textarea": 1,
            "[class*='input'] textarea": 1,
            "button:has-text('发送')": 1,
        },
    }
