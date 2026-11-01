# Telegram Knowledge Hub

A private Telegram bot that acts as Sahil's mobile front door. You send it reels, videos, photos, voice notes, links and notes. It files each one into a searchable archive with an AI summary. Claude and ChatGPT can read that archive through an MCP connector.

```
Phone ──Telegram──▶ /telegram/webhook ──▶ queue ──▶ yt-dlp / Telegram download
                                                  ├─ ffmpeg: frames + audio
                                                  ├─ Whisper: transcript
                                                  ├─ Claude: summary, key points, techniques, tags, skill idea
                                                  └─ SQLite + FTS5 on /data volume
Claude / ChatGPT ──MCP──▶ /s/<MCP_SECRET>/mcp  (search · fetch · get_frames · list_recent · add_note · list_skill_candidates)
Browser ──▶ /view/<MCP_SECRET>/item/<id>       (private read-only item page)
```

## What you can send

| Send | What happens |
|---|---|
| Instagram / TikTok / YouTube / X link (+ optional note) | Downloads it, takes 4–16 frames, transcribes it, analyses it, and replies with a summary |
| A video or photo from your camera roll (up to 20 MB) | Same pipeline as a link |
| A voice note | Transcribed. If it starts with "todo …" it goes to Todoist; if it starts with "queue …" it goes to the overnight agents; anything else is saved as a note |
| Plain text | Saved as a note. `#tags` are picked up |
| `/todo`, `/queue`, `/ask`, `/recent`, `/get N`, `/delete N`, `/status` | See `/help` in the bot |

No patient information should go through this bot. The analyser flags anything that looks like PHI.

## Environment variables (Railway → service → Variables)

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Get it from @BotFather |
| `CLAIM_CODE` | yes | Already set. The first person to send `/start <code>` becomes the only allowed user |
| `ANTHROPIC_API_KEY` | yes, for analysis | From console.anthropic.com |
| `CLAUDE_MODEL` | no | Default `claude-sonnet-5-5` |
| `OPENAI_API_KEY` | no | When set, transcription uses OpenAI Whisper. Otherwise it uses local faster-whisper on the server, which is free but slower |
| `TODOIST_API_TOKEN` | for /todo and /queue | Todoist → Settings → Integrations → Developer |
| `TODOIST_PROJECT_ID` | no | Defaults to the Inbox |
| `TODOIST_QUEUE_LABEL` | no | Default `agent`, which matches the overnight agent queue |
| `IG_COOKIES` | recommended | Netscape `cookies.txt` contents from a logged-in **burner** Instagram account. Without it, Instagram often blocks downloads |
| `MCP_SECRET` | yes | Already set. It is part of the connector URL, so treat that URL like a password |
| `TELEGRAM_ALLOWED_USER_IDS` | no | An alternative to claiming: a comma-separated list of Telegram user IDs |
| `DEFAULT_TEXT_ACTION` | no | `note` (default) or `todo` |
| `KEEP_VIDEO` | no | `1` keeps the original video files. By default only the frames are kept, to save space |

## Getting Instagram cookies (burner account)

1. Create a throwaway Instagram account and log into it in a desktop browser.
2. Install a "Get cookies.txt LOCALLY"-type extension and export the cookies for instagram.com.
3. Paste the whole file into the `IG_COOKIES` variable.

When downloads start failing, log in again and re-export. yt-dlp updates itself every time the service restarts.

## Running tests locally

```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -q
```

## Known limits
- Bots can only fetch Telegram uploads up to 20 MB. For anything bigger, send a link.
- Instagram downloads depend on yt-dlp and cookies, and they will break from time to time.
- The archive is a single SQLite file on the Railway volume (`/data/hub.db`). Back it up occasionally.
