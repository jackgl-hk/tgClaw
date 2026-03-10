# Automation Policy

Goal: minimize interactive approvals while keeping safety controls.

Rules
- Allowed execution roots: `ALLOWED_ROOTS` from `.env`.
- New file creation is allowed without asking.
- Routine development work inside the active workdir should run automatically.
- Approval is reserved for clearly destructive actions such as deleting real data, resetting git history, or targeting paths outside the normal project workspace.
- If approval is needed, the bot asks in Telegram with `/approve <id>` or `/reject <id>`.
- Avoid terminal prompts; use Telegram as the approval channel.
- After code tasks, the bot should run basic verification commands automatically when it can detect the project type.

Config knobs
- `REQUIRE_APPROVAL_FOR_DELETE=1`
- `DELETE_KEYWORDS=delete,remove,rm,trash,cleanup,drop,reset,wipe`
- `AUTO_VERIFY=1`
- `AUTO_VERIFY_TIMEOUT_SEC=300`
- `TELEGRAM_ALLOWED_CHAT_IDS=...`

Operational note
- System-level changes (brew install, launchctl edits, sudo, etc.) still require explicit confirmation by the owner.
