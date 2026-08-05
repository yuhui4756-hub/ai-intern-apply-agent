from __future__ import annotations

import re
from typing import Any


TARGET_SKILLS = [
    "Python",
    "FastAPI",
    "Flask",
    "RAG",
    "Agent",
    "LangChain",
    "LangGraph",
    "LlamaIndex",
    "Prompt",
    "Function Calling",
    "Tool Calling",
    "向量数据库",
    "Milvus",
    "FAISS",
    "Chroma",
    "PGVector",
    "SQLite",
    "MySQL",
    "Redis",
    "Elasticsearch",
    "Docker",
    "Linux",
    "pytest",
    "OpenAI",
    "DeepSeek",
    "Qwen",
    "Ollama",
]

RISK_KEYWORDS = [
    "培训费",
    "贷款",
    "押金",
    "无薪",
    "课程顾问",
    "电话销售",
    "邀约",
    "拉新",
    "地推",
    "兼职推广",
    "收费内推",
    "包装简历",
]

CAUTION_KEYWORDS = [
    "全栈",
    "CUDA",
    "论文",
    "模型训练",
    "强化学习",
    "前端",
    "React",
    "Vue",
    "销售",
    "运营",
    "标注",
]

GOOD_GROWTH_KEYWORDS = [
    "导师",
    "落地",
    "生产",
    "评测",
    "benchmark",
    "知识库",
    "检索",
    "Agent",
    "RAG",
    "工程化",
    "Docker",
]

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京", "苏州", "厦门", "武汉", "长沙"]
UNKNOWN_VALUES = {"?", "？", "未知", "未填写", "未填", "无", "暂无", "N/A", "n/a", "None", "null"}
SALARY_NUMBER = r"\d+(?:\.\d+)?"
SALARY_RANGE = r"(?:-|~|～|至|到|—|–|－)"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def find_skills(text: str) -> list[str]:
    lowered = text.lower()
    matched: list[str] = []
    for skill in TARGET_SKILLS:
        if skill.lower() in lowered or skill in text:
            matched.append(skill)
    return matched


def extract_city(text: str) -> str:
    for city in CITIES:
        if city in text:
            return city
    return ""


def extract_salary(text: str) -> str:
    patterns = [
        fr"{SALARY_NUMBER}\s*{SALARY_RANGE}\s*{SALARY_NUMBER}\s*元\s*/?\s*[天日]",
        fr"{SALARY_NUMBER}\s*{SALARY_RANGE}\s*{SALARY_NUMBER}\s*/\s*[天日]",
        fr"{SALARY_NUMBER}\s*元\s*/?\s*[天日]",
        fr"{SALARY_NUMBER}\s*/\s*[天日]",
        fr"{SALARY_NUMBER}\s*[kK]\s*{SALARY_RANGE}\s*{SALARY_NUMBER}\s*[kK]\s*(?:/[月年])?",
        fr"{SALARY_NUMBER}\s*{SALARY_RANGE}\s*{SALARY_NUMBER}\s*[kK]\s*(?:/[月年])?",
        fr"{SALARY_NUMBER}\s*千\s*{SALARY_RANGE}\s*{SALARY_NUMBER}\s*千\s*(?:/[月年])?",
        fr"{SALARY_NUMBER}\s*{SALARY_RANGE}\s*{SALARY_NUMBER}\s*千\s*(?:/[月年])?",
        r"(?:薪资|薪酬|工资|日薪|实习补贴|补贴)[:：]?\s*(?:面议|薪资面议)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return clean_salary_text(match.group(0))
    return ""


def clean_salary_text(value: str) -> str:
    return re.sub(r"\s+", "", normalize(value)).strip(" -:：,，。；;")


def looks_like_salary_text(value: str) -> bool:
    text = clean_salary_text(value).lower()
    if not text:
        return False
    if any(keyword in text for keyword in ["面议", "薪资待定", "薪酬待定"]):
        return True
    if not re.search(r"\d", text):
        return False
    return bool(re.search(r"[k千万元￥¥]", text) or "/天" in text or "/日" in text or "日薪" in text or "月薪" in text)


def extract_company(text: str) -> str:
    patterns = [
        r"(?:公司名称|公司|企业|单位)[:：]\s*([^\n\r，,。；;]{2,40})",
        r"([^\n\r，,。；;]{2,40})(?:招聘|直聘|校招)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = clean_value(match.group(1))
            if value:
                return value
    return ""


def clean_value(value: Any) -> str:
    text = normalize(str(value or ""))
    text = text.strip(" -:：,，。；;")
    return "" if text in UNKNOWN_VALUES else text


def clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    cleaned = []
    for value in values:
        item = clean_value(value)
        if item and item not in cleaned:
            cleaned.append(item)
    return cleaned


def clean_extracted(extracted: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(extracted)
    for key in ["title", "company", "city", "salary_text", "internship_days", "internship_duration"]:
        cleaned[key] = clean_value(cleaned.get(key))
    for key in ["responsibilities", "requirements", "required_skills", "bonus_skills", "risk_signals", "caution_signals"]:
        cleaned[key] = clean_list(cleaned.get(key))
    return cleaned


def salary_score(salary_text: str) -> int:
    if not salary_text:
        return 8
    numbers = [int(item) for item in re.findall(r"\d+", salary_text)]
    if not numbers:
        return 8
    low = min(numbers)
    if "k" in salary_text.lower():
        return 15
    if low >= 300:
        return 15
    if low >= 200:
        return 13
    if low >= 150:
        return 10
    return 4


def guess_title(text: str) -> str:
    for line in (text or "").splitlines():
        clean = line.strip(" -：:")
        if not clean:
            continue
        if any(word in clean for word in ["实习", "开发", "Agent", "AI", "大模型"]):
            return clean[:80]
    return "待命名岗位"


def rule_extract_jd(jd_text: str, fallback_title: str = "", fallback_company: str = "", fallback_city: str = "", fallback_salary: str = "") -> dict[str, Any]:
    text = jd_text or ""
    skills = find_skills(text)
    risk_signals = [keyword for keyword in RISK_KEYWORDS if keyword in text]
    caution_signals = [keyword for keyword in CAUTION_KEYWORDS if keyword in text]
    city = fallback_city or extract_city(text)
    salary = fallback_salary or extract_salary(text)
    return clean_extracted({
        "title": fallback_title or guess_title(text),
        "company": fallback_company or extract_company(text),
        "city": city,
        "salary_text": salary,
        "internship_days": "5 天" if re.search(r"每周\s*5|5\s*天", text) else "",
        "internship_duration": "3 个月以上" if re.search(r"3\s*个月|三个月|长期", text) else "",
        "responsibilities": split_section(text, ["岗位职责", "工作职责", "职责", "你将负责"]),
        "requirements": split_section(text, ["任职要求", "岗位要求", "要求", "希望你"]),
        "required_skills": skills,
        "bonus_skills": [skill for skill in skills if skill in ["LangChain", "LlamaIndex", "LangGraph", "Docker", "Elasticsearch"]],
        "risk_signals": risk_signals,
        "caution_signals": caution_signals,
    })


def split_section(text: str, markers: list[str]) -> list[str]:
    lines = [line.strip(" -\t") for line in (text or "").splitlines() if line.strip()]
    selected = []
    active = False
    for line in lines:
        if any(marker in line for marker in markers):
            active = True
            continue
        if active and re.search(r"(福利|薪资|公司|地点|加分|其他)", line):
            break
        if active:
            selected.append(line[:180])
        if len(selected) >= 6:
            break
    if selected:
        return selected
    return [line[:180] for line in lines[:5]]


def score_job(extracted: dict[str, Any], jd_text: str, resume_text: str = "") -> dict[str, Any]:
    text = jd_text or ""
    required_skills = extracted.get("required_skills") or find_skills(text)
    resume_lower = (resume_text or "").lower()
    matched_skills = [skill for skill in required_skills if skill.lower() in resume_lower or skill in ["Python", "FastAPI", "SQLite", "RAG"]]

    tech_score = min(35, 8 + len(matched_skills) * 4 + ("RAG" in required_skills) * 4 + ("Agent" in required_skills) * 3)
    growth_score = min(25, 8 + sum(3 for keyword in GOOD_GROWTH_KEYWORDS if keyword.lower() in text.lower()))
    salary_points = salary_score(extracted.get("salary_text") or "")
    location_score = 10 if (extracted.get("city") in ["北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", ""] or "远程" in text) else 6

    risk_signals = extracted.get("risk_signals") or []
    caution_signals = extracted.get("caution_signals") or []
    risk_score = 15
    if risk_signals:
        risk_score = 2
    elif caution_signals:
        risk_score = 9

    score = int(min(100, tech_score + growth_score + salary_points + location_score + risk_score))
    if risk_signals:
        level = "低匹配"
        recommendation = "跳过"
        risk_level = "高"
        skip_reason = "命中高风险信号：" + "、".join(risk_signals)
    elif score >= 80:
        level = "高匹配"
        recommendation = "必投"
        risk_level = "低" if not caution_signals else "谨慎"
        skip_reason = ""
    elif score >= 60:
        level = "中匹配"
        recommendation = "可冲"
        risk_level = "谨慎" if caution_signals else "低"
        skip_reason = ""
    else:
        level = "低匹配"
        recommendation = "跳过"
        risk_level = "谨慎" if caution_signals else "低"
        skip_reason = "技术匹配或成长性不足"

    return {
        "score": score,
        "level": level,
        "recommendation": recommendation,
        "risk_level": risk_level,
        "skip_reason": skip_reason,
        "matched_skills": matched_skills,
        "missing_skills": [skill for skill in required_skills if skill not in matched_skills],
        "caution_signals": caution_signals,
        "risk_signals": risk_signals,
        "score_breakdown": {
            "technical": tech_score,
            "growth": growth_score,
            "salary": salary_points,
            "location_time": location_score,
            "company_risk": risk_score,
        },
    }


def generate_message(extracted: dict[str, Any], scoring: dict[str, Any]) -> dict[str, str]:
    company = extracted.get("company") or "贵公司"
    skills = scoring.get("matched_skills") or ["Python", "RAG", "FastAPI"]
    top_skills = "、".join(skills[:5])
    title = extracted.get("title") or "AI 应用开发实习"
    message = (
        f"您好，我是智能科学与技术本科在读，正在寻找 AI 应用开发/Agent 开发方向实习机会。"
        f"我近期做过本地优先的 AI 伴侣与 RAG 知识库项目，实践了 {top_skills} 等内容。"
        f"看了{company}的「{title}」岗位，感觉和我的项目经历比较匹配，希望有机会进一步沟通。"
    )
    email = (
        f"您好，\n\n"
        f"我想投递「{title}」岗位。我是智能科学与技术本科在读，近期围绕 AI 伴侣与 RAG 知识库应用实践了 "
        f"{top_skills}，也关注检索效果、来源可追溯和工程可复验。\n\n"
        f"我对贵岗位中 AI 应用落地、Agent/RAG 或大模型应用后端相关方向很感兴趣。"
        f"如果方便，希望有机会进一步沟通。\n\n"
        f"谢谢。"
    )
    return {"message": message, "email": email}


def build_interview_review(job: dict[str, Any] | None, source_text: str) -> dict[str, Any]:
    extracted = {}
    if job:
        extracted = job.get("extracted") or {}
    title = (job or {}).get("title") or extracted.get("title") or "目标岗位"
    weak_questions = extract_questions(source_text)
    plan = {
        "title": title,
        "three_day_plan": [
            "第 1 天：复盘 JD 技术点，补齐 RAG/Agent/FastAPI 基础表达。",
            "第 2 天：围绕项目经历做追问模拟，重点讲清问题、方案、指标和边界。",
            "第 3 天：做一轮完整模拟面试，整理没答好的问题和最终回答稿。",
        ],
        "seven_day_plan": [
            "第 1-2 天：梳理 JD 和公司业务，准备自我介绍与项目介绍。",
            "第 3-4 天：补 RAG、向量检索、Prompt、Agent 工具调用题。",
            "第 5 天：补 Python/FastAPI/SQLite/测试部署基础题。",
            "第 6 天：模拟项目深挖和行为面试。",
            "第 7 天：查漏补缺，准备反问和面试复盘模板。",
        ],
    }
    questions = weak_questions or [
        "请介绍一下你的 RAG 项目是如何做文档切分和检索评测的？",
        "如果召回结果不准确，你会从哪些方向排查？",
        "Agent 工具调用和普通函数调用有什么区别？",
        "FastAPI 项目里你如何设计接口、错误处理和测试？",
    ]
    markdown = render_review_markdown(title, source_text, questions)
    return {"plan": plan, "questions": questions, "markdown": markdown}


def extract_questions(text: str) -> list[str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    questions = []
    for line in lines:
        if "?" in line or "？" in line or line.startswith(("Q:", "问：")):
            questions.append(line[:160])
    return questions[:12]


def render_review_markdown(title: str, source_text: str, questions: list[str]) -> str:
    question_lines = "\n".join(f"- {question}" for question in questions)
    source_summary = normalize(source_text)[:500] or "暂无转写文本，建议先补充面试记录。"
    return (
        f"# {title} 面试复盘\n\n"
        f"## 面试记录摘要\n\n{source_summary}\n\n"
        f"## 需要重点补的问题\n\n{question_lines}\n\n"
        f"## 下一步\n\n"
        f"- 把每个没答好的问题改写成 1 分钟回答稿。\n"
        f"- 对项目问题补充背景、个人职责、技术取舍和可验证结果。\n"
        f"- 下一轮模拟面试优先追问 RAG、Agent、FastAPI 和数据库基础。\n"
    )
