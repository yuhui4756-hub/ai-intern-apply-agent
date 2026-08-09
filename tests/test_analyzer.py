from app.services.analyzer import daily_salary_bounds, clean_extracted, extract_salary, looks_like_salary_text, rule_extract_jd, score_job


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


def test_daily_salary_preference_does_not_guess_from_monthly_salary():
    jd = "AI 应用开发实习生，Python、FastAPI、RAG，薪资 120-180 元/天。"
    extracted = rule_extract_jd(jd)
    scoring = score_job(extracted, jd, "Python FastAPI RAG", {"min_salary_per_day": 200})

    assert daily_salary_bounds("120-180元/天") == (120, 180)
    assert daily_salary_bounds("3-5K/月") is None
    assert scoring["recommendation"] == "跳过"
    assert "低于设置底线 200 元/天" in scoring["skip_reason"]

    monthly_jd = "AI 应用开发实习生，Python、FastAPI、RAG，薪资 3-5K/月。"
    monthly_scoring = score_job(rule_extract_jd(monthly_jd), monthly_jd, "Python FastAPI RAG", {"min_salary_per_day": 200})
    assert "薪资最低" not in monthly_scoring["skip_reason"]


def test_score_treats_explicit_language_alternatives_as_one_requirement():
    jd = "岗位要求：熟悉 Python/Java/Go/php 中的至少一种，且熟悉 LangChain。"
    extracted = {
        "required_skills": ["Python", "Java", "Go", "php", "LangChain"],
        "salary_text": "200-300元/天",
    }

    scoring = score_job(extracted, jd, "Python RAG")

    assert "Python" in scoring["matched_skills"]
    assert not {"Java", "Go", "php"} & set(scoring["missing_skills"])
    assert "LangChain" in scoring["missing_skills"]
    assert "Python / Java / Go / php（满足其一）" in scoring["alternative_requirement_groups"]


def test_score_does_not_count_ai_coding_tools_as_skill_gaps():
    jd = "要求熟练使用 Claude Code、Codex、Cursor 等 AI 编程工具，熟悉 Python 和 Redis。"
    extracted = {
        "required_skills": ["Python", "Redis", "Claude Code", "Codex", "Cursor", "AI编程工具"],
        "salary_text": "200-300元/天",
    }

    scoring = score_job(extracted, jd, "Python")

    assert scoring["missing_skills"] == ["Redis"]
    assert scoring["non_blocking_skills"] == ["Claude Code", "Codex", "Cursor", "AI编程工具"]


def test_score_reduces_technical_points_when_many_requirements_are_unmatched():
    jd = "AI Agent 开发岗位，要求 Python、RAG、LangChain、LangGraph、Kubernetes、Milvus、vLLM、LoRA、MLOps、Prometheus。"
    extracted = {
        "required_skills": ["Python", "RAG", "LangChain", "LangGraph", "Kubernetes", "Milvus", "vLLM", "LoRA", "MLOps", "Prometheus"],
        "salary_text": "20-35k",
    }

    scoring = score_job(extracted, jd, "Python RAG")

    assert scoring["score_breakdown"]["technical"] == 13
    assert scoring["score_breakdown"]["required_units"] == 10
    assert scoring["score_breakdown"]["matched_requirement_units"] == 2
    assert scoring["score"] < 70
    assert scoring["fit_notes"]
