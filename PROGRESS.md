# PROGRESS — Telegram Knowledge Hub

## Done
- v1 code complete (app/): webhook, claim-code auth, link/video/photo/voice pipeline, notes, Todoist /todo + /queue,
  /ask (FTS5 + Claude), /recent /get /delete /status, MCP connector (search, fetch, get_frames, list_recent,
  add_note, list_skill_candidates), private item viewer.
- 6 end-to-end tests passing (ffmpeg real; Telegram/Claude/Todoist/transcription mocked).
- Dockerfile + railway.json ready.

## Blocked / next
- GitHub integration can't create repos (403). Need Sahil to create empty private repo SBhar-AI/telegram-hub,
  then: add_repo(push) -> git push -> Railway project (service from repo + volume at /data) -> set vars -> domain.
- Not yet verified against live APIs: Telegram, Anthropic, Todoist, yt-dlp on Instagram (sandbox has no access).
