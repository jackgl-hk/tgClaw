# Usage

Commands:
- /start or /help: show help
- /workdir <path>: set working directory for your chat
- /switch <project>: save memory and switch project
- /leave: save memory and reset to default workdir
- /status [id]: show task status
- /tasks [project|all]: show recent tasks for a project (or all) plus queue stats
- /current: show current project/workdir and last task id for this chat
- /log <id> [lines]: show last lines from a task log (default 20)
- /selfcheck: run Codex/node diagnostics
- /approve <id>: approve a destructive task
- /cancel <id>: cancel a running task
- /reject <id>: reject a task
- /projects: list available project folders with short summaries + server hints
- /providers: list available providers
- /provider <name> or /model <name>: switch provider for this chat
- /reset: clear tasks for this chat

Any other text is treated as a task and queued for Codex (you'll get a task id immediately).
Project detection: if your message mentions a known project name, the bot switches to that folder automatically and includes the project list in the Codex prompt.
Project memory: each completed task is appended to a per-project memory file. Switching/leave will save a memory marker and clear the active project.
Flutter: the host has Flutter + emulators installed; running `flutter run` should happen in the current project folder without extra prompts.
Common Flutter commands supported: `flutter pub get`, `flutter analyze`, `flutter run`, `flutter test`, `flutter build`, `flutter clean`.
Project docs: the bot auto-loads key Markdown files (like `AGENTS.md`, `README.md`, `task.md`, `service/README.md`, `docs/DEPLOYMENT*.md`) from the project folder and includes them in the Codex prompt.
Flutter SDK: when a task mentions Flutter, the bot auto-allows the Flutter SDK directory in the Codex sandbox (configurable via `FLUTTER_SDK_PATH`).
If Codex reports an approval prompt, the bot will ask you to `/approve` or `/reject`.
If a task fails, the bot will include a short diagnostics block automatically.

Provider selection:
- Use `PROVIDER_COMMANDS` in `.env` to map provider names to CLI commands.
- Switch provider per chat with `/provider <name>` (alias `/model`).
Example:
```
PROVIDER_DEFAULT=codex
PROVIDER_COMMANDS=codex=codex,kimi=kimi
PROVIDER_ARGS=codex=,kimi=
```

Codex path note:
- If Codex is installed via Bun, it lives at `~/.bun/bin/codex`.
- The bot adds `~/.bun/bin` to PATH automatically and falls back to it if your configured path is missing.
- The bot runs `codex exec` by default for non-interactive execution.
- For parallel Codex runs, set `CODEX_MAX_WORKERS` (default 1).
- Stale running tasks are auto-failed after `TASK_STALE_SEC` (0 = `CODEX_TIMEOUT_SEC + 60`; if both are 0, watchdog is disabled).

Telegram connectivity note:
- If the bot cannot reach Telegram (common on restricted networks), set `TELEGRAM_PROXY` to a reachable HTTPS proxy URL.
- The bot falls back to `curl` if the HTTP client fails (useful with unstable proxies).
- You can force `curl` for all Telegram calls via `TELEGRAM_FORCE_CURL=1`.

Power note:
- To keep the Mac awake, set `ENABLE_CAFFEINATE=1` (launches `/usr/bin/caffeinate -dimsu`).
- Closing the lid still sleeps the Mac unless you use clamshell mode (external display + power + input).

Automation policy is documented in `docs/AUTOMATION_POLICY.md`.
