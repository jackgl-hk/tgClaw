# Architecture

Telegram -> Bot (long polling) -> Task router -> LLM CLI (Codex or compatible) -> Telegram response.

Entry / wake-up:
- Any message to the bot triggers routing; commands are handled immediately, others become tasks.

Core concepts (aligned with OpenClaw-style agent roles):
- Gateway: Telegram bot + command router.
- Brain: Provider CLI (Codex / Kimi / other) selected per chat.
- Hands: Codex sandbox execution on your local filesystem.
- Memory: per-project memory logs + task logs.
- Skills: reusable runbooks stored under `skills/`.
- Router: command parser that decides immediate response vs queued task.
- Channels: Telegram (current). Additional channels can be added with new adapters.
- Heartbeat: not enabled by default; can be added as a scheduled job.

Safety:
- Only runs inside `ALLOWED_ROOTS` from `.env`.
- Destructive requests require approval.
