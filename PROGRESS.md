# PROGRESS — Telegram Knowledge Hub

## Done
- v1 code complete (see README). 6 end-to-end tests passing locally.
- Repo: github.com/SBhar-AI/Telegram (main).
- Railway project `telegram-hub` (28143b2c-372c-4216-8a94-49182187d90d), service `bot`
  (004f02b1-8e9d-476c-b916-d0bf27803990), volume hub-data at /data,
  domain https://bot-production-85a8.up.railway.app. Vars set: CLAIM_CODE, MCP_SECRET, DATA_DIR, PORT, PUBLIC_URL,
  TODOIST_QUEUE_LABEL=agent, TODOIST_CAPTURE_LABEL=telegram.

## v2 (2026-11-01): resource discovery + skill inbox
- Analysis extracts resources; Claude + web_search_20250305 resolves them; GitHub repos enriched (README, stars, SKILL.md files).
- Direct GitHub and article links are handled; skill drafts go to the skill_inbox (MCP) and the /skills command.
- DB migration adds resources + skill_draft columns (FTS rebuilt). 7 tests passing.
- Live bot: @UhuraSRBBot, claimed by the owner. Webhook OK.

## Waiting on Sahil
- Optional: GITHUB_TOKEN, IG_COOKIES, OPENAI_API_KEY.
- Connect the MCP URL in Claude Settings → Connectors so the skill inbox can be reviewed.

## Not yet verified live
- Telegram webhook registration, Claude analysis call, Todoist v1 endpoint, yt-dlp on Instagram, local whisper on Railway.
