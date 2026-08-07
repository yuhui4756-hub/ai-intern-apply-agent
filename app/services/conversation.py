from __future__ import annotations

import re
from typing import Any

from .job_fetcher import normalize_visible_text


INTERVIEW_RE = re.compile(r"(面试|笔试|约.*时间|时间方便|视频面|电话面|腾讯会议|飞书会议|牛客|面一下)")
RESUME_REQUEST_RE = re.compile(
    r"((发|发送|投递|上传|提交|补充|提供|给|看下|看看).{0,12}(简历|附件))|"
    r"((简历|附件).{0,12}(发|发送|投递|上传|提交|补充|提供|给|看下|看看))"
)
SENSITIVE_RE = re.compile(r"(手机号|电话|微信|邮箱|身份证|银行卡|押金|培训费|贷款|offer|地址|到公司|线下)")
IRRELEVANT_RE = re.compile(r"(周末玩|游戏|恋爱|私事|无关)")
JOB_INFO_RE = re.compile(r"(工作内容|技术栈|实习周期|到岗|每周|薪资|导师|转正|远程|base|地点|流程|要求|接受大二)")
HIGH_RISK_RE = re.compile(r"(押金|培训费|贷款)")
TARGET_RE = re.compile(r"(AI|人工智能|大模型|LLM|RAG|Agent|智能体|Python|FastAPI|后端|应用开发|算法|模型|知识库)", re.IGNORECASE)
BROADCAST_RE = re.compile(
    r"(系统自动|系统推荐|群发|批量|急招职位|向您推荐|推荐.*职位|"
    r"期待您.{0,6}投递|期待你的投递|欢迎投递|"
    r"有兴趣.{0,12}(投递|发简历|聊一聊|沟通)|感兴趣.{0,12}(投递|发简历|聊一聊|沟通)|"
    r"正在招聘.{0,30}(期待|欢迎|投递|发简历)|可以直接发简历|可直接发简历)"
)
DIRECT_QUESTION_RE = re.compile(r"(请问|是否|能否|方便|吗[？?]?|么[？?]?|什么时间|什么时候|想了解|考虑吗|有兴趣吗)")
UI_NOISE_LINE_RE = re.compile(
    r"^(发简历|交换手机号|交换微信号|再考虑一下|发送|去使用>?|"
    r"请输入文字.*发送|我们为您生成了.*打招呼语.*|不支持此消息查看.*查看消息内容[!！]?)$"
)
TIMESTAMP_LINE_RE = re.compile(r"^\d{1,2}:\d{2}$")


def compact_conversation_text(text: str, limit: int = 12000) -> str:
    return prepare_conversation_text(text, clean_limit=limit)["clean_text"]


def prepare_conversation_text(text: str, clean_limit: int = 12000, raw_limit: int = 20000) -> dict[str, Any]:
    raw_text = normalize_visible_text(text or "")
    kept_lines: list[str] = []
    ignored_lines: list[str] = []
    for line in raw_text.splitlines():
        clean_line = line.strip()
        if is_conversation_noise_line(clean_line):
            ignored_lines.append(clean_line)
        else:
            kept_lines.append(line)

    clean_text = "\n".join(kept_lines).strip()
    if len(raw_text) > raw_limit:
        raw_text = raw_text[:raw_limit]
    if len(clean_text) > clean_limit:
        clean_text = clean_text[-clean_limit:]

    return {
        "raw_text": raw_text,
        "clean_text": clean_text,
        "ignored_lines": ignored_lines[:80],
        "raw_length": len(raw_text),
        "clean_length": len(clean_text),
    }


def is_conversation_noise_line(line: str) -> bool:
    clean = line.strip()
    return not clean or bool(UI_NOISE_LINE_RE.search(clean) or TIMESTAMP_LINE_RE.search(clean))


def classify_conversation(text: str, job: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = compact_conversation_text(text)
    latest = latest_message_excerpt(clean)
    risk_flags: list[str] = []
    reply_signals = conversation_reply_signals(clean, job)

    sensitive_or_resume = bool(RESUME_REQUEST_RE.search(clean) or SENSITIVE_RE.search(clean))
    high_risk = bool(HIGH_RISK_RE.search(clean))
    if sensitive_or_resume:
        risk_flags.append("涉及简历、联系方式、面试时间、薪资谈判或敏感信息，需要用户确认")
    if high_risk:
        risk_flags.append("命中高风险招聘信号")

    if high_risk:
        message_type = "需要我处理"
        action_required = True
        reason = "对话命中押金、培训费或贷款等高风险招聘信号。"
        draft = ""
    elif INTERVIEW_RE.search(clean):
        message_type = "面试邀请"
        action_required = True
        reason = "识别到 HR 提到面试、笔试或约时间。"
        draft = ""
    elif IRRELEVANT_RE.search(clean):
        message_type = "无关内容"
        action_required = True
        reason = "对话疑似偏离岗位沟通范围。"
        draft = ""
    elif should_skip_reply(reply_signals):
        message_type = "无需回复"
        action_required = False
        reason = "；".join(reply_signals["reasons"])
        draft = ""
        risk_flags.extend(reply_signals["risk_flags"])
    elif sensitive_or_resume:
        message_type = "需要我处理"
        action_required = True
        reason = "对话中包含必须转人工确认的内容。"
        draft = ""
    else:
        message_type = "岗位沟通"
        action_required = False
        reason = "对话仍在岗位、求职或流程范围内，可生成待确认草稿。"
        draft = build_safe_reply(clean, job)

    return {
        "message_type": message_type,
        "summary": latest,
        "action_required": action_required,
        "reason": reason,
        "draft_message": draft,
        "risk_flags": risk_flags,
        "reply_gate": reply_signals["action"],
        "reply_gate_reasons": reply_signals["reasons"],
    }


def conversation_reply_signals(text: str, job: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = compact_conversation_text(text)
    job_text = " ".join(
        str((job or {}).get(key) or "")
        for key in ["title", "company", "jd_text", "summary", "extracted_json", "recommendation", "match_level"]
    )
    has_job = bool(job)
    is_broadcast = bool(BROADCAST_RE.search(clean))
    target_related = bool(TARGET_RE.search(clean) or TARGET_RE.search(job_text))
    direct_question = bool(DIRECT_QUESTION_RE.search(clean))
    sensitive_or_resume = bool(RESUME_REQUEST_RE.search(clean) or SENSITIVE_RE.search(clean))
    reasons: list[str] = []
    risk_flags: list[str] = []
    action = "allow"

    if is_broadcast:
        reasons.append("疑似 HR 群发或系统推荐消息")
        risk_flags.append("疑似群发/系统推荐，默认不进入自动回复")
    if not has_job:
        reasons.append("未匹配到已保存目标岗位")
    if not target_related:
        reasons.append("未出现 AI 应用、Agent、Python、后端等目标方向信号")

    if sensitive_or_resume and not is_broadcast:
        action = "manual"
    elif is_broadcast and not direct_question:
        action = "skip"
    elif not has_job and not target_related:
        action = "manual" if direct_question else "skip"
    elif not has_job:
        action = "manual"
    elif not target_related:
        action = "manual"

    return {
        "action": action,
        "reasons": reasons,
        "risk_flags": risk_flags,
        "is_broadcast": is_broadcast,
        "target_related": target_related,
        "direct_question": direct_question,
    }


def should_skip_reply(signals: dict[str, Any]) -> bool:
    return str(signals.get("action") or "") == "skip"


def latest_message_excerpt(text: str, limit: int = 260) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return "未读取到有效对话文本。"
    return " / ".join(lines[-6:])[:limit]


def build_safe_reply(text: str, job: dict[str, Any] | None = None) -> str:
    title = (job or {}).get("title") or "这个岗位"
    if JOB_INFO_RE.search(text):
        return (
            f"您好，感谢回复。我对「{title}」比较感兴趣，想进一步了解一下这个岗位的主要工作内容、"
            "技术栈、实习周期和每周到岗要求。也想请问团队是否会有导师或比较明确的培养安排？"
        )
    return (
        f"您好，感谢回复。我想继续了解「{title}」的岗位内容、技术栈和实习安排，"
        "如果方便的话，想请您介绍一下主要工作和后续流程。"
    )
