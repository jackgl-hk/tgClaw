from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from shutil import which


def _normalize_provider(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().lower()


def _select_provider(settings, provider: str | None) -> tuple[str | None, str | None, str]:
    provider_name = _normalize_provider(provider) or _normalize_provider(settings.provider_default)
    command = None
    args = None
    if provider_name:
        command = settings.provider_commands.get(provider_name)
        args = settings.provider_args.get(provider_name)
    if not command:
        command = settings.codex_task_command
    if args is None:
        args = settings.codex_task_args
    return command, args, provider_name


def run_codex(
    settings,
    task_text: str,
    workdir: str,
    log_path: Path | None = None,
    needs_flutter: bool = False,
    on_pid=None,
    provider: str | None = None,
) -> tuple[str | None, str | None]:
    cmd, cmd_args, provider_name = _select_provider(settings, provider)
    if not cmd:
        if provider_name:
            return None, f"Provider '{provider_name}' command is not set"
        return None, "CODEX_TASK_COMMAND is not set"

    env = os.environ.copy()
    _ensure_path(env)
    _add_project_flutter_to_path(env, workdir)
    resolved_cmd, resolve_error = _resolve_codex_command(cmd, env)
    if resolve_error:
        return None, resolve_error
    if not resolved_cmd:
        return None, "CODEX_TASK_COMMAND is not set"

    extra_add_dirs: list[str] = []
    if needs_flutter and settings.flutter_auto_add_dir:
        flutter_root = _detect_flutter_sdk(env, settings.flutter_sdk_path)
        if flutter_root:
            extra_add_dirs.append(str(flutter_root))
        pod_root = _detect_pod_root(env)
        if pod_root:
            extra_add_dirs.append(str(pod_root))
        for fallback in _flutter_fallback_roots():
            extra_add_dirs.append(str(fallback))
        for support_dir in _flutter_ios_support_dirs():
            extra_add_dirs.append(str(support_dir))

    command = _build_command(
        resolved_cmd,
        cmd_args,
        task_text,
        workdir,
        extra_add_dirs=extra_add_dirs,
    )

    try:
        result = _run_command_stream(
            command,
            workdir=workdir,
            env=env,
            timeout_sec=settings.codex_timeout_sec,
            force_tty=False,
            log_path=log_path,
            on_pid=on_pid,
        )
    except Exception as exc:
        return None, f"Codex command failed to start: {exc}"

    if result.timed_out:
        return None, f"Codex command timed out after {int(result.elapsed)}s"

    if result.returncode != 0 and _is_tty_error(result.output):
        try:
            result = _run_command_stream(
                command,
                workdir=workdir,
                env=env,
                timeout_sec=settings.codex_timeout_sec,
                force_tty=True,
                log_path=log_path,
                on_pid=on_pid,
            )
        except Exception as exc:
            return None, f"Codex command failed to start: {exc}"

    if result.timed_out:
        return None, f"Codex command timed out after {int(result.elapsed)}s"

    if result.returncode != 0:
        err = result.output.strip()
        return None, f"Codex command failed ({result.returncode}): {err}"
    return result.output.strip(), None


def _ensure_path(env: dict) -> None:
    path = env.get("PATH", "")

    extra_path = env.get("EXTRA_PATH") or env.get("PATH_EXTRA")
    if extra_path:
        for part in extra_path.split(os.pathsep):
            part = part.strip()
            if part and part not in path:
                path = part + os.pathsep + path

    for candidate in ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin"):
        if candidate not in path and Path(candidate).exists():
            path = candidate + os.pathsep + path

    bun_bin = str(Path.home() / ".bun" / "bin")
    if bun_bin not in path:
        path = bun_bin + os.pathsep + path

    if not which("pod", path=path):
        gem_root = Path.home() / ".gem" / "ruby"
        if gem_root.exists():
            for pod_bin in gem_root.glob("*/bin/pod"):
                path = str(pod_bin.parent) + os.pathsep + path
                break

    # Ensure node is discoverable for Codex CLI.
    if not which("node", path=path):
        candidates = []
        candidates.append(Path("/opt/homebrew/bin/node"))
        candidates.append(Path("/opt/homebrew/opt/node/bin/node"))
        candidates.append(Path("/opt/homebrew/opt/node@20/bin/node"))
        candidates.append(Path("/usr/local/bin/node"))
        candidates.append(Path.home() / ".volta" / "bin" / "node")
        candidates.append(Path.home() / ".asdf" / "shims" / "node")
        # nvm versions
        nvm_root = Path.home() / ".nvm" / "versions" / "node"
        if nvm_root.exists():
            for node_bin in nvm_root.glob("*/bin/node"):
                candidates.append(node_bin)
        for node_path in candidates:
            if node_path.exists():
                path = str(node_path.parent) + os.pathsep + path
                break

    env["PATH"] = path


def _add_project_flutter_to_path(env: dict, workdir: str) -> None:
    sdk_bin = Path(workdir) / ".flutter-sdk" / "bin"
    if sdk_bin.exists():
        path = env.get("PATH", "")
        sdk_str = str(sdk_bin)
        if sdk_str not in path:
            env["PATH"] = sdk_str + os.pathsep + path


def _build_command(
    resolved_cmd: str,
    codex_task_args: str | None,
    task_text: str,
    workdir: str,
    extra_add_dirs: list[str] | None = None,
) -> list[str]:
    args = shlex.split(codex_task_args) if codex_task_args else []
    if not args:
        args = [
            "exec",
            "--full-auto",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
        ]
    elif "exec" not in args:
        args = ["exec"] + args

    if "--cd" not in args and "-C" not in args:
        args += ["--cd", workdir]

    if extra_add_dirs:
        existing = set()
        for i, item in enumerate(args):
            if item == "--add-dir" and i + 1 < len(args):
                existing.add(args[i + 1])
        for path in extra_add_dirs:
            if path in existing:
                continue
            args += ["--add-dir", path]

    return [resolved_cmd] + args + [task_text]


def _detect_pod_root(env: dict) -> Path | None:
    pod = which("pod", path=env.get("PATH", ""))
    if not pod:
        return None
    pod_path = Path(pod).resolve()
    for root in (Path("/opt/homebrew"), Path("/usr/local")):
        try:
            pod_path.relative_to(root)
        except ValueError:
            continue
        return root
    gem_root = Path.home() / ".gem"
    try:
        pod_path.relative_to(gem_root)
    except ValueError:
        return pod_path.parent
    return gem_root


def _flutter_fallback_roots() -> list[Path]:
    roots = [Path("/opt/homebrew"), Path("/usr/local"), Path.home() / ".gem"]
    return [root for root in roots if root.exists()]


def _flutter_ios_support_dirs() -> list[Path]:
    home = Path.home()
    candidates = [
        Path("/Applications/Xcode.app"),
        Path("/Library/Developer/CommandLineTools"),
        home / ".cocoapods",
        home / ".pub-cache",
        home / "Library" / "Developer",
        home / "Library" / "Caches",
        home / "Library" / "Logs",
        home / "Library" / "Preferences",
        home / "Library" / "Application Support",
    ]
    return [path for path in candidates if path.exists()]


def _detect_flutter_sdk(env: dict, override: str | None) -> Path | None:
    if override:
        path = Path(override).expanduser()
        if path.exists():
            return path
    flutter_bin = which("flutter", path=env.get("PATH", ""))
    if not flutter_bin:
        return None
    path = Path(flutter_bin).resolve()
    if path.name == "flutter" and path.parent.name == "bin":
        root = path.parent.parent
        if root.exists():
            return root
    return path.parent


def diagnose_codex(settings) -> str:
    env = os.environ.copy()
    _ensure_path(env)

    lines: list[str] = []
    lines.append(f"codex command: {settings.codex_task_command or 'not set'}")

    resolved_cmd, resolve_error = _resolve_codex_command(settings.codex_task_command, env)
    if resolve_error:
        lines.append(f"codex resolved: {resolve_error}")
        return "\n".join(lines)

    if not resolved_cmd:
        lines.append("codex resolved: not found")
        return "\n".join(lines)

    lines.append(f"codex resolved: {resolved_cmd}")
    node_path = which("node", path=env.get("PATH", ""))
    lines.append(f"node: {node_path or 'not found'}")

    try:
        proc = _run_command(
            [resolved_cmd, "--version"],
            workdir=str(Path.home()),
            env=env,
            timeout_sec=20,
            force_tty=True,
        )
        if proc.returncode == 0:
            lines.append(f"codex --version: {proc.stdout.strip()}")
        else:
            err = proc.stderr.strip() or proc.stdout.strip()
            lines.append(f"codex --version failed ({proc.returncode}): {err}")
    except Exception as exc:
        lines.append(f"codex --version error: {exc}")

    return "\n".join(lines)


def _is_tty_error(stderr: str) -> bool:
    text = (stderr or "").lower()
    return "stdin is not a terminal" in text or "not a tty" in text


def _resolve_codex_command(cmd: str | None, env: dict) -> tuple[str | None, str | None]:
    if not cmd:
        resolved = which("codex", path=env.get("PATH", ""))
        return resolved, None

    if os.path.isabs(cmd):
        if Path(cmd).exists():
            return cmd, None
        bun_cmd = Path.home() / ".bun" / "bin" / "codex"
        if bun_cmd.exists():
            return str(bun_cmd), None
        return None, f"CODEX_TASK_COMMAND not found: {cmd}"

    resolved = which(cmd, path=env.get("PATH", ""))
    if resolved:
        return resolved, None
    return None, f"CODEX_TASK_COMMAND not found on PATH: {cmd}"


@dataclass
class StreamResult:
    returncode: int
    output: str
    timed_out: bool
    elapsed: float


def _run_command_stream(
    command: list[str],
    workdir: str,
    env: dict,
    timeout_sec: int,
    force_tty: bool,
    log_path: Path | None,
    on_pid=None,
) -> StreamResult:
    final_cmd = command
    if force_tty:
        script_path = Path("/usr/bin/script")
        if not script_path.exists():
            raise RuntimeError("Pseudo-TTY required but /usr/bin/script not found")
        final_cmd = [str(script_path), "-q", "/dev/null"] + command

    timeout = None if timeout_sec <= 0 else timeout_sec
    tail = deque(maxlen=200)
    start = time.monotonic()

    log_file = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8")

    proc = subprocess.Popen(
        final_cmd,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        bufsize=1,
    )
    if on_pid:
        try:
            on_pid(proc.pid)
        except Exception:
            pass

    def _reader() -> None:
        if not proc.stdout:
            return
        for line in proc.stdout:
            tail.append(line)
            if log_file:
                log_file.write(line)
                log_file.flush()

    reader = threading.Thread(target=_reader, name="codex-log-reader", daemon=True)
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    finally:
        if proc.stdout:
            proc.stdout.close()
        reader.join(timeout=2)
        if log_file:
            log_file.close()

    elapsed = time.monotonic() - start
    output = "".join(tail).strip()
    return StreamResult(proc.returncode or 0, output, timed_out, elapsed)


def _run_command(
    command: list[str],
    workdir: str,
    env: dict,
    timeout_sec: int,
    force_tty: bool,
) -> subprocess.CompletedProcess:
    final_cmd = command
    if force_tty:
        script_path = Path("/usr/bin/script")
        if not script_path.exists():
            raise RuntimeError("Pseudo-TTY required but /usr/bin/script not found")
        final_cmd = [str(script_path), "-q", "/dev/null"] + command

    timeout = None if timeout_sec <= 0 else timeout_sec
    return subprocess.run(
        final_cmd,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
