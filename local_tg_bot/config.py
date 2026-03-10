from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv


_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_map(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    items: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            items[key] = value
    return items


def _first_nonempty(*values: str | None, default: str = "") -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return default


def _derive_project_root() -> str:
    allowed = os.getenv("ALLOWED_ROOTS", "")
    allowed_first = allowed.split(",")[0].strip() if allowed else ""
    return _first_nonempty(
        os.getenv("PROJECT_ROOT"),
        os.getenv("DEFAULT_WORKDIR"),
        allowed_first,
        default="/path/to/projects",
    )


@dataclass
class Settings:
    telegram_bot_token: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_base_url: str = os.getenv("TELEGRAM_BASE_URL", "https://api.telegram.org")
    telegram_proxy: str | None = os.getenv("TELEGRAM_PROXY")
    telegram_force_curl: bool = os.getenv("TELEGRAM_FORCE_CURL", "0") == "1"
    telegram_poll_interval_sec: float = _get_float("TELEGRAM_POLL_INTERVAL_SEC", 0.5)
    telegram_long_poll_timeout_sec: int = _get_int("TELEGRAM_LONG_POLL_TIMEOUT_SEC", 20)
    telegram_allowed_chat_ids: str | None = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS")
    telegram_message_max_chars: int = _get_int("TELEGRAM_MESSAGE_MAX_CHARS", 3500)

    allowed_roots: str = _first_nonempty(os.getenv("ALLOWED_ROOTS"), default="/path/to/projects")
    default_workdir: str = _first_nonempty(
        os.getenv("DEFAULT_WORKDIR"),
        os.getenv("PROJECT_ROOT"),
        os.getenv("ALLOWED_ROOTS", "").split(",")[0] if os.getenv("ALLOWED_ROOTS") else None,
        default="/path/to/projects",
    )
    project_root: str = _derive_project_root()
    project_memory_dir: str = os.getenv("PROJECT_MEMORY_DIR", "data/memory")
    project_session_dir: str = os.getenv("PROJECT_SESSION_DIR", "data/sessions")
    project_memory_max_lines: int = _get_int("PROJECT_MEMORY_MAX_LINES", 600)
    project_memory_context_lines: int = _get_int("PROJECT_MEMORY_CONTEXT_LINES", 80)
    project_context_files: str = os.getenv(
        "PROJECT_CONTEXT_FILES",
        "AGENTS.md,agents.md,README.md,readme.md,TASK.md,task.md,PRD.md,prd.md,DESIGN.md,design.md,STATUS.md,status.md,service/README.md,service/readme.md,server/README.md,server/readme.md,docs/README.md,docs/readme.md,docs/OVERVIEW.md,docs/overview.md,docs/DEPLOYMENT.md,docs/DEPLOYMENT_MAC_MINI.md,DEPLOYMENT.md",
    )
    project_context_max_chars: int = _get_int("PROJECT_CONTEXT_MAX_CHARS", 6000)
    flutter_sdk_path: str | None = os.getenv("FLUTTER_SDK_PATH")
    flutter_auto_add_dir: bool = os.getenv("FLUTTER_AUTO_ADD_DIR", "1") == "1"
    auto_verify: bool = os.getenv("AUTO_VERIFY", "1") == "1"
    auto_verify_timeout_sec: int = _get_int("AUTO_VERIFY_TIMEOUT_SEC", 300)
    auto_plan: bool = os.getenv("AUTO_PLAN", "1") == "1"
    auto_subtasks: bool = os.getenv("AUTO_SUBTASKS", "1") == "1"
    auto_progress_updates: bool = os.getenv("AUTO_PROGRESS_UPDATES", "1") == "1"
    auto_retry_attempts: int = _get_int("AUTO_RETRY_ATTEMPTS", 1)
    auto_retry_on_failure: bool = os.getenv("AUTO_RETRY_ON_FAILURE", "1") == "1"
    auto_update_status_docs: bool = os.getenv("AUTO_UPDATE_STATUS_DOCS", "1") == "1"
    auto_status_max_entries: int = _get_int("AUTO_STATUS_MAX_ENTRIES", 12)

    codex_task_command: str | None = os.getenv("CODEX_TASK_COMMAND")
    codex_task_args: str | None = os.getenv("CODEX_TASK_ARGS")
    codex_timeout_sec: int = _get_int("CODEX_TIMEOUT_SEC", 600)
    codex_max_workers: int = _get_int("CODEX_MAX_WORKERS", 1)
    task_stale_sec: int = _get_int("TASK_STALE_SEC", 0)

    provider_default: str = os.getenv("PROVIDER_DEFAULT", "codex")
    provider_commands: dict[str, str] = field(
        default_factory=lambda: _parse_map(os.getenv("PROVIDER_COMMANDS"))
    )
    provider_args: dict[str, str] = field(
        default_factory=lambda: _parse_map(os.getenv("PROVIDER_ARGS"))
    )

    require_approval_for_delete: bool = os.getenv("REQUIRE_APPROVAL_FOR_DELETE", "1") == "1"
    delete_keywords: str = os.getenv(
        "DELETE_KEYWORDS",
        "delete,remove,rm,trash,cleanup,drop,reset,wipe",
    )

    task_store_path: str = os.getenv("TASK_STORE_PATH", "data/tasks.json")
    enable_caffeinate: bool = os.getenv("ENABLE_CAFFEINATE", "0") == "1"


settings = Settings()
