from __future__ import annotations

"""Strict, versioned contracts for the chat Agent's planner output.

The planner is allowed to choose among a small set of already-authorized
tools. This module validates its JSON before the harness sees any call. It is
deliberately separate from the execution code so the same rules can be used by
offline evaluations and regression tests.
"""

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


AGENT_CONTRACT_VERSION = "agent-plan-v1"

ROLE_OPTIONS = ("AI 应用开发实习", "Agent 开发实习", "AI 后端实习", "RAG 开发实习")
CITY_OPTIONS = ("北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京")


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class PlannerToolCall(ContractModel):
    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class PlannerOutput(ContractModel):
    plan: str = Field(default="", max_length=220)
    tool_calls: list[PlannerToolCall] = Field(default_factory=list, max_length=3)
    response: str = Field(default="", max_length=1600)
    task_summary: str = Field(default="", max_length=300)


class EmptyArguments(ContractModel):
    pass


class JobIdArguments(ContractModel):
    job_id: int | None = Field(default=None, gt=0)


class SelectJobArguments(ContractModel):
    job_id: int = Field(gt=0)


class ListJobsArguments(ContractModel):
    limit: int = Field(default=8, ge=1, le=20)
    status: str = Field(default="", max_length=30)
    recommendation: str = Field(default="", max_length=30)
    risk_level: str = Field(default="", max_length=30)


class SearchJobsArguments(ContractModel):
    role: Literal["AI 应用开发实习", "Agent 开发实习", "AI 后端实习", "RAG 开发实习"] | None = None
    city: Literal["北京", "上海", "广州", "深圳", "杭州", "重庆", "成都", "南京"] | None = None
    min_salary_per_day: int | None = Field(default=None, ge=0, le=10_000)


class InspectCurrentPageArguments(ContractModel):
    full_page: bool = False


class SearchMemoryArguments(ContractModel):
    query: str = Field(min_length=1, max_length=240)


TOOL_ARGUMENT_MODELS: dict[str, type[ContractModel]] = {
    "list_jobs": ListJobsArguments,
    "select_job": SelectJobArguments,
    "inspect_job": JobIdArguments,
    "search_jobs": SearchJobsArguments,
    "inspect_current_page": InspectCurrentPageArguments,
    "prepare_application": JobIdArguments,
    "prepare_communication": JobIdArguments,
    "prepare_interview": JobIdArguments,
    "check_resume": JobIdArguments,
    "tailor_resume": JobIdArguments,
    "check_unread": EmptyArguments,
    "search_memory": SearchMemoryArguments,
    "save_job_evidence": JobIdArguments,
}


@dataclass(frozen=True)
class ContractViolation:
    code: str
    summary: str
    tool_name: str = ""


@dataclass(frozen=True)
class ValidatedToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ValidatedPlannerOutput:
    plan: str
    tool_calls: list[ValidatedToolCall]
    response: str
    task_summary: str
    violations: list[ContractViolation]


class AgentContractError(ValueError):
    """The planner JSON itself could not satisfy the top-level contract."""

    def __init__(self, summary: str):
        super().__init__(summary)
        self.summary = summary


def validation_summary(error: ValidationError, scope: str) -> str:
    """Return a bounded diagnostic without echoing untrusted model content."""
    details = []
    for item in error.errors()[:3]:
        location = ".".join(str(part) for part in item.get("loc") or []) or "字段"
        kind = str(item.get("type") or "invalid")
        details.append(f"{location}:{kind}")
    suffix = "；".join(details) or "字段不符合要求"
    return f"{scope}不符合 {AGENT_CONTRACT_VERSION} 契约（{suffix}）。"


def validate_planner_output(value: Any) -> ValidatedPlannerOutput:
    """Validate planner JSON and retain only calls allowed by the harness.

    A malformed top-level object is rejected as a whole. A well-formed plan
    may contain individual unknown tools or invalid argument objects; those
    violations are returned separately so the harness can request a repair
    before it decides whether any call may run.
    """
    try:
        output = PlannerOutput.model_validate(value)
    except ValidationError as exc:
        raise AgentContractError(validation_summary(exc, "模型规划输出")) from exc

    calls: list[ValidatedToolCall] = []
    violations: list[ContractViolation] = []
    for call in output.tool_calls:
        argument_model = TOOL_ARGUMENT_MODELS.get(call.name)
        if not argument_model:
            violations.append(
                ContractViolation(
                    code="unknown_tool",
                    summary="模型请求了不在受限工具清单中的动作，已拒绝。",
                    tool_name=call.name[:64],
                )
            )
            continue
        try:
            arguments = argument_model.model_validate(call.arguments).model_dump(exclude_none=True)
        except ValidationError as exc:
            violations.append(
                ContractViolation(
                    code="invalid_arguments",
                    summary=validation_summary(exc, f"工具 {call.name} 参数"),
                    tool_name=call.name,
                )
            )
            continue
        calls.append(ValidatedToolCall(name=call.name, arguments=arguments))
    return ValidatedPlannerOutput(
        plan=output.plan,
        tool_calls=calls,
        response=output.response,
        task_summary=output.task_summary,
        violations=violations,
    )


def planner_contract_description() -> str:
    """Short schema guidance that is safe to put in the planner system prompt."""
    return (
        f"输出必须满足 {AGENT_CONTRACT_VERSION}：只允许 plan、tool_calls、response、task_summary 四个字段；"
        "每个 tool_call 只能含 name 和 arguments，arguments 只能包含该工具列出的字段。"
        "未知字段、未知工具、URL、选择器、命令、SQL、文件路径和提交动作都会被 harness 拒绝。"
    )
