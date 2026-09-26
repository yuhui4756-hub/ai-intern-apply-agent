from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.agent_contract import AgentContractError, validate_planner_output
from app.services.analyzer import rule_extract_jd, score_job
from app.services.conversation import classify_conversation


DEFAULT_CASES = Path(__file__).with_name("agent_quality_cases.jsonl")
EVAL_VERSION = "v0.9"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"评测样本第 {line_no} 行不是有效 JSON：{exc.msg}") from exc
        if not isinstance(case, dict) or not case.get("id") or not case.get("suite"):
            raise ValueError(f"评测样本第 {line_no} 行缺少 id 或 suite。")
        cases.append(case)
    if not cases:
        raise ValueError("评测集为空。")
    return cases


def check_equal(checks: list[dict[str, Any]], label: str, actual: Any, expected: Any) -> None:
    checks.append({"label": label, "passed": actual == expected, "expected": expected, "actual": actual})


def check_contains(checks: list[dict[str, Any]], label: str, values: Any, expected: list[str]) -> None:
    actual = list(values) if isinstance(values, list) else []
    missing = [item for item in expected if item not in actual]
    checks.append({"label": label, "passed": not missing, "expected": expected, "actual": actual, "missing": missing})


def check_excludes(checks: list[dict[str, Any]], label: str, values: Any, unwanted: list[str]) -> None:
    actual = list(values) if isinstance(values, list) else []
    present = [item for item in unwanted if item in actual]
    checks.append({"label": label, "passed": not present, "expected_absent": unwanted, "actual": actual, "present": present})


def evaluate_jd_rule(case: dict[str, Any]) -> dict[str, Any]:
    payload = case.get("input") if isinstance(case.get("input"), dict) else {}
    expected = case.get("expect") if isinstance(case.get("expect"), dict) else {}
    text = str(payload.get("jd_text") or "")
    extracted = rule_extract_jd(text)
    scoring = score_job(
        extracted,
        text,
        resume_text=str(payload.get("resume_text") or ""),
        preferences=payload.get("preferences") if isinstance(payload.get("preferences"), dict) else {},
    )
    checks: list[dict[str, Any]] = []
    if "required_skills_contains" in expected:
        check_contains(checks, "required_skills_contains", extracted["required_skills"], list(expected["required_skills_contains"]))
    if "risk_signals_contains" in expected:
        check_contains(checks, "risk_signals_contains", extracted["risk_signals"], list(expected["risk_signals_contains"]))
    if "salary_text" in expected:
        check_equal(checks, "salary_text", extracted["salary_text"], expected["salary_text"])
    if "recommendation" in expected:
        check_equal(checks, "recommendation", scoring["recommendation"], expected["recommendation"])
    if "recommendation_not" in expected:
        checks.append(
            {
                "label": "recommendation_not",
                "passed": scoring["recommendation"] != expected["recommendation_not"],
                "expected_not": expected["recommendation_not"],
                "actual": scoring["recommendation"],
            }
        )
    if "risk_level" in expected:
        check_equal(checks, "risk_level", scoring["risk_level"], expected["risk_level"])
    if "minimum_score" in expected:
        checks.append(
            {
                "label": "minimum_score",
                "passed": scoring["score"] >= int(expected["minimum_score"]),
                "expected_minimum": int(expected["minimum_score"]),
                "actual": scoring["score"],
            }
        )
    if "salary_daily_range" in expected:
        check_equal(checks, "salary_daily_range", scoring["score_breakdown"]["salary_daily_range"], expected["salary_daily_range"])
    if "alternative_requirement_groups_contains" in expected:
        check_contains(
            checks,
            "alternative_requirement_groups_contains",
            scoring["alternative_requirement_groups"],
            list(expected["alternative_requirement_groups_contains"]),
        )
    if "missing_skills_excludes" in expected:
        check_excludes(checks, "missing_skills_excludes", scoring["missing_skills"], list(expected["missing_skills_excludes"]))
    return {"checks": checks, "observed": {"extracted": extracted, "scoring": scoring}}


def evaluate_conversation(case: dict[str, Any]) -> dict[str, Any]:
    payload = case.get("input") if isinstance(case.get("input"), dict) else {}
    expected = case.get("expect") if isinstance(case.get("expect"), dict) else {}
    result = classify_conversation(
        str(payload.get("text") or ""),
        payload.get("job") if isinstance(payload.get("job"), dict) else None,
    )
    checks: list[dict[str, Any]] = []
    for key in ("message_type", "reply_gate", "action_required"):
        if key in expected:
            check_equal(checks, key, result.get(key), expected[key])
    if "risk_flags_contains" in expected:
        flags = "；".join(str(item) for item in result.get("risk_flags") or [])
        for expected_fragment in expected["risk_flags_contains"]:
            checks.append(
                {
                    "label": "risk_flags_contains",
                    "passed": str(expected_fragment) in flags,
                    "expected_fragment": expected_fragment,
                    "actual": flags,
                }
            )
    return {"checks": checks, "observed": result}


def evaluate_agent_contract(case: dict[str, Any]) -> dict[str, Any]:
    payload = case.get("input") if isinstance(case.get("input"), dict) else {}
    expected = case.get("expect") if isinstance(case.get("expect"), dict) else {}
    checks: list[dict[str, Any]] = []
    try:
        result = validate_planner_output(payload.get("plan"))
    except AgentContractError as exc:
        check_equal(checks, "status", "contract_error", expected.get("status"))
        return {"checks": checks, "observed": {"error": exc.summary}}

    status = "valid" if not result.violations else "violations"
    check_equal(checks, "status", status, expected.get("status"))
    if "violation_codes" in expected:
        check_equal(checks, "violation_codes", [item.code for item in result.violations], expected["violation_codes"])
    if "tool_names" in expected:
        check_equal(checks, "tool_names", [item.name for item in result.tool_calls], expected["tool_names"])
    return {
        "checks": checks,
        "observed": {
            "tool_names": [item.name for item in result.tool_calls],
            "violation_codes": [item.code for item in result.violations],
        },
    }


SUITE_RUNNERS = {
    "jd_rule": evaluate_jd_rule,
    "conversation": evaluate_conversation,
    "agent_contract": evaluate_agent_contract,
}


def run_evaluation(case_path: Path = DEFAULT_CASES) -> dict[str, Any]:
    results = []
    for case in load_cases(case_path):
        suite = str(case["suite"])
        runner = SUITE_RUNNERS.get(suite)
        if not runner:
            raise ValueError(f"未知评测套件：{suite}")
        outcome = runner(case)
        checks = outcome["checks"]
        results.append(
            {
                "id": str(case["id"]),
                "suite": suite,
                "passed": all(bool(item["passed"]) for item in checks),
                "checks": checks,
                "observed": outcome["observed"],
            }
        )

    suites: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "passed": 0, "failed": 0})
    for result in results:
        suite = suites[result["suite"]]
        suite["total"] += 1
        suite["passed"] += int(result["passed"])
        suite["failed"] += int(not result["passed"])
    total = len(results)
    passed = sum(int(result["passed"]) for result in results)
    return {
        "evaluation_version": EVAL_VERSION,
        "dataset": str(case_path.name),
        "scope": "offline synthetic/de-identified regression checks; not a real-platform accuracy claim",
        "summary": {"total": total, "passed": passed, "failed": total - passed},
        "suites": dict(suites),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local job-Agent quality regression suite.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="JSONL case file")
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when any case fails")
    args = parser.parse_args()

    report = run_evaluation(args.cases)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 1 if args.strict and report["summary"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
