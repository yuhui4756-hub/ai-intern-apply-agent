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
