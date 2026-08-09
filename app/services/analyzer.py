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

AI_CODING_ASSISTANT_MARKERS = (
    "claude code",
    "codex",
    "cursor",
    "copilot",
    "windsurf",
    "codeium",
    "trae",
    "ai coding",
    "ai编程",
    "ai 编程",
    "aigc工具",
    "vibe coding",
)
ALTERNATIVE_REQUIREMENT_MARKERS = (
    "至少一种",
    "至少一项",
    "至少一门",
    "以下之一",
    "其中一种",
    "任意一种",
    "任一种",
)
HIGH_BAR_REQUIREMENT_MARKERS = (
    "深入掌握",
    "精通",
    "扎实",
    "生产级",
    "高并发",
    "年以上",
    "全栈开发经验",
)

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京", "苏州", "厦门", "武汉", "长沙"]
UNKNOWN_VALUES = {"?", "？", "未知", "未填写", "未填", "无", "暂无", "N/A", "n/a", "None", "null"}
SALARY_NUMBER = r"\d+(?:\.\d+)?"
SALARY_RANGE = r"(?:-|~|～|至|到|—|–|－)"
PLATFORM_SAFETY_NOTICE_MARKERS = (
    "猎聘温馨提示",
    "BOSS直聘温馨提示",
    "BOSS直聘安全提示",
    "实习僧温馨提示",
    "智联招聘温馨提示",
    "前程无忧温馨提示",
    "本平台招聘方不向求职者提供任何收费服务",
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_platform_safety_notice(text: str) -> str:
    """Remove generic platform fraud warnings that are not part of the job JD."""
    value = text or ""
    matches = [value.find(marker) for marker in PLATFORM_SAFETY_NOTICE_MARKERS if value.find(marker) >= 0]
    if not matches:
        return value
    return value[: min(matches)].rstrip()


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


def daily_salary_bounds(salary_text: str) -> tuple[int, int] | None:
    """Return the stated daily salary range without guessing from monthly pay."""
    text = clean_salary_text(salary_text).lower()
    if not text or not any(marker in text for marker in ("/天", "/日", "日薪", "元天", "元日")):
        return None
    numbers = [float(item) for item in re.findall(SALARY_NUMBER, text)]
    if not numbers:
        return None
    return int(min(numbers)), int(max(numbers))


def salary_score(
    salary_text: str,
    min_salary_per_day: int | None = None,
    target_salary_per_day: int | None = None,
) -> int:
    if not salary_text:
        return 8
    daily_bounds = daily_salary_bounds(salary_text)
    if daily_bounds and min_salary_per_day:
        low, high = daily_bounds
        if low < min_salary_per_day:
            return 0
        if target_salary_per_day:
            if low >= target_salary_per_day:
                return 15
            if high >= target_salary_per_day:
                return 13
            return 10
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


def skill_is_non_blocking(skill: str) -> bool:
    lowered = normalize(skill).lower()
    return any(marker in lowered for marker in AI_CODING_ASSISTANT_MARKERS)


def alternative_skill_groups(required_skills: list[str], jd_text: str) -> list[list[str]]:
    """Find requirement groups where the JD explicitly accepts any one listed skill."""
    groups: list[list[str]] = []
    for sentence in re.split(r"[。；;\n]", jd_text or ""):
        lowered = sentence.lower()
        marker = next((value for value in ALTERNATIVE_REQUIREMENT_MARKERS if value in sentence), "")
        if not marker:
            continue
        marker_index = sentence.index(marker)
        # "Python/Go 中的至少一种" puts its list before the marker, while
        # "至少精通以下之一：Python/Go" puts it after the marker.
        scope = sentence[marker_index + len(marker) :] if marker == "以下之一" else sentence[:marker_index]
        scope_lower = scope.lower()
        members = [skill for skill in required_skills if skill.lower() in scope_lower]
        if len(members) < 2:
            continue
        normalized_members = list(dict.fromkeys(members))
        if normalized_members not in groups:
            groups.append(normalized_members)
    return groups


def score_job(
    extracted: dict[str, Any],
    jd_text: str,
    resume_text: str = "",
    preferences: dict[str, Any] | None = None,
    matching_evidence: str = "简历正文",
) -> dict[str, Any]:
    text = jd_text or ""
    preferences = preferences if isinstance(preferences, dict) else {}
    required_skills = list(dict.fromkeys(extracted.get("required_skills") or find_skills(text)))
    resume_lower = (resume_text or "").lower()
    matched_skills = [skill for skill in required_skills if skill.lower() in resume_lower]
    non_blocking_skills = [skill for skill in required_skills if skill_is_non_blocking(skill)]
    alternative_groups = alternative_skill_groups(required_skills, text)
    alternative_members = {skill for group in alternative_groups for skill in group}
    required_units = [skill for skill in required_skills if skill not in alternative_members and skill not in non_blocking_skills]
    matched_units = sum(1 for skill in required_units if skill in matched_skills)
    missing_skills = [skill for skill in required_units if skill not in matched_skills]
    alternative_labels: list[str] = []
    for group in alternative_groups:
        active_group = [skill for skill in group if skill not in non_blocking_skills]
        if len(active_group) < 2:
            continue
        group_matches = [skill for skill in active_group if skill in matched_skills]
        label = " / ".join(active_group) + "（满足其一）"
        alternative_labels.append(label)
        if group_matches:
            matched_units += 1
        else:
            missing_skills.append(label)

    total_requirement_units = len(required_units) + len(alternative_labels)
    technical_coverage = matched_units / total_requirement_units if total_requirement_units else 0
    tech_score = min(35, 8 + int(27 * technical_coverage))
    fit_notes: list[str] = []
    if total_requirement_units >= 8 and technical_coverage < 0.4:
        fit_notes.append(
            f"岗位列出 {total_requirement_units} 项技术要求，当前简历可证实匹配 {matched_units} 项，技术门槛较高。"
        )
    high_bar_markers = [marker for marker in HIGH_BAR_REQUIREMENT_MARKERS if marker.lower() in text.lower()]
    if high_bar_markers and total_requirement_units >= 8 and technical_coverage < 0.65:
        penalty = 7 if total_requirement_units >= 15 and technical_coverage < 0.5 else 4
        tech_score = max(0, tech_score - penalty)
        fit_notes.append("岗位明确要求" + "、".join(high_bar_markers[:3]) + "等高门槛能力，技术分已保守下调。")
    growth_score = min(25, 8 + sum(3 for keyword in GOOD_GROWTH_KEYWORDS if keyword.lower() in text.lower()))
    min_salary_per_day = preferences.get("min_salary_per_day")
    target_salary_per_day = preferences.get("target_salary_per_day")
    min_salary_per_day = min_salary_per_day if isinstance(min_salary_per_day, int) and min_salary_per_day > 0 else None
    target_salary_per_day = target_salary_per_day if isinstance(target_salary_per_day, int) and target_salary_per_day > 0 else None
    salary_text = extracted.get("salary_text") or ""
    daily_bounds = daily_salary_bounds(salary_text)
    salary_points = salary_score(salary_text, min_salary_per_day, target_salary_per_day)
    preferred_cities = [str(city).strip() for city in preferences.get("cities", []) if str(city).strip()]
    preferred_cities = preferred_cities or ["北京", "上海", "广州", "深圳", "杭州", "重庆", "成都"]
    remote_policy = str(preferences.get("remote_policy") or "接受")
    preference_notes: list[str] = []
    is_remote = "远程" in text
    remote_only_mismatch = remote_policy == "仅远程" and bool(extracted.get("city")) and not is_remote
    if remote_policy == "仅远程":
        location_score = 10 if is_remote else 0
        if remote_only_mismatch:
            preference_notes.append("岗位标注为线下城市，和仅远程偏好不符")
    elif remote_policy == "不接受" and is_remote and not extracted.get("city"):
        location_score = 0
        preference_notes.append("岗位仅标注远程，和远程偏好不符")
    else:
        location_score = 10 if (
            extracted.get("city") in preferred_cities
            or not extracted.get("city")
            or is_remote
        ) else 6
    salary_below_minimum = bool(
        daily_bounds and min_salary_per_day and daily_bounds[0] < min_salary_per_day
    )
    if salary_below_minimum:
        preference_notes.append(
            f"薪资最低 {daily_bounds[0]} 元/天，低于设置底线 {min_salary_per_day} 元/天"
        )
    desired_days = re.search(r"([1-7])\s*天", str(preferences.get("internship_days") or ""))
    stated_days = re.search(r"(?:每周|一周)\s*([1-7])\s*天", text)
    if desired_days and stated_days and int(stated_days.group(1)) > int(desired_days.group(1)):
        preference_notes.append(
            f"每周到岗 {stated_days.group(1)} 天，高于偏好（{preferences.get('internship_days')}）"
        )

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
    elif salary_below_minimum or remote_only_mismatch:
        level = "低匹配"
        recommendation = "跳过"
        risk_level = "谨慎" if caution_signals else "低"
        skip_reason = preference_notes[0]
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
        "missing_skills": missing_skills,
        "alternative_requirement_groups": alternative_labels,
        "non_blocking_skills": non_blocking_skills,
        "fit_notes": fit_notes,
        "matching_evidence": matching_evidence,
        "caution_signals": caution_signals,
        "risk_signals": risk_signals,
        "preference_notes": preference_notes,
        "score_breakdown": {
            "technical": tech_score,
            "growth": growth_score,
            "salary": salary_points,
            "location_time": location_score,
            "company_risk": risk_score,
            "salary_daily_range": list(daily_bounds) if daily_bounds else [],
            "required_units": total_requirement_units,
            "matched_requirement_units": matched_units,
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
