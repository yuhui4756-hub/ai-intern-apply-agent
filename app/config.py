from __future__ import annotations

import os
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
    ("company_research", "公司搜索摘要"),
    ("interview_prep", "面试准备"),
    ("interview_review", "面试复盘"),
]
