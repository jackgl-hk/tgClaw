# TG Bot Task Runner (Codex / LLM CLI)

A local Telegram bot that lets you trigger Codex (or another LLM CLI) tasks on your own machine.

Key behaviors:
- Only operates inside your configured project root (see `.env`).
- Creating new files is allowed without asking.
- Routine development work inside the current workdir runs without asking.
- Clearly destructive actions still require approval via Telegram.
- No streaming; tasks are queued and you get a task id immediately.
- On failure, it runs a quick self-check (Codex + Node) and includes diagnostics.
- If Codex emits an approval prompt, the bot will forward it and wait for /approve or /reject.
- Each task writes a log file; use `/log <id>` to fetch the tail.
- The bot detects project names in your message and adds a project index (with a short summary for the current project) to the prompt.
- Use `/switch <project>` to save memory and change projects; `/leave` clears the active project.
- Use `/tasks` to see queue stats and recent task ids by project.
- Use `/current` to show the current project/workdir and last task id.
- Use `/provider <name>` (alias `/model`) to switch CLI provider per chat.
- Flutter + emulators can be used on the host; `flutter run` should be executed in the current project folder when requested.
- Common Flutter commands supported: `flutter pub get`, `flutter analyze`, `flutter run`, `flutter test`, `flutter build`, `flutter clean`.
- After code tasks, the bot runs basic automatic verification when it can detect the project type. For Flutter this includes `flutter pub get` and `flutter analyze`.
- For non-trivial code tasks, the bot first generates a short execution plan, then executes, then verifies.
- If execution fails, the bot can do one automatic repair retry with the previous error and log tail as context.
- Use `/cancel <id>` to stop a running task.
- The bot auto-loads key Markdown files from the project folder into the prompt (includes `service/README.md` and `docs/DEPLOYMENT*.md` when present).
- If a task mentions Flutter, the bot automatically adds the Flutter SDK directory to the Codex sandbox (override with `FLUTTER_SDK_PATH`).

Quick start:
1. Copy `.env.example` to `.env` and fill in values.
2. `make setup`
3. `make bot`

How it works:
- Telegram long-polling receives your messages.
- If the message is a command (`/status`, `/log`, `/projects`, etc.), the bot handles it immediately.
- Otherwise it is treated as a task and sent to your CLI via `CODEX_TASK_COMMAND` (default `codex`).
- Output is captured to a log file; the bot replies with status and lets you fetch logs.
- If a task requests approval (destructive actions), the bot pauses and waits for `/approve` or `/reject`.
- Successful code tasks include an automatic verification summary in the reply and the task log.
- `/status` and `/tasks` include task phase and attempt count so you can see whether a task is planning, executing, retrying, or verifying.

Entry / wake-up:
- The entry point is the Telegram chat itself.
- Send any message to the bot to “wake” it and queue a task.
- Use `/start` or `/help` to get the command list.

Model entry:
- This bot does not call model APIs directly. It delegates to your CLI.
- Configure the CLI path and args via `CODEX_TASK_COMMAND` and `CODEX_TASK_ARGS`.
- Set the API keys required by your CLI in `.env` (examples are listed in `.env.example`).
- Optional: use `PROVIDER_DEFAULT` and `PROVIDER_COMMANDS` to define multiple providers (e.g., `kimi`) and switch with `/provider`.

OpenClaw alignment (concepts):
- Gateway: Telegram bot + command router.
- Brain: provider CLI (Codex / Kimi / other) selected per chat.
- Hands: local execution via Codex sandbox and filesystem access.
- Memory: per-project memory + task logs.

OpenClaw-inspired extensions (planned):
- Channels: keep Telegram as the default, add more channels later.
- Routing: keep per-chat isolation; extend to multi-agent routing if needed.
- Heartbeat: optional periodic check-ins via a scheduled job.

Docs:
- docs/TELEGRAM_SETUP.md
- docs/USAGE.md
- docs/SECURITY.md
- docs/AUTOMATION_POLICY.md
- skills/README.md

Launchd logs:
- `~/Library/Logs/local-tg.out.log`
- `~/Library/Logs/local-tg.err.log`

Codex path tip:
- If installed via Bun, use `~/.bun/bin/codex` or leave `CODEX_TASK_COMMAND=codex` and ensure PATH includes `~/.bun/bin`.
- Codex CLI requires `node` on PATH (Homebrew installs it in `/opt/homebrew/bin`).
- Tasks run via `codex exec` by default to avoid interactive hangs.
- `CODEX_MAX_WORKERS` controls parallel Codex workers (default 1).
- `TASK_STALE_SEC` controls when long-running tasks are auto-failed (0 = timeout + 60s; if both are 0, watchdog is disabled).

Telegram connectivity:
- If Telegram API is unreachable, set `TELEGRAM_PROXY` to an HTTPS proxy URL in `.env`.
- The bot falls back to `curl` if the default HTTP client fails.
- Set `TELEGRAM_FORCE_CURL=1` to always use `curl`.

Power:
- Set `ENABLE_CAFFEINATE=1` to keep the Mac awake.
- Lid-closed operation requires clamshell mode (external display + power + input) or a dedicated keep-awake tool.
