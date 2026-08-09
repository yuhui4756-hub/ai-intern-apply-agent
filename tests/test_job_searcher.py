from app.services.job_searcher import build_search_url, extract_candidates_from_anchors, pick_search_page


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
