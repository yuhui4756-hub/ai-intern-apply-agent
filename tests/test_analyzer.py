from app.services.analyzer import clean_extracted, extract_salary, looks_like_salary_text, rule_extract_jd, score_job


def test_high_match_rag_agent_job_is_recommended():
    jd = """
    AI Agent 开发实习生
    岗位职责：负责 RAG 知识库、文档解析、向量检索和 Agent 工具调用。
    任职要求：熟悉 Python、FastAPI、LangChain、Prompt Engineering、SQLite，有测试意识。
    薪资：200-300元/天，杭州，每周 5 天。
    """
    resume = "Python FastAPI SQLite RAG FTS5 BM25 pytest Ollama DeepSeek API"

    extracted = rule_extract_jd(jd)
    scoring = score_job(extracted, jd, resume)

    assert scoring["recommendation"] in {"必投", "可冲"}
    assert scoring["score"] >= 60
    assert "Python" in scoring["matched_skills"]
    assert "RAG" in scoring["matched_skills"]


def test_high_risk_keyword_is_skipped():
    jd = """
    AI 实习生
    入职前需要缴纳培训费，可贷款，后续安排项目。
    """

    extracted = rule_extract_jd(jd)
    scoring = score_job(extracted, jd, "")

    assert scoring["recommendation"] == "跳过"
    assert scoring["risk_level"] == "高"
    assert "培训费" in scoring["skip_reason"]


def test_extract_company_from_jd_text():
    jd = """
    公司名称：杭州测试智能科技有限公司
    岗位：AI 应用开发实习生
    要求 Python、RAG、FastAPI。
    """

    extracted = rule_extract_jd(jd)

    assert extracted["company"] == "杭州测试智能科技有限公司"


def test_clean_unknown_placeholders():
    cleaned = clean_extracted(
        {
            "title": "?",
            "company": "未知",
            "city": "N/A",
            "salary_text": "200-300元/天",
            "required_skills": ["Python", "？", "RAG"],
            "risk_signals": ["暂无"],
        }
    )

    assert cleaned["title"] == ""
    assert cleaned["company"] == ""
    assert cleaned["city"] == ""
    assert cleaned["required_skills"] == ["Python", "RAG"]
    assert cleaned["risk_signals"] == []


def test_extract_salary_handles_common_intern_formats():
    assert extract_salary("实习薪资：150～250 元/天，每周 5 天") == "150～250元/天"
    assert extract_salary("薪资 120-180/天，实习三个月") == "120-180/天"
    assert extract_salary("月薪 2k-4k，13薪") == "2k-4k"
    assert extract_salary("薪资：3-5K/月") == "3-5K/月"
    assert extract_salary("补贴：100元/日") == "100元/日"


def test_salary_guard_rejects_time_requirements():
    assert not looks_like_salary_text("每周 5 天")
    assert not looks_like_salary_text("3 个月以上")
