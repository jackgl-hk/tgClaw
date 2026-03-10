# Telegram Setup

1. Create a bot with BotFather and get the token.
2. Get your chat_id (start the bot and inspect updates).
3. Put the token and chat_id in `.env`.

Example:
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Logs (when running as launchd):
- `~/Library/Logs/local-tg.out.log`
- `~/Library/Logs/local-tg.err.log`
