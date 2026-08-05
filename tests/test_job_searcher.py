from app.services.job_searcher import build_search_url, extract_candidates_from_anchors


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
