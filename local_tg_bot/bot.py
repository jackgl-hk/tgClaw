from __future__ import annotations

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
from urllib.parse import urlparse

from .config import settings
from .policy import parse_allowed_roots, is_path_allowed, needs_delete_approval
from .runner import run_codex, diagnose_codex, _ensure_path, _add_project_flutter_to_path
from .store import TaskStore
from .telegram import post, split_text, set_force_curl


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


def _send_message(chat_id: int, text: str) -> None:
    for chunk in split_text(text, settings.telegram_message_max_chars):
        payload = {"chat_id": chat_id, "text": chunk}
        resp = post(_base_url(), "/sendMessage", payload, proxy=settings.telegram_proxy)
        if not resp:
            _log(f"sendMessage failed for chat_id={chat_id}")


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
            "/tasks [project|all] | /log <id> [lines] | /selfcheck | /approve <id> | /reject <id> | "
            "/cancel <id> | /reset\n"
            "Notes: Flutter commands (flutter run/pub get/analyze/test/build/clean) run in current project.",
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
            _send_message(chat_id, f"{task.task_id}: {task.status}")
            return True
        tasks = store.list_tasks_for_chat(chat_id)[-5:]
        if not tasks:
            _send_message(chat_id, "No tasks.")
            return True
        lines = [f"{t.task_id}: {t.status}" for t in tasks]
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
            lines.append(f"{t.task_id} [{t.status}] ({proj})")
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
            lines.append(f"Last task: {last_task.task_id} [{last_task.status}]")
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
        _send_message(chat_id, f"Approved. Task queued: {task.task_id}")
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


def _run_task(chat_id: int, task, store: TaskStore, allowed_roots: list[str]) -> None:
    if not is_path_allowed(task.workdir, allowed_roots):
        task.status = "blocked"
        task.error = "Workdir not allowed"
        store.update_task(task)
        _send_message(chat_id, task.error)
        return

    task.status = "running"
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
    project_name = _project_name_for_path(task.workdir)
    memory = _memory_context(project_name, chat_id, settings.project_memory_context_lines)
    text_lower = (task.text or "").lower()
    wants_ios = any(key in text_lower for key in ("ios", "iphone", "ipad", "simulator", "模拟器", "苹果"))
    wants_web = any(key in text_lower for key in ("web", "chrome"))
    wants_android = any(key in text_lower for key in ("android", "pixel", "emulator", "安卓"))
    if ("flutter" in text_lower) and not (wants_ios or wants_web or wants_android):
        wants_ios = True
    needs_flutter = ("flutter" in text_lower) or wants_ios
    sections = [preamble]
    if docs:
        sections.append(docs)
    if memory:
        sections.append(memory)
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
    sections.append(f"User request:\n{task.text}")
    task_prompt = "\n\n".join(sections)

    def _set_pid(pid: int) -> None:
        task.pid = pid
        store.update_task(task)

    provider = store.get_chat_provider(chat_id) or settings.provider_default or "codex"
    output, error = run_codex(
        settings,
        task_prompt,
        task.workdir,
        log_path=log_path,
        needs_flutter=needs_flutter,
        on_pid=_set_pid,
        provider=provider,
    )
    if error:
        if _needs_codex_approval(error):
            task.status = "needs_approval"
            task.error = error
            store.update_task(task)
            _send_message(
                chat_id,
                f"Codex approval required for task {task.task_id}. Reply /approve {task.task_id} or /reject {task.task_id}",
            )
            return
        task.status = "failed"
        task.error = error
        store.update_task(task)
        _append_memory(chat_id, task, "failed", error)
        diag = diagnose_codex(settings)
        _send_message(chat_id, f"Task failed: {error}\n\nDiagnostics:\n{diag}")
        return

    if (_needs_ios_verbose_retry(wants_ios, output) or _log_has_ios_launch_error(log_path)) and not _log_has_diagnostics(log_path):
        diag = _run_flutter_verbose_diag(task.workdir)
        task.status = "failed"
        task.error = "iOS launch failed. See diagnostics."
        task.output = (output or "") + "\n\nDiagnostics:\n" + diag
        store.update_task(task)
        _append_memory(chat_id, task, "failed", task.output or "")
        try:
            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write("\n[diagnostics]\n")
                log_file.write(task.output or "")
                log_file.write("\n")
        except Exception as exc:
            _log(f"failed to write diagnostics for {task.task_id}: {exc!r}")
        _send_message(chat_id, task.output)
        return

    task.status = "done"
    task.output = output or "(no output)"
    store.update_task(task)
    _append_memory(chat_id, task, "done", task.output or "")
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
                        store.update_task(task)
                        _send_message(
                            chat_id,
                            f"Approval required for task {task.task_id}. Reply /approve {task.task_id} or /reject {task.task_id}",
                        )
                        continue
                    task.status = "queued"
                    store.update_task(task)
                    _send_message(chat_id, f"Task queued: {task.task_id}")
                    runner.enqueue(task.task_id, chat_id)

            time.sleep(settings.telegram_poll_interval_sec)
    finally:
        if caffeinate_proc and caffeinate_proc.poll() is None:
            caffeinate_proc.terminate()
