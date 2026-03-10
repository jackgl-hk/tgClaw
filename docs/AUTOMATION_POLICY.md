# Automation Policy

Goal: minimize interactive approvals while keeping safety controls.

Rules
- Allowed execution roots: `ALLOWED_ROOTS` from `.env`.
- New file creation is allowed without asking.
- Any deletion or destructive action requires approval via Telegram.
- If approval is needed, the bot asks in Telegram with `/approve <id>` or `/reject <id>`.
- Avoid terminal prompts; use Telegram as the approval channel.

Config knobs
- `REQUIRE_APPROVAL_FOR_DELETE=1`
- `DELETE_KEYWORDS=delete,remove,rm,trash,cleanup,drop,reset,wipe`
- `TELEGRAM_ALLOWED_CHAT_IDS=...`

Operational note
- System-level changes (brew install, launchctl edits, sudo, etc.) still require explicit confirmation by the owner.
