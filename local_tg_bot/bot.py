from __future__ import annotations

import json
import time
import sys
import threading
import queue
import subprocess
from pathlib import Path
import os
import re
import signal
from datetime import datetime, timedelta
from shutil import which
from urllib.parse import urlparse

from .config import settings
from .policy import parse_allowed_roots, is_path_allowed, needs_delete_approval
from .runner import run_codex, diagnose_codex, _ensure_path, _add_project_flutter_to_path
from .store import TaskStore
from .telegram import inline_keyboard, post, split_text, set_force_curl


def _now_id() -> str:
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")


def _log(message: str) -> None:
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    print(f"[{ts}] bot {message}", file=sys.stderr)


def _allowed_chat_ids() -> set[int] | None:
    raw = settings.telegram_allowed_chat_ids
    if not raw:
        return None
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ids.add(int(item))
        except ValueError:
            continue
    return ids or None


def _validate_proxy(proxy: str | None) -> None:
    if not proxy:
        return
    if "HTTP端口" in proxy:
        _log("TELEGRAM_PROXY has placeholder port; update it to a real port.")
        return
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.netloc:
        _log("TELEGRAM_PROXY is invalid; expected full URL like http://127.0.0.1:1087.")
        return
    if parsed.port is None:
        _log("TELEGRAM_PROXY missing port; expected http://127.0.0.1:1087.")
        return


def _list_projects(root: str) -> list[str]:
    projects: list[str] = []
    try:
        for entry in Path(root).iterdir():
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name.startswith("._"):
                continue
            projects.append(name)
    except Exception as exc:
        _log(f"Failed to list projects under {root}: {exc!r}")
    projects.sort()
    return projects


def _normalize_text(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _find_project_in_text(text: str, projects: list[str]) -> str | None:
    if not text:
        return None
    lower = text.lower()
    # Prefer direct substring matches (longest first).
    for name in sorted(projects, key=len, reverse=True):
        if name.lower() in lower:
            return name
    normalized = _normalize_text(text)
    normalized_map = { _normalize_text(name): name for name in projects }
    for key, name in sorted(normalized_map.items(), key=lambda item: len(item[0]), reverse=True):
        if key and key in normalized:
            return name
    # Alias for common typo.
    if "i-agent" in lower and "ai-agent" in projects:
        return "ai-agent"
    return None


def _resolve_project_path(
    value: str,
    allowed_roots: list[str],
    project_root: str,
    projects: list[str],
) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if os.path.isabs(raw) and is_path_allowed(raw, allowed_roots):
        return raw
    project = _find_project_in_text(raw, projects)
    if project:
        candidate = str(Path(project_root) / project)
        if is_path_allowed(candidate, allowed_roots):
            return candidate
    return None


def _project_preamble(projects: list[str], workdir: str) -> str:
    project_name = Path(workdir).name if Path(workdir).parent == Path(settings.project_root) else ""
    listed = ", ".join(projects) if projects else "(none)"
    lines = [
        "Project context (one folder = one project):",
        f"- Projects root: {settings.project_root}",
        f"- Available projects: {listed}",
        f"- Current project: {project_name or workdir}",
        "- If the request mentions a project name, focus on that folder. Otherwise use the current project.",
        "- Flutter is installed locally and emulators are available. If the user asks to run Flutter, run it in the current project folder.",
        "- Inside the current workdir, make reasonable development changes directly without asking for confirmation unless the action is clearly destructive.",
        "- After code changes, run the project's basic verification commands when feasible and report the result.",
    ]
    if project_name:
        summary, hint = _project_info(settings.project_root, project_name)
        if summary:
            lines.append(f"- Project summary: {summary}")
        if hint:
            lines.append(f"- Project server hint: {hint}")
    return "\n".join(lines)


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _project_readme_text(project_root: str, project: str) -> str:
    base = Path(project_root) / project
    for name in ("README.md", "readme.md"):
        path = base / name
        if path.exists() and path.is_file():
            return _read_text_file(path)
    return ""


def _summarize_readme(text: str) -> str:
    lower = text.lower()
    if "a new flutter project." in lower and "starting point for a flutter application" in lower:
        return "Flutter template README (needs project-specific summary)"
    lines = text.splitlines()
    in_code = False
    heading = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("#"):
            if not heading:
                heading = line.lstrip("#").strip()
            continue
        if line.startswith("- [") and "](" in line:
            continue
        return line
    return heading


def _project_server_hint(text: str) -> str | None:
    lower = text.lower()
    if "ssh trading" in lower:
        return "server: ssh trading"
    import re
    match = re.search(r"ssh\s+([\w@.-]+)", text)
    if match:
        return f"server: ssh {match.group(1)}"
    hints: list[str] = []
    if "systemctl" in lower:
        hints.append("systemd")
    if "pm2" in lower:
        hints.append("pm2")
    if "nginx" in lower:
        hints.append("nginx")
    if "docker" in lower or "cloud run" in lower:
        hints.append("cloud run")
    if hints:
        seen: list[str] = []
        for item in hints:
            if item not in seen:
                seen.append(item)
        return "server: " + ", ".join(seen)
    return None


def _project_info(project_root: str, project: str) -> tuple[str | None, str | None]:
    text = _project_readme_text(project_root, project)
    if not text:
        return None, None
    summary = _summarize_readme(text) or None
    hint = _project_server_hint(text)
    return summary, hint


def _available_providers() -> list[str]:
    names: set[str] = set()
    if settings.codex_task_command:
        names.add("codex")
    if settings.provider_default:
        names.add(_normalize_provider(settings.provider_default))
    names.update(_normalize_provider(name) for name in settings.provider_commands.keys())
    return sorted(n for n in names if n)


def _normalize_provider(value: str) -> str:
    return value.strip().lower()


def _context_file_candidates() -> list[str]:
    raw = settings.project_context_files
    if not raw:
        return []
    items = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        items.append(part)
    return items


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (truncated)"


def _project_docs_context(workdir: str) -> str:
    candidates = _context_file_candidates()
    if not candidates:
        return ""
    base = Path(workdir)
    max_total = max(0, settings.project_context_max_chars)
    max_per_file = 2000
    remaining = max_total if max_total > 0 else None
    chunks: list[str] = []
    for rel in candidates:
        path = (base / rel).resolve()
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not text:
            continue
        limit = max_per_file
        if remaining is not None:
            limit = min(limit, remaining)
        snippet = _truncate_text(text, limit)
        chunks.append(f"[{path.relative_to(base)}]\n{snippet}")
        if remaining is not None:
            remaining -= len(snippet)
            if remaining <= 0:
                break
    if not chunks:
        return ""
    return "Project docs (selected):\n" + "\n\n".join(chunks)


def _project_name_for_path(workdir: str) -> str:
    try:
        path = Path(workdir).resolve()
        root = Path(settings.project_root).resolve()
        if path.parent == root:
            return path.name
        return path.name
    except Exception:
        return Path(workdir).name


def _memory_dir() -> Path:
    base = Path(settings.project_memory_dir)
    if not base.is_absolute():
        base = Path(settings.task_store_path).parent / base
    base.mkdir(parents=True, exist_ok=True)
    return base


def _session_dir() -> Path:
    base = Path(settings.project_session_dir)
    if not base.is_absolute():
        base = Path(settings.task_store_path).parent / base
    base.mkdir(parents=True, exist_ok=True)
    return base


def _memory_path(project: str, chat_id: int) -> Path:
    safe = _normalize_text(project) or project.replace(" ", "_")
    base = _memory_dir() / safe
    base.mkdir(parents=True, exist_ok=True)
    return base / f"chat_{chat_id}.md"


def _append_memory(chat_id: int, task, outcome: str, detail: str) -> None:
    project = _project_name_for_path(task.workdir)
    path = _memory_path(project, chat_id)
    try:
        with path.open("a", encoding="utf-8") as file:
            file.write(f"## {datetime.utcnow().isoformat()}Z\n")
            file.write(f"- project: {project}\n")
            file.write(f"- workdir: {task.workdir}\n")
            file.write(f"- status: {outcome}\n")
            file.write(f"- task: {task.text.strip()}\n")
            if detail:
                file.write(f"- detail: {detail.strip()}\n")
            file.write("\n")
    except Exception as exc:
        _log(f"memory append failed for {project}: {exc!r}")
        return
    _trim_memory(path, settings.project_memory_max_lines)


def _session_path(project: str, chat_id: int) -> Path:
    safe = _normalize_text(project) or project.replace(" ", "_")
    base = _session_dir() / safe
    base.mkdir(parents=True, exist_ok=True)
    return base / f"chat_{chat_id}.json"


def _status_doc_path(workdir: str) -> Path:
    base = Path(workdir)
    for name in ("STATUS.md", "status.md"):
        path = base / name
        if path.exists():
            return path
    return base / "STATUS.md"


def _safe_summary(text: str, limit: int = 800) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    return cleaned[:limit].rstrip()


def _update_session_archive(chat_id: int, task, summary: str) -> None:
    project = _project_name_for_path(task.workdir)
    path = _session_path(project, chat_id)
    payload = {
        "project": project,
        "workdir": task.workdir,
        "chat_id": chat_id,
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "current_task_id": task.task_id,
        "last_status": task.status,
        "last_phase": task.phase,
        "recent_plan": task.plan or [],
        "recent_summary": _safe_summary(summary, 1200),
        "recent_tasks": [],
    }
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        payload.update(
            {
                "project": project,
                "workdir": task.workdir,
                "chat_id": chat_id,
                "updated_at": datetime.utcnow().isoformat() + "Z",
                "current_task_id": task.task_id,
                "last_status": task.status,
                "last_phase": task.phase,
                "recent_plan": task.plan or [],
                "recent_summary": _safe_summary(summary, 1200),
            }
        )
    recent = payload.get("recent_tasks")
    if not isinstance(recent, list):
        recent = []
    recent.insert(
        0,
        {
            "task_id": task.task_id,
            "status": task.status,
            "phase": task.phase,
            "attempts": task.attempts,
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "request": _safe_summary(task.text, 400),
            "summary": _safe_summary(summary, 800),
            "plan": task.plan or [],
        },
    )
    payload["recent_tasks"] = recent[:10]
    try:
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        _log(f"failed to update session archive for {project}: {exc!r}")


def _session_context(project: str, chat_id: int) -> str:
    path = _session_path(project, chat_id)
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ""
    lines = []
    summary = payload.get("recent_summary")
    if isinstance(summary, str) and summary.strip():
        lines.append("Last session summary: " + summary.strip())
    plan = payload.get("recent_plan")
    if isinstance(plan, list) and plan:
        lines.append("Last session plan:")
        lines.extend(f"- {str(item).strip()}" for item in plan[:6] if str(item).strip())
    recent = payload.get("recent_tasks")
    if isinstance(recent, list) and recent:
        lines.append("Session archive:")
        for item in recent[:3]:
            if not isinstance(item, dict):
                continue
            tid = item.get("task_id", "")
            status = item.get("status", "")
            request = str(item.get("request", "")).strip()
            if tid or request:
                lines.append(f"- {tid} [{status}] {request}".strip())
    if not lines:
        return ""
    return "Project session archive:\n" + "\n".join(lines)


def _update_status_doc(task, summary: str, verification: str | None = None) -> None:
    if not settings.auto_update_status_docs:
        return
    path = _status_doc_path(task.workdir)
    title = f"# Status\n\n"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    current = (
        "## Current\n\n"
        f"- Project: `{_project_name_for_path(task.workdir)}`\n"
        f"- Last task: `{task.task_id}`\n"
        f"- Status: `{task.status}`\n"
        f"- Phase: `{task.phase or 'n/a'}`\n"
        f"- Updated: {now}\n"
    )
    if task.plan:
        current += "\n### Last Plan\n\n" + "\n".join(f"- {item}" for item in task.plan[:6]) + "\n"
    history_entry = [
        f"### {task.task_id} - {now}",
        f"- Request: {_safe_summary(task.text, 300)}",
        f"- Result: {_safe_summary(summary, 500) or '(no summary)'}",
    ]
    if verification:
        history_entry.append(f"- Verification: {_safe_summary(verification, 400)}")
    history_block = "\n".join(history_entry)

    old_text = ""
    if path.exists():
        old_text = _read_text_file(path)
    old_history = ""
    marker = "## History"
    if marker in old_text:
        old_history = old_text.split(marker, 1)[1].strip()
    entries: list[str] = []
    if old_history:
        parts = [part.strip() for part in old_history.split("\n### ") if part.strip()]
        for idx, part in enumerate(parts):
            entries.append(("### " + part) if idx > 0 else (part if part.startswith("### ") else "### " + part))
    entries.insert(0, history_block)
    entries = entries[: settings.auto_status_max_entries]
    final = title + current + "\n## History\n\n" + "\n\n".join(entries).strip() + "\n"
    try:
        path.write_text(final, encoding="utf-8")
    except Exception as exc:
        _log(f"failed to update status doc {path}: {exc!r}")


def _trim_memory(path: Path, max_lines: int) -> None:
    if max_lines <= 0:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return
    if len(lines) <= max_lines:
        return
    keep = lines[-max_lines:]
    try:
        path.write_text("\n".join(keep) + "\n", encoding="utf-8")
    except Exception:
        return


def _memory_context(project: str, chat_id: int, max_lines: int) -> str:
    if max_lines <= 0:
        return ""
    path = _memory_path(project, chat_id)
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    tail = []
    for line in lines[-max_lines:]:
        lower = line.lower()
        stripped = line.lstrip()
        if stripped.startswith("<"):
            continue
        if "flutter sdk" in lower and "writable" in lower:
            continue
        if "copy the sdk" in lower or "copy sdk" in lower:
            continue
        if "chrome" in lower or "web" in lower:
            continue
        if "cocoapods" in lower or "pod install" in lower:
            continue
        if "wss://chatgpt.com" in lower or "backend-api/codex" in lower:
            continue
        if "reconnecting" in lower or "403 forbidden" in lower:
            continue
        if "<!doctype html" in lower or "cf-ray" in lower:
            continue
        tail.append(line)
    if not tail:
        return ""
    return "Project memory (recent):\n" + "\n".join(tail)


def _run_text_command(command: list[str], workdir: str, timeout: int = 8) -> str:
    try:
        proc = subprocess.run(
            command,
            cwd=workdir,
            env=_prepare_env(workdir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return ""
    text = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return text


def _project_snapshot_context(workdir: str) -> str:
    base = Path(workdir)
    entries: list[str] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda item: item.name.lower()):
            name = entry.name
            if name.startswith(".") and name not in {".github", ".vscode"}:
                continue
            if name in {"build", ".dart_tool", "node_modules", ".turbo", ".next"}:
                continue
            suffix = "/" if entry.is_dir() else ""
            entries.append(name + suffix)
            if len(entries) >= 40:
                break
    except Exception:
        return ""

    markers = []
    for marker in ("pubspec.yaml", "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod"):
        if (base / marker).exists():
            markers.append(marker)

    lines = []
    if entries:
        lines.append("Top-level files:")
        lines.append(", ".join(entries))
    if markers:
        lines.append("Tech markers: " + ", ".join(markers))
    if not lines:
        return ""
    return "Project snapshot:\n" + "\n".join(lines)


def _git_context(workdir: str) -> str:
    branch = _run_text_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], workdir, timeout=5)
    status = _run_text_command(["git", "status", "--short"], workdir, timeout=6)
    if not branch and not status:
        return ""
    lines = []
    if branch:
        lines.append(f"Git branch: {branch.splitlines()[0].strip()}")
    if status:
        tail = status.splitlines()[:20]
        lines.append("Git status:\n" + "\n".join(tail))
    return "\n".join(lines)


def _recent_task_context(store: TaskStore, chat_id: int, workdir: str, limit: int = 3) -> str:
    project = _project_name_for_path(workdir)
    tasks = [
        task
        for task in reversed(store.list_tasks_for_chat(chat_id))
        if _task_project_name(task) == project and task.status not in {"queued", "running"}
    ]
    chunks: list[str] = []
    for task in tasks[:limit]:
        lines = [
            f"- id: {task.task_id}",
            f"  status: {task.status}",
            f"  request: {task.text.strip()}",
        ]
        detail = (task.output or task.error or "").strip()
        if detail:
            detail = _truncate_text(detail, 600)
            lines.append("  result: " + detail.replace("\n", " "))
        chunks.append("\n".join(lines))
    if not chunks:
        return ""
    return "Recent task history:\n" + "\n\n".join(chunks)


def _execution_style_context() -> str:
    return (
        "Execution style:\n"
        "- Work like Codex in a local terminal session, not like a generic chatbot.\n"
        "- First inspect relevant files and current repo state before making changes.\n"
        "- Prefer concrete action over questions when the request can be reasonably inferred from the current project.\n"
        "- Keep changes inside the current workdir unless the request clearly targets another allowed project.\n"
        "- If code changes are needed, implement them, then run the lightest useful verification commands.\n"
        "- In the final response, summarize what you changed, what you ran, and any remaining blocker or risk.\n"
        "- Do not ask to copy files or manually inspect obvious things you can inspect yourself.\n"
    )


def _should_auto_plan(text: str, workdir: str) -> bool:
    if not settings.auto_plan:
        return False
    if not _is_code_task(text, workdir):
        return False
    lower = (text or "").lower()
    simple = (
        "status",
        "log",
        "readme",
        "docs",
        "summary",
        "summarize",
        "analyze only",
    )
    return not any(token in lower for token in simple)


def _extract_plan_lines(text: str, limit: int = 6) -> list[str]:
    items: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("1. ", "2. ", "3. ", "4. ", "5. ", "6. ", "- ", "* ")):
            line = re.sub(r"^(\d+\.\s+|[-*]\s+)", "", line).strip()
            if line:
                items.append(line)
        elif not items and len(line) <= 120:
            items.append(line)
        if len(items) >= limit:
            break
    return items[:limit]


def _plan_task(
    task,
    store: TaskStore,
    provider: str,
    needs_flutter: bool,
    base_sections: list[str],
    log_path: Path,
) -> list[str]:
    plan_prompt = "\n\n".join(
        base_sections
        + [
            "Planning task:\n"
            "- Produce a short execution plan before coding.\n"
            "- Use 3 to 6 concise steps.\n"
            "- Focus on concrete implementation and verification steps.\n"
            "- Output only the plan as numbered lines.",
            f"User request:\n{task.text}",
        ]
    )
    plan_output, plan_error = run_codex(
        settings,
        plan_prompt,
        task.workdir,
        log_path=log_path,
        needs_flutter=needs_flutter,
        provider=provider,
    )
    if plan_error:
        return []
    plan = _extract_plan_lines(plan_output or "")
    if plan:
        task.plan = plan
        task.phase = "planned"
        task.total_steps = len(plan)
        store.update_task(task)
    return plan


def _phase_text(task) -> str:
    parts = [f"{task.task_id}: {task.status}"]
    if task.phase:
        parts.append(f"phase={task.phase}")
    if task.total_steps:
        parts.append(f"step={task.current_step}/{task.total_steps}")
    if task.attempts:
        parts.append(f"attempts={task.attempts}")
    return " | ".join(parts)


def _approval_card_text(task) -> str:
    lines = [
        "Approval required",
        f"Task: `{task.task_id}`",
        f"Project: `{_task_project_name(task)}`",
        f"Phase: `{task.phase or 'n/a'}`",
        "",
        "Reason:",
        _safe_summary(task.error or task.text, 500) or "(no detail)",
    ]
    return "\n".join(lines)


def _input_card_text(task) -> str:
    lines = [
        "Need more input",
        f"Task: `{task.task_id}`",
        f"Project: `{_task_project_name(task)}`",
        "",
        "Question:",
        _safe_summary(task.needs_input_prompt or task.error or "", 700) or "(no detail)",
        "",
        f"Reply with: `/reply {task.task_id} <your answer>`",
    ]
    return "\n".join(lines)


def _send_approval_card(chat_id: int, task) -> None:
    _send_message_card(
        chat_id,
        _approval_card_text(task),
        [
            [("Approve", f"approve:{task.task_id}"), ("Reject", f"reject:{task.task_id}")],
            [("Status", f"status:{task.task_id}"), ("Log", f"log:{task.task_id}")],
        ],
    )


def _send_input_card(chat_id: int, task) -> None:
    _send_message_card(
        chat_id,
        _input_card_text(task),
        [
            [("Status", f"status:{task.task_id}"), ("Cancel", f"cancel:{task.task_id}")],
        ],
    )


def _should_subtask_execute(task, workdir: str) -> bool:
    return bool(
        settings.auto_subtasks
        and task.plan
        and len(task.plan) >= 2
        and _is_code_task(task.text, workdir)
    )


def _needs_user_input(output: str | None) -> bool:
    text = (output or "").strip().lower()
    if not text:
        return False
    triggers = [
        "i need to know",
        "please tell me",
        "which project",
        "which file",
        "which path",
        "which directory",
        "what project",
        "what path",
        "what file",
        "can you clarify",
        "need more information",
        "please provide",
    ]
    if any(trigger in text for trigger in triggers):
        return True
    return text.endswith("?") and len(text.splitlines()) <= 8


def _send_message(chat_id: int, text: str) -> None:
    for chunk in split_text(text, settings.telegram_message_max_chars):
        payload = {"chat_id": chat_id, "text": chunk}
        resp = post(_base_url(), "/sendMessage", payload, proxy=settings.telegram_proxy)
        if not resp:
            _log(f"sendMessage failed for chat_id={chat_id}")


def _send_message_card(chat_id: int, text: str, rows: list[list[tuple[str, str]]]) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": inline_keyboard(rows),
    }
    resp = post(_base_url(), "/sendMessage", payload, proxy=settings.telegram_proxy)
    if not resp:
        _log(f"sendMessage card failed for chat_id={chat_id}")


def _answer_callback(callback_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    post(_base_url(), "/answerCallbackQuery", payload, proxy=settings.telegram_proxy)


def _send_action(chat_id: int, action: str = "typing") -> None:
    payload = {"chat_id": chat_id, "action": action}
    post(_base_url(), "/sendChatAction", payload, proxy=settings.telegram_proxy)


def _base_url() -> str:
    return settings.telegram_base_url.rstrip("/") + f"/bot{settings.telegram_bot_token}"


def _handle_command(
    chat_id: int,
    text: str,
    store: TaskStore,
    allowed_roots: list[str],
    runner: "TaskRunner",
    projects: list[str],
) -> bool:
    if text.startswith("/start") or text.startswith("/help"):
        _send_message(
            chat_id,
            "Local TG bot ready.\n"
            "Commands: /workdir <path> | /projects | /providers | /provider <name> | /model <name> | "
            "/switch <project> | /leave | /current | /status [id] | "
            "/tasks [project|all] | /log <id> [lines] | /selfcheck | /approve <id> | /reject <id> | /reply <id> <text> | "
            "/cancel <id> | /reset\n"
            "Notes: workdir tasks are auto-executed unless clearly destructive. Basic verification runs automatically for code tasks.",
        )
        return True
    if text.startswith("/reset"):
        store.clear_chat(chat_id)
        _send_message(chat_id, "Chat tasks cleared.")
        return True
    if text.startswith("/workdir "):
        path = text.split(" ", 1)[1].strip()
        resolved = _resolve_project_path(path, allowed_roots, settings.project_root, projects)
        if not resolved:
            _send_message(chat_id, "Workdir not allowed.")
            return True
        store.set_chat_workdir(chat_id, resolved)
        _send_message(chat_id, f"Workdir set to {resolved}")
        return True
    if text.startswith("/switch "):
        target = text.split(" ", 1)[1].strip()
        resolved = _resolve_project_path(target, allowed_roots, settings.project_root, projects)
        if not resolved:
            _send_message(chat_id, "Project not found or not allowed.")
            return True
        current = store.get_chat_workdir(chat_id) or settings.default_workdir
        # Save a memory marker for the current project before switching.
        temp_task = store.create_task(_now_id(), chat_id, "Switch project", current, False)
        _append_memory(chat_id, temp_task, "switch", f"switched to {resolved}")
        temp_task.status = "done"
        store.update_task(temp_task)
        store.set_chat_workdir(chat_id, resolved)
        _send_message(chat_id, f"Switched to {resolved}. Memory saved.")
        return True
    if text.startswith("/leave"):
        current = store.get_chat_workdir(chat_id) or settings.default_workdir
        temp_task = store.create_task(_now_id(), chat_id, "Leave project", current, False)
        _append_memory(chat_id, temp_task, "leave", "cleared current project context")
        temp_task.status = "done"
        store.update_task(temp_task)
        store.set_chat_workdir(chat_id, settings.default_workdir)
        _send_message(chat_id, "Left current project. Workdir reset to default.")
        return True
    if text.startswith("/projects"):
        if not projects:
            _send_message(chat_id, "No projects found.")
            return True
        lines = ["Projects:"]
        for name in projects:
            summary, hint = _project_info(settings.project_root, name)
            line = f"{name}"
            if summary:
                line += f" — {summary}"
            if hint:
                line += f" [{hint}]"
            lines.append(f"- {line}")
        _send_message(chat_id, "\n".join(lines))
        return True
    if text.startswith("/providers"):
        providers = _available_providers()
        current = store.get_chat_provider(chat_id) or settings.provider_default or "codex"
        if not providers:
            _send_message(chat_id, f"Current provider: {current}\nNo providers configured.")
            return True
        _send_message(
            chat_id,
            "Providers:\n- " + "\n- ".join(providers) + f"\nCurrent provider: {current}",
        )
        return True
    if text.startswith("/provider") or text.startswith("/model"):
        parts = text.split(maxsplit=1)
        current = store.get_chat_provider(chat_id) or settings.provider_default or "codex"
        providers = _available_providers()
        if len(parts) == 1 or not parts[1].strip():
            if providers:
                _send_message(
                    chat_id,
                    "Providers:\n- " + "\n- ".join(providers) + f"\nCurrent provider: {current}",
                )
            else:
                _send_message(chat_id, f"Current provider: {current}\nNo providers configured.")
            return True
        target = _normalize_provider(parts[1])
        if target not in providers:
            _send_message(
                chat_id,
                "Unknown provider. Available:\n- " + "\n- ".join(providers),
            )
            return True
        store.set_chat_provider(chat_id, target)
        _send_message(chat_id, f"Provider set to {target}.")
        return True
    if text.startswith("/status"):

        parts = text.split(" ", 1)
        if len(parts) == 2:
            task_id = parts[1].strip()
            task = store.get_task(task_id)
            if not task:
                _send_message(chat_id, "Task not found.")
                return True
            lines = [_phase_text(task)]
            if task.plan:
                lines.append("Plan:")
                lines.extend(f"- {item}" for item in task.plan[:6])
            _send_message(chat_id, "\n".join(lines))
            return True
        tasks = store.list_tasks_for_chat(chat_id)[-5:]
        if not tasks:
            _send_message(chat_id, "No tasks.")
            return True
        lines = [_phase_text(t) for t in tasks]
        _send_message(chat_id, "\n".join(lines))
        return True
    if text.startswith("/tasks"):
        parts = text.split(maxsplit=1)
        scope = parts[1].strip() if len(parts) > 1 else ""
        stats = runner.stats()
        header = (
            f"Workers: {stats['workers']} | Queue: {stats['queue']} | Active: {len(stats['active'])}"
        )

        all_tasks = store.list_tasks_for_chat(chat_id)
        if scope.lower() in ("all", "*"):
            tasks = store.list_all_tasks()
        else:
            if scope:
                resolved = _resolve_project_path(
                    scope, allowed_roots, settings.project_root, projects
                )
                project_name = _project_name_for_path(resolved) if resolved else scope
            else:
                current = store.get_chat_workdir(chat_id) or settings.default_workdir
                project_name = _project_name_for_path(current)
            tasks = [t for t in all_tasks if _task_project_name(t) == project_name]

        tasks = _recent_tasks(tasks, 10)
        if not tasks:
            _send_message(chat_id, header + "\nNo tasks.")
            return True

        lines = [header]
        for t in tasks:
            proj = _task_project_name(t)
            extra = f" phase={t.phase}" if t.phase else ""
            tries = f" attempts={t.attempts}" if t.attempts else ""
            lines.append(f"{t.task_id} [{t.status}] ({proj}){extra}{tries}")
        _send_message(chat_id, "\n".join(lines))
        return True
    if text.startswith("/current"):
        workdir = store.get_chat_workdir(chat_id) or settings.default_workdir
        project = _project_name_for_path(workdir)
        provider = store.get_chat_provider(chat_id) or settings.provider_default or "codex"
        tasks = store.list_tasks_for_chat(chat_id)
        last_task = tasks[-1] if tasks else None
        lines = [
            f"Current project: {project}",
            f"Workdir: {workdir}",
        ]
        lines.append(f"Provider: {provider}")
        summary, hint = _project_info(settings.project_root, project)
        if summary:
            lines.append(f"Summary: {summary}")
        if hint:
            lines.append(f"Server hint: {hint}")
        if last_task:
            suffix = f" phase={last_task.phase}" if last_task.phase else ""
            lines.append(f"Last task: {last_task.task_id} [{last_task.status}]{suffix}")
        else:
            lines.append("Last task: (none)")
        _send_message(chat_id, "\n".join(lines))
        return True
    if text.startswith("/selfcheck"):
        diag = diagnose_codex(settings)
        _send_message(chat_id, f"Self-check:\n{diag}")
        return True
    if text.startswith("/log"):
        parts = text.split()
        if len(parts) < 2:
            _send_message(chat_id, "Usage: /log <task_id> [lines]")
            return True
        task_id = parts[1].strip()
        lines = 20
        if len(parts) >= 3 and parts[2].isdigit():
            lines = max(1, min(200, int(parts[2])))
        path = _task_log_path(task_id)
        if not path.exists():
            _send_message(chat_id, "Log not found for that task id.")
            return True
        tail = _tail_log(path, lines)
        _send_message(chat_id, f"Log tail ({lines} lines):\n{tail}")
        return True
    if text.startswith("/approve "):
        task_id = text.split(" ", 1)[1].strip()
        task = store.get_task(task_id)
        if not task:
            _send_message(chat_id, "Task not found.")
            return True
        if task.status != "needs_approval":
            _send_message(chat_id, f"Task status is {task.status}.")
            return True
        task.status = "queued"
        store.update_task(task)
        task.needs_input_prompt = None
        _send_message(chat_id, f"Approved. Task queued: {task.task_id}")
        runner.enqueue(task.task_id, chat_id)
        return True
    if text.startswith("/reply "):
        parts = text.split(" ", 2)
        if len(parts) < 3:
            _send_message(chat_id, "Usage: /reply <task_id> <your answer>")
            return True
        task_id = parts[1].strip()
        reply_text = parts[2].strip()
        task = store.get_task(task_id)
        if not task:
            _send_message(chat_id, "Task not found.")
            return True
        task.text = task.text.rstrip() + f"\n\nUser clarification:\n{reply_text}"
        task.status = "queued"
        task.phase = "clarified"
        task.needs_input_prompt = None
        task.error = None
        store.update_task(task)
        _send_message(chat_id, f"Reply attached. Task queued: {task.task_id}")
        runner.enqueue(task.task_id, chat_id)
        return True
    if text.startswith("/cancel "):
        task_id = text.split(" ", 1)[1].strip()
        task = store.get_task(task_id)
        if not task:
            _send_message(chat_id, "Task not found.")
            return True
        if task.status in ("done", "failed", "cancelled"):
            _send_message(chat_id, f"Task status is {task.status}.")
            return True
        if task.pid:
            try:
                os.kill(task.pid, signal.SIGTERM)
            except Exception as exc:
                _log(f"Failed to kill pid {task.pid}: {exc!r}")
        task.status = "cancelled"
        task.error = "Cancelled by user."
        task.pid = None
        store.update_task(task)
        _send_message(chat_id, f"Task {task_id} cancelled.")
        return True
    if text.startswith("/reject "):
        task_id = text.split(" ", 1)[1].strip()
        task = store.get_task(task_id)
        if not task:
            _send_message(chat_id, "Task not found.")
            return True
        task.status = "rejected"
        task.phase = "rejected"
        store.update_task(task)
        _send_message(chat_id, f"Task {task_id} rejected.")
        return True
    return False


def _needs_codex_approval(error: str) -> bool:
    text = (error or "").lower()
    triggers = [
        "approval required",
        "requires approval",
        "needs approval",
        "confirmation required",
        "requires confirmation",
        "confirm to",
        "approve to",
    ]
    return any(trigger in text for trigger in triggers)


def _needs_ios_verbose_retry(wants_ios: bool, output: str | None) -> bool:
    if not wants_ios or not output:
        return False
    lower = output.lower()
    if "flutter run -v" in lower or " -v -d " in lower:
        return False
    return "error launching application" in lower and "iphone" in lower


def _log_has_ios_launch_error(log_path: Path) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return False
    return "error launching application" in text and "iphone" in text


def _log_has_diagnostics(log_path: Path) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8", errors="ignore").lower()
    except Exception:
        return False
    return "[diagnostics]" in text or "flutter run -v" in text


def _flutter_bin(workdir: str) -> str:
    sdk_bin = Path(workdir) / ".flutter-sdk" / "bin" / "flutter"
    if sdk_bin.exists():
        return str(sdk_bin)
    return "flutter"


def _prepare_env(workdir: str) -> dict:
    env = os.environ.copy()
    _ensure_path(env)
    _add_project_flutter_to_path(env, workdir)
    return env


def _is_docs_only_task(text: str) -> bool:
    lower = (text or "").lower()
    if any(token in lower for token in ("flutter", "dart", "build", "run", "test", "analyze", "fix", "bug")):
        return False
    docs_tokens = ("readme", ".md", "markdown", "docs", "documentation", "translate", "summarize", "summary")
    return any(token in lower for token in docs_tokens)


def _is_code_task(text: str, workdir: str) -> bool:
    if _is_docs_only_task(text):
        return False
    lower = (text or "").lower()
    triggers = (
        "fix",
        "bug",
        "implement",
        "create",
        "build",
        "run",
        "test",
        "analyze",
        "refactor",
        "screen",
        "page",
        "api",
        "flutter",
        "dart",
        "react",
        "ios",
        "android",
    )
    if any(token in lower for token in triggers):
        return True
    base = Path(workdir)
    return any(
        (base / marker).exists()
        for marker in ("pubspec.yaml", "package.json", "pyproject.toml", "requirements.txt")
    )


def _detect_package_manager(workdir: str) -> str:
    base = Path(workdir)
    if (base / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (base / "yarn.lock").exists():
        return "yarn"
    return "npm"


def _load_package_scripts(workdir: str) -> dict[str, str]:
    path = Path(workdir) / "package.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in scripts.items():
        if isinstance(key, str) and isinstance(value, str):
            result[key] = value
    return result


def _verification_plan(workdir: str, text: str) -> list[tuple[str, list[str]]]:
    if not settings.auto_verify or not _is_code_task(text, workdir):
        return []

    base = Path(workdir)
    steps: list[tuple[str, list[str]]] = []

    if (base / "pubspec.yaml").exists():
        flutter = _flutter_bin(workdir)
        steps.append(("flutter pub get", [flutter, "pub", "get"]))
        steps.append(("flutter analyze", [flutter, "analyze"]))
        return steps

    scripts = _load_package_scripts(workdir)
    package_manager = _detect_package_manager(workdir)
    if package_manager == "pnpm":
        runner = "pnpm"
    elif package_manager == "yarn":
        runner = "yarn"
    else:
        runner = "npm"

    if scripts.get("lint"):
        steps.append((f"{runner} lint", [runner, "run", "lint"] if runner != "yarn" else [runner, "lint"]))
    elif scripts.get("check"):
        steps.append((f"{runner} check", [runner, "run", "check"] if runner != "yarn" else [runner, "check"]))

    if scripts.get("typecheck"):
        steps.append(
            (f"{runner} typecheck", [runner, "run", "typecheck"] if runner != "yarn" else [runner, "typecheck"])
        )

    if not steps and scripts.get("test"):
        if any(token in (text or "").lower() for token in ("test", "fix", "bug", "implement")):
            steps.append((f"{runner} test", [runner, "run", "test"] if runner != "yarn" else [runner, "test"]))

    if steps:
        return steps[:2]

    if (
        ((base / "pyproject.toml").exists() or (base / "pytest.ini").exists() or (base / "tests").exists())
        and which("pytest", path=_prepare_env(workdir).get("PATH", ""))
    ):
        return [("pytest -q", ["pytest", "-q"])]

    return []


def _trim_command_output(text: str, max_lines: int = 80) -> str:
    lines = (text or "").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines).strip()
    return "\n".join(lines[-max_lines:]).strip()


def _run_verification(workdir: str, task_text: str, log_path: Path) -> tuple[bool, str]:
    steps = _verification_plan(workdir, task_text)
    if not steps:
        return True, "No automatic verification steps matched this task."

    env = _prepare_env(workdir)
    results: list[str] = []
    all_ok = True

    for label, command in steps:
        try:
            proc = subprocess.run(
                command,
                cwd=workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=settings.auto_verify_timeout_sec,
            )
            combined = ((proc.stdout or "") + (proc.stderr or "")).strip()
        except subprocess.TimeoutExpired:
            all_ok = False
            combined = f"Timed out after {settings.auto_verify_timeout_sec}s."
            proc = None
        except Exception as exc:
            all_ok = False
            combined = f"Failed to start: {exc}"
            proc = None

        ok = proc is not None and proc.returncode == 0
        if not ok:
            all_ok = False
        snippet = _trim_command_output(combined)
        status = "PASS" if ok else "FAIL"
        line = f"[{status}] {label}"
        if snippet:
            line += "\n" + snippet
        results.append(line)

    summary = "Verification:\n" + "\n\n".join(results)
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write("\n[verification]\n")
            log_file.write(summary)
            log_file.write("\n")
    except Exception as exc:
        _log(f"failed to write verification for log {log_path}: {exc!r}")
    return all_ok, summary


def _parse_ios_device_id(devices_output: str) -> str | None:
    for line in devices_output.splitlines():
        if "ios" not in line.lower() or "simulator" not in line.lower():
            continue
        if "•" not in line:
            continue
        parts = [part.strip() for part in line.split("•")]
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return None


def _run_flutter_verbose_diag(workdir: str) -> str:
    flutter = _flutter_bin(workdir)
    env = _prepare_env(workdir)
    devices_cmd = [flutter, "devices"]
    try:
        devices_proc = subprocess.run(
            devices_cmd,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return f"Failed to run flutter devices: {exc}"

    devices_output = (devices_proc.stdout or "") + (devices_proc.stderr or "")
    ios_id = _parse_ios_device_id(devices_output)
    if not ios_id:
        return "No iOS simulator found.\n\nflutter devices output:\n" + devices_output

    log_path = Path("/tmp/flutter_run.log")
    try:
        with log_path.open("w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                [flutter, "run", "-v", "-d", ios_id],
                cwd=workdir,
                env=env,
                stdout=log_file,
                stderr=log_file,
            )
            try:
                proc.wait(timeout=180)
            except subprocess.TimeoutExpired:
                proc.terminate()
    except Exception as exc:
        return f"Failed to run flutter run -v: {exc}"

    try:
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return f"Failed to read verbose log: {exc}"

    tail_lines = log_text.splitlines()[-200:]
    keyword_lines = [
        line
        for line in log_text.splitlines()
        if any(token in line.lower() for token in ("error", "failed", "pod", "pods", "xcode", "codesign", "clang", "ld"))
    ]
    tail_block = "\n".join(tail_lines)
    keyword_block = "\n".join(keyword_lines[-120:])
    return (
        "Verbose tail (last 200 lines):\n"
        + tail_block
        + "\n\nFiltered errors (last 120 lines):\n"
        + (keyword_block or "(none)")
    )


def _task_log_path(task_id: str) -> Path:
    base = Path(settings.task_store_path).parent / "logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{task_id}.log"


def _tail_log(path: Path, max_lines: int) -> str:
    if max_lines <= 0:
        max_lines = 20
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception as exc:
        return f"Failed to read log: {exc}"
    if not lines:
        return "(log is empty)"
    tail = [_redact_line(line) for line in lines[-max_lines:]]
    return "\n".join(tail)


def _redact_line(line: str) -> str:
    lower = line.lower()
    if "token" not in lower and "apikey" not in lower and "api_key" not in lower and "secret" not in lower:
        return line
    # Redact quoted secrets.
    line = re.sub(r"(['\"])[^'\"]{6,}\\1", r"'[REDACTED]'", line)
    # Redact key=value style.
    line = re.sub(r"(?i)(token|apikey|api_key|secret)\\s*[:=]\\s*[^\\s,;]+", r"\\1=[REDACTED]", line)
    return line


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.rstrip("Z")
        return datetime.fromisoformat(cleaned)
    except Exception:
        return None


def _task_project_name(task) -> str:
    return _project_name_for_path(task.workdir)


def _recent_tasks(tasks: list, limit: int) -> list:
    def _key(t):
        return _parse_time(t.updated_at) or _parse_time(t.created_at) or datetime.min
    tasks = sorted(tasks, key=_key, reverse=True)
    return tasks[:limit]


class TaskRunner:
    def __init__(self, store: TaskStore, allowed_roots: list[str], max_workers: int) -> None:
        self.store = store
        self.allowed_roots = allowed_roots
        self.queue: queue.Queue[tuple[str, int]] = queue.Queue()
        workers = max(1, max_workers)
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._completed = 0
        self.threads = [
            threading.Thread(target=self._worker, name=f"task-worker-{i+1}", daemon=True)
            for i in range(workers)
        ]
        for thread in self.threads:
            thread.start()

    def enqueue(self, task_id: str, chat_id: int) -> None:
        self.queue.put((task_id, chat_id))

    def _worker(self) -> None:
        while True:
            task_id, chat_id = self.queue.get()
            try:
                task = self.store.get_task(task_id)
                if not task:
                    continue
                with self._lock:
                    self._active.add(task_id)
                try:
                    _run_task(chat_id, task, self.store, self.allowed_roots)
                except Exception as exc:
                    _log(f"worker crashed for task {task_id}: {exc!r}")
                    if task:
                        task.status = "failed"
                        task.error = f"Bot error: {exc!r}"
                        self.store.update_task(task)
                        _send_message(chat_id, f"Task failed: {task.error}")
            finally:
                with self._lock:
                    self._active.discard(task_id)
                    self._completed += 1
                self.queue.task_done()

    def stats(self) -> dict[str, object]:
        with self._lock:
            active = sorted(self._active)
            completed = self._completed
        return {
            "workers": len(self.threads),
            "queue": self.queue.qsize(),
            "active": active,
            "completed": completed,
        }


class TaskWatchdog:
    def __init__(self, store: TaskStore, stale_after: int) -> None:
        self.store = store
        self.stale_after = max(60, stale_after)
        self.thread = threading.Thread(target=self._worker, name="task-watchdog", daemon=True)
        self.thread.start()

    def _worker(self) -> None:
        while True:
            now = datetime.utcnow()
            for task in self.store.list_all_tasks():
                if task.status != "running":
                    continue
                updated = _parse_time(task.updated_at) or _parse_time(task.created_at)
                if not updated:
                    continue
                age = (now - updated).total_seconds()
                if age < self.stale_after:
                    continue
                task.status = "failed"
                task.error = f"Task timed out after {int(age)}s."
                self.store.update_task(task)
                _send_message(task.chat_id, f"Task {task.task_id} timed out.")
            time.sleep(30)


def _send_phase_update(chat_id: int, task, include_plan: bool = False) -> None:
    if not settings.auto_progress_updates:
        return
    lines = [_phase_text(task)]
    if include_plan and task.plan:
        lines.append("Plan:")
        lines.extend(f"- {item}" for item in task.plan[:6])
    _send_message(chat_id, "\n".join(lines))


def _append_log_block(log_path: Path, title: str, text: str) -> None:
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{title}]\n")
            log_file.write(text.rstrip() + "\n")
    except Exception as exc:
        _log(f"failed to write {title} block for {log_path.name}: {exc!r}")


def _handle_callback(
    callback_id: str,
    chat_id: int,
    data: str,
    store: TaskStore,
    runner: "TaskRunner",
) -> bool:
    action, _, task_id = data.partition(":")
    if not action or not task_id:
        _answer_callback(callback_id, "Unsupported action")
        return False
    task = store.get_task(task_id)
    if not task:
        _answer_callback(callback_id, "Task not found")
        return True
    if action == "approve":
        if task.status != "needs_approval":
            _answer_callback(callback_id, f"Task is {task.status}")
            return True
        task.status = "queued"
        task.phase = "approved"
        task.needs_input_prompt = None
        store.update_task(task)
        runner.enqueue(task.task_id, chat_id)
        _answer_callback(callback_id, "Approved")
        _send_message(chat_id, f"Approved. Task queued: {task.task_id}")
        return True
    if action == "reject":
        task.status = "rejected"
        task.phase = "rejected"
        store.update_task(task)
        _answer_callback(callback_id, "Rejected")
        _send_message(chat_id, f"Task {task.task_id} rejected.")
        return True
    if action == "cancel":
        if task.pid:
            try:
                os.kill(task.pid, signal.SIGTERM)
            except Exception as exc:
                _log(f"Failed to kill pid {task.pid}: {exc!r}")
        task.status = "cancelled"
        task.phase = "cancelled"
        task.error = "Cancelled by user."
        task.pid = None
        store.update_task(task)
        _answer_callback(callback_id, "Cancelled")
        _send_message(chat_id, f"Task {task.task_id} cancelled.")
        return True
    if action == "status":
        _answer_callback(callback_id, "Status sent")
        lines = [_phase_text(task)]
        if task.plan:
            lines.append("Plan:")
            lines.extend(f"- {item}" for item in task.plan[:6])
        if task.needs_input_prompt:
            lines.append("Pending input:")
            lines.append(task.needs_input_prompt)
        _send_message(chat_id, "\n".join(lines))
        return True
    if action == "log":
        _answer_callback(callback_id, "Log sent")
        tail = _tail_log(_task_log_path(task.task_id), 20)
        _send_message(chat_id, f"Log tail (20 lines):\n{tail}")
        return True
    _answer_callback(callback_id, "Unsupported action")
    return False


def _build_retry_context(log_path: Path, error: str | None) -> str:
    retry_context = [
        "Retry instruction:",
        "- The previous attempt failed. Inspect the failure, fix the root cause, and retry end-to-end.",
    ]
    if error:
        retry_context.append("Previous error:\n" + error)
    tail = _tail_log(log_path, 80)
    if tail:
        retry_context.append("Recent log tail:\n" + tail)
    return "\n".join(retry_context)


def _run_task(chat_id: int, task, store: TaskStore, allowed_roots: list[str]) -> None:
    if not is_path_allowed(task.workdir, allowed_roots):
        task.status = "blocked"
        task.error = "Workdir not allowed"
        store.update_task(task)
        _send_message(chat_id, task.error)
        return

    task.status = "running"
    task.phase = "preparing"
    store.update_task(task)
    _send_action(chat_id, "typing")

    log_path = _task_log_path(task.task_id)
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"=== {datetime.utcnow().isoformat()}Z ===\n")
            log_file.write(f"task_id: {task.task_id}\n")
            log_file.write(f"workdir: {task.workdir}\n")
            log_file.write(f"provider: {store.get_chat_provider(chat_id) or settings.provider_default or 'codex'}\n")
            log_file.write(f"text: {task.text}\n\n")
    except Exception as exc:
        _log(f"failed to write log header for {task.task_id}: {exc!r}")

    projects = _list_projects(settings.project_root)
    preamble = _project_preamble(projects, task.workdir)
    docs = _project_docs_context(task.workdir)
    snapshot = _project_snapshot_context(task.workdir)
    git_ctx = _git_context(task.workdir)
    project_name = _project_name_for_path(task.workdir)
    memory = _memory_context(project_name, chat_id, settings.project_memory_context_lines)
    session = _session_context(project_name, chat_id)
    recent_tasks = _recent_task_context(store, chat_id, task.workdir)
    text_lower = (task.text or "").lower()
    wants_ios = any(key in text_lower for key in ("ios", "iphone", "ipad", "simulator", "模拟器", "苹果"))
    wants_web = any(key in text_lower for key in ("web", "chrome"))
    wants_android = any(key in text_lower for key in ("android", "pixel", "emulator", "安卓"))
    if ("flutter" in text_lower) and not (wants_ios or wants_web or wants_android):
        wants_ios = True
    needs_flutter = ("flutter" in text_lower) or wants_ios
    base_sections = [preamble, _execution_style_context()]
    sections = list(base_sections)
    if docs:
        sections.append(docs)
    if snapshot:
        sections.append(snapshot)
    if git_ctx:
        sections.append(git_ctx)
    if memory:
        sections.append(memory)
    if session:
        sections.append(session)
    if recent_tasks:
        sections.append(recent_tasks)
    if needs_flutter:
        sections.append(
            "Flutter note: SDK is already installed and writable. Do NOT propose copying the SDK. "
            "Run Flutter commands directly in the current project. Prefer iOS simulator if available."
        )
    if wants_ios:
        sections.append(
            "iOS note: prefer iOS simulator (default) and do NOT fall back to web/Chrome unless the user asked. "
            "Run `flutter devices` to pick an iOS simulator and then `flutter run -d <ios-device-id>` if needed. "
            "If `flutter run` fails with a generic 'Error launching application', immediately rerun with "
            "`flutter run -v -d <ios-device-id>` and capture full output to a log file, then show the tail. "
            "If CocoaPods is missing, report the error and stop."
        )
    sections.append(
        "Automation policy: inside the current workdir, proceed end-to-end without asking for confirmation for normal development work. "
        "Only pause for clearly destructive actions such as deleting real data, resetting git history, or operating outside the allowed project roots."
    )
    sections.append(
        "Output contract:\n"
        "- Do the work when possible, not just analysis.\n"
        "- Mention key files changed and key commands run.\n"
        "- If something fails, say exactly what failed and what you tried.\n"
    )

    def _set_pid(pid: int) -> None:
        task.pid = pid
        store.update_task(task)

    provider = store.get_chat_provider(chat_id) or settings.provider_default or "codex"
    if _should_auto_plan(task.text, task.workdir):
        task.phase = "planning"
        store.update_task(task)
        plan = _plan_task(
            task,
            store,
            provider,
            needs_flutter,
            base_sections + sections[2:],
            log_path,
        )
        if plan:
            _append_log_block(log_path, "plan", "\n".join(f"- {item}" for item in plan))
            _send_phase_update(chat_id, task, include_plan=True)

    attempts_allowed = 1 + max(0, settings.auto_retry_attempts if settings.auto_retry_on_failure else 0)
    output = None
    error = None
    completed_steps: list[str] = []
    steps = task.plan[:] if _should_subtask_execute(task, task.workdir) else []
    total_steps = len(steps)

    def _execute_prompt(prompt_sections: list[str], phase: str) -> tuple[str | None, str | None]:
        local_output = None
        local_error = None
        for attempt in range(1, attempts_allowed + 1):
            task.attempts = attempt
            task.phase = phase if attempt == 1 else f"retrying_{phase}"
            store.update_task(task)
            if attempt == 1 or attempt > 1:
                _send_phase_update(chat_id, task, include_plan=False)
            local_sections = list(prompt_sections)
            if attempt > 1:
                local_sections.append(_build_retry_context(log_path, local_error))
            task_prompt = "\n\n".join(local_sections)
            local_output, local_error = run_codex(
                settings,
                task_prompt,
                task.workdir,
                log_path=log_path,
                needs_flutter=needs_flutter,
                on_pid=_set_pid,
                provider=provider,
            )
            if not local_error:
                break
            if _needs_codex_approval(local_error):
                task.status = "needs_approval"
                task.phase = "awaiting_approval"
                task.error = local_error
                store.update_task(task)
                _send_approval_card(chat_id, task)
                return None, local_error
            if attempt < attempts_allowed:
                _append_log_block(log_path, f"retry-{phase}-{attempt}", local_error)
        return local_output, local_error

    if steps:
        task.total_steps = total_steps
        store.update_task(task)
        step_outputs: list[str] = []
        for idx, step in enumerate(steps, start=1):
            task.current_step = idx
            step_sections = list(sections)
            step_sections.append("Execution plan:\n" + "\n".join(f"{i+1}. {item}" for i, item in enumerate(steps)))
            if completed_steps:
                step_sections.append(
                    "Completed steps:\n" + "\n".join(f"- {item}" for item in completed_steps)
                )
            step_sections.append(
                "Current step:\n"
                f"- Execute only step {idx} of {total_steps}: {step}\n"
                "- Make the necessary changes for this step and briefly summarize the concrete work completed."
            )
            step_sections.append(f"Overall user request:\n{task.text}")
            output, error = _execute_prompt(step_sections, f"step_{idx}")
            if error:
                break
            completed_steps.append(step)
            if output:
                step_outputs.append(f"Step {idx}: {step}\n{output.strip()}")
        if not error:
            output = "\n\n".join(step_outputs).strip()
    else:
        attempt_sections = list(sections)
        if task.plan:
            attempt_sections.append("Execution plan:\n" + "\n".join(f"{i+1}. {item}" for i, item in enumerate(task.plan)))
        attempt_sections.append(f"User request:\n{task.text}")
        output, error = _execute_prompt(attempt_sections, "executing")

    latest = store.get_task(task.task_id)
    if latest and latest.status == "needs_approval":
        return

    if error:
        task.status = "failed"
        task.phase = "failed"
        task.error = error
        store.update_task(task)
        _append_memory(chat_id, task, "failed", error)
        _update_session_archive(chat_id, task, error)
        _update_status_doc(task, error)
        diag = diagnose_codex(settings)
        _send_message(chat_id, f"Task failed: {error}\n\nDiagnostics:\n{diag}")
        return

    if _needs_user_input(output):
        task.status = "needs_input"
        task.phase = "awaiting_input"
        task.needs_input_prompt = (output or "").strip()
        task.output = output
        store.update_task(task)
        _update_session_archive(chat_id, task, output or "")
        _update_status_doc(task, output or "")
        _send_input_card(chat_id, task)
        return

    if (_needs_ios_verbose_retry(wants_ios, output) or _log_has_ios_launch_error(log_path)) and not _log_has_diagnostics(log_path):
        diag = _run_flutter_verbose_diag(task.workdir)
        task.status = "failed"
        task.phase = "failed"
        task.error = "iOS launch failed. See diagnostics."
        task.output = (output or "") + "\n\nDiagnostics:\n" + diag
        store.update_task(task)
        _append_memory(chat_id, task, "failed", task.output or "")
        _update_session_archive(chat_id, task, task.output or "")
        _update_status_doc(task, task.output or "")
        _append_log_block(log_path, "diagnostics", task.output or "")
        _send_message(chat_id, task.output)
        return

    task.phase = "verifying"
    store.update_task(task)
    verify_ok, verify_summary = _run_verification(task.workdir, task.text, log_path)
    final_output = output or "(no output)"
    if verify_summary:
        final_output = final_output.rstrip() + "\n\n" + verify_summary

    task.status = "done" if verify_ok else "done_with_issues"
    task.phase = "completed" if verify_ok else "completed_with_issues"
    task.output = final_output
    store.update_task(task)
    _append_memory(chat_id, task, task.status, task.output or "")
    _update_session_archive(chat_id, task, final_output)
    _update_status_doc(task, final_output, verify_summary)
    _send_message(chat_id, task.output)


def run_bot() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    caffeinate_proc = None
    if settings.enable_caffeinate:
        try:
            caffeinate_proc = subprocess.Popen(["/usr/bin/caffeinate", "-dimsu"])
            _log("caffeinate started to prevent sleep.")
        except Exception as exc:
            _log(f"caffeinate failed to start: {exc!r}")

    _validate_proxy(settings.telegram_proxy)
    set_force_curl(settings.telegram_force_curl)
    store = TaskStore(settings.task_store_path)
    allowed_roots = parse_allowed_roots(settings.allowed_roots)
    allowed_chat_ids = _allowed_chat_ids()
    runner = TaskRunner(store, allowed_roots, settings.codex_max_workers)
    stale_after = settings.task_stale_sec
    if stale_after <= 0:
        if settings.codex_timeout_sec > 0:
            stale_after = settings.codex_timeout_sec + 60
        else:
            stale_after = 0
    if stale_after > 0:
        TaskWatchdog(store, stale_after)
    projects = _list_projects(settings.project_root)
    last_projects_refresh = time.monotonic()
    offset = 0

    print("Local TG bot started.")
    if allowed_chat_ids:
        first_id = next(iter(allowed_chat_ids))
        _send_message(first_id, "Local TG bot started.")
    try:
        while True:
            now = time.monotonic()
            if now - last_projects_refresh > 60:
                projects = _list_projects(settings.project_root)
                last_projects_refresh = now

            payload = {
                "offset": offset,
                "timeout": settings.telegram_long_poll_timeout_sec,
                "allowed_updates": ["message", "edited_message"],
            }
            resp = post(
                _base_url(),
                "/getUpdates",
                payload,
                timeout=65,
                proxy=settings.telegram_proxy,
            )
            if resp:
                updates = resp.get("result", [])
                if updates:
                    offset = int(updates[-1]["update_id"]) + 1
                    _log(f"received {len(updates)} updates; last_id={offset-1}")
                for update in updates:
                    callback = update.get("callback_query")
                    if callback:
                        callback_id = callback.get("id")
                        data = (callback.get("data") or "").strip()
                        message = callback.get("message") or {}
                        chat = message.get("chat") or {}
                        chat_id = chat.get("id")
                        if callback_id and chat_id is not None:
                            if allowed_chat_ids and chat_id not in allowed_chat_ids:
                                _answer_callback(callback_id, "Not allowed")
                                continue
                            _handle_callback(callback_id, chat_id, data, store, runner)
                        continue
                    message = update.get("message") or update.get("edited_message")
                    if not message:
                        continue
                    chat = message.get("chat") or {}
                    chat_id = chat.get("id")
                    if chat_id is None:
                        continue
                    if allowed_chat_ids and chat_id not in allowed_chat_ids:
                        _log(f"Ignored chat_id={chat_id}; not in TELEGRAM_ALLOWED_CHAT_IDS.")
                        continue
                    text = (message.get("text") or "").strip()
                    if not text:
                        continue
                    if _handle_command(chat_id, text, store, allowed_roots, runner, projects):
                        continue

                    workdir = store.get_chat_workdir(chat_id) or settings.default_workdir
                    mentioned = _find_project_in_text(text, projects)
                    if mentioned:
                        candidate = str(Path(settings.project_root) / mentioned)
                        if is_path_allowed(candidate, allowed_roots):
                            workdir = candidate
                            store.set_chat_workdir(chat_id, workdir)

                    requires_approval = False
                    if settings.require_approval_for_delete:
                        keywords = [k.strip() for k in settings.delete_keywords.split(",")]
                        requires_approval = needs_delete_approval(text, keywords)

                    task_id = _now_id()
                    task = store.create_task(task_id, chat_id, text, workdir, requires_approval)

                    if requires_approval:
                        task.status = "needs_approval"
                        task.phase = "awaiting_approval"
                        store.update_task(task)
                        _send_approval_card(chat_id, task)
                        continue
                    task.status = "queued"
                    store.update_task(task)
                    _send_message(chat_id, f"Task queued: {task.task_id}")
                    runner.enqueue(task.task_id, chat_id)

            time.sleep(settings.telegram_poll_interval_sec)
    finally:
        if caffeinate_proc and caffeinate_proc.poll() is None:
            caffeinate_proc.terminate()
