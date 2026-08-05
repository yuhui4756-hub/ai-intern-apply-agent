from __future__ import annotations

import os
import re
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"


def load_local_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_local_env()


def read_env_values(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_value(key: str, value: str, path: Path = ENV_PATH) -> None:
    key = key.strip()
    if not key:
        raise ValueError("Environment key cannot be empty.")

    lines: list[str] = []
    found = False
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    rendered = f"{key}={value}"
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        existing_key = stripped.split("=", 1)[0].strip()
        if existing_key == key:
            lines[index] = rendered
            found = True
            break

    if not found:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(rendered)

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    os.environ[key] = value


def mask_secret(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * 8}{value[-4:]}"


def looks_masked(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped) and set(stripped) <= {"*"}


def suggest_api_key_env(name: str = "", base_url: str = "") -> str:
    source = f"{name} {base_url}".lower()
    if "deepseek" in source:
        return "DEEPSEEK_API_KEY"
    if "openai" in source or "api.openai.com" in source:
        return "OPENAI_API_KEY"
    if "anthropic" in source:
        return "ANTHROPIC_API_KEY"
    if "qwen" in source or "dashscope" in source or "aliyun" in source:
        return "QWEN_API_KEY"
    if "siliconflow" in source:
        return "SILICONFLOW_API_KEY"
    if "localhost" in source or "127.0.0.1" in source:
        return "LOCAL_PROXY_API_KEY"

    token = re.sub(r"[^A-Za-z0-9]+", "_", name or "OPENAI_COMPATIBLE").strip("_").upper()
    if not token:
        token = "OPENAI_COMPATIBLE"
    if not token.endswith("API_KEY"):
        token = f"{token}_API_KEY"
    return token


def data_dir() -> Path:
    path = ROOT_DIR / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    configured = os.environ.get("APP_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return data_dir() / "app.sqlite3"


TASK_TYPES = [
    ("jd_extract", "JD 初步抽取"),
    ("job_match", "岗位匹配解释"),
    ("message_draft", "投递话术生成"),
    ("hr_reply_classify", "HR 回复分类"),
    ("hr_reply_draft", "HR 回复草稿"),
    ("company_research", "公司搜索摘要"),
    ("interview_prep", "面试准备"),
    ("interview_review", "面试复盘"),
]
