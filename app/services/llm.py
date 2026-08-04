from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from ..db import connect, utc_now


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return max(1, int(chinese_chars * 1.2 + other_chars / 4))


@dataclass
class LLMResult:
    content: str
    input_tokens: int
    output_tokens: int
    usage_source: str


class OpenAICompatibleClient:
    def __init__(self, profile: dict[str, Any], task_type: str):
        self.profile = profile
        self.task_type = task_type
        self.base_url = (profile.get("base_url") or os.environ.get("OPENAI_COMPATIBLE_BASE_URL") or "").rstrip("/")
        self.model = profile.get("model") or os.environ.get("OPENAI_COMPATIBLE_MODEL") or ""
        api_key_env = profile.get("api_key_env") or "OPENAI_COMPATIBLE_API_KEY"
        self.api_key = os.environ.get(api_key_env) or ""
        self.temperature = float(profile.get("temperature") or 0.2)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    def complete(self, messages: list[dict[str, str]], response_format: dict[str, str] | None = None) -> LLMResult:
        if not self.configured:
            raise RuntimeError("OpenAI-compatible profile is not fully configured.")

        url = f"{self.base_url}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        input_text = "\n".join(message.get("content", "") for message in messages)
        with httpx.Client(timeout=45) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"].get("content", "")
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or estimate_tokens(input_text))
        output_tokens = int(usage.get("completion_tokens") or estimate_tokens(content))
        usage_source = "api" if usage else "estimated"
        return LLMResult(content=content, input_tokens=input_tokens, output_tokens=output_tokens, usage_source=usage_source)

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        result = self.complete(messages, response_format={"type": "json_object"})
        self.log_call(result, "success", "")
        try:
            return json.loads(result.content)
        except json.JSONDecodeError as exc:
            self.log_error(f"JSON parse failed: {exc}")
            raise

    def complete_text(self, messages: list[dict[str, str]]) -> str:
        result = self.complete(messages)
        self.log_call(result, "success", "")
        return result.content

    def log_call(self, result: LLMResult, status: str, error_message: str) -> None:
        input_cost = float(self.profile.get("input_cost_per_million") or 0)
        output_cost = float(self.profile.get("output_cost_per_million") or 0)
        estimated_cost = (result.input_tokens / 1_000_000 * input_cost) + (result.output_tokens / 1_000_000 * output_cost)
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO model_call_logs (
                    task_type, model_profile, model_name, input_tokens, output_tokens,
                    total_tokens, estimated_cost, usage_source, status, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.task_type,
                    self.profile.get("name") or "",
                    self.model,
                    result.input_tokens,
                    result.output_tokens,
                    result.input_tokens + result.output_tokens,
                    estimated_cost,
                    result.usage_source,
                    status,
                    error_message,
                    utc_now(),
                ),
            )

    def log_error(self, error_message: str) -> None:
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO model_call_logs (
                    task_type, model_profile, model_name, status, error_message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.task_type,
                    self.profile.get("name") or "",
                    self.model,
                    "error",
                    error_message[:500],
                    utc_now(),
                ),
            )


def route_profile(task_type: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT p.*
            FROM model_routes r
            LEFT JOIN model_profiles p ON p.id = r.profile_id
            WHERE r.task_type = ?
            """,
            (task_type,),
        ).fetchone()
        if row and row["id"] is not None:
            return {key: row[key] for key in row.keys()}
        fallback = conn.execute("SELECT * FROM model_profiles WHERE is_default = 1 LIMIT 1").fetchone()
        return {key: fallback[key] for key in fallback.keys()} if fallback else None


def client_for_task(task_type: str) -> OpenAICompatibleClient | None:
    profile = route_profile(task_type)
    if not profile:
        return None
    return OpenAICompatibleClient(profile, task_type)
