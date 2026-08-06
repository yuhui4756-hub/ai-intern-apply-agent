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

    if RESUME_REQUEST_RE.search(clean) or SENSITIVE_RE.search(clean):
        risk_flags.append("涉及简历、联系方式、面试时间、薪资谈判或敏感信息，需要用户确认")
    if HIGH_RISK_RE.search(clean):
        risk_flags.append("命中高风险招聘信号")

    if INTERVIEW_RE.search(clean):
        message_type = "面试邀请"
        action_required = True
        reason = "识别到 HR 提到面试、笔试或约时间。"
        draft = ""
    elif risk_flags:
        message_type = "需要我处理"
        action_required = True
        reason = "对话中包含必须转人工确认的内容。"
        draft = ""
    elif IRRELEVANT_RE.search(clean):
        message_type = "无关内容"
        action_required = True
        reason = "对话疑似偏离岗位沟通范围。"
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
    }


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
