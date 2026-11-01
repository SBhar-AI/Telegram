# PROGRESS — Telegram Knowledge Hub

## Done
- v1 code complete (see README). 6 end-to-end tests passing locally.
- Repo: github.com/SBhar-AI/Telegram (main).
- Railway project `telegram-hub` (28143b2c-372c-4216-8a94-49182187d90d), service `bot`
  (004f02b1-8e9d-476c-b916-d0bf27803990), volume hub-data at /data,
  domain https://bot-production-85a8.up.railway.app. Vars set: CLAIM_CODE, MCP_SECRET, DATA_DIR, PORT, PUBLIC_URL,
  TODOIST_QUEUE_LABEL=agent, TODOIST_CAPTURE_LABEL=telegram.

## Waiting on Sahil
- TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, TODOIST_API_TOKEN (optional: OPENAI_API_KEY, IG_COOKIES) in Railway vars.
- Claim the bot via t.me/<botname>?start=<CLAIM_CODE>.

## Not yet verified live
- Telegram webhook registration, Claude analysis call, Todoist v1 endpoint, yt-dlp on Instagram, local whisper on Railway.
