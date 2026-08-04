from app.services.analyzer import rule_extract_jd, score_job


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
