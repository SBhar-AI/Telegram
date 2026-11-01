# Telegram Knowledge Hub

A private Telegram bot that acts as Sahil's mobile front door. You send it reels, videos, photos, voice notes, links and notes. It files each one into a searchable archive with an AI summary. Claude and ChatGPT can read that archive through an MCP connector.

```
Phone ──Telegram──▶ /telegram/webhook ──▶ queue ──▶ yt-dlp / Telegram download
                                                  ├─ ffmpeg: frames + audio
                                                  ├─ Whisper: transcript
                                                  ├─ Claude: summary, key points, techniques, tags, skill idea
                                                  └─ SQLite + FTS5 on /data volume
Claude / ChatGPT ──MCP──▶ /s/<MCP_SECRET>/mcp  (search · fetch · get_frames · list_recent · add_note · list_skill_candidates ·
                                                 list_resources · skill_inbox · get_skill_draft · mark_skill_draft)
Browser ──▶ /view/<MCP_SECRET>/item/<id>       (private read-only item page)
```

## The loop: see it → send it → strengthen Claude

1. You send a reel/video/link to **@UhuraSRBBot**.
2. It is analysed (frames + transcript + caption): summary, key points, reusable techniques.
3. **Resources** it mentions, shows or advertises (GitHub repos, Claude skills, prompts, tools, papers, "comment X to get it", "link in bio") are extracted and **resolved with web search** to real public links. GitHub repos are checked for stars, last update, license and **SKILL.md files**, which are pulled in.
4. If there is something skill-worthy, a **skill draft** (a complete SKILL.md tailored to Sahil) is written and placed in the **skill inbox**.
5. **Security screening.** Every GitHub repo and every skill draft is screened against Anthropic's *Skills for enterprise* checklist. The bot downloads a snapshot pinned to a specific commit (it never runs any of it) and scans every file for the doc's risk categories plus supply-chain red flags. It checks provenance (age, stars, license, whether it's a fork). Then a separate Claude reviewer, which did not write the draft, gives a 🟢/🟡/🔴 verdict with **why** and **how to make it safer**. Skill files from 🔴 repos are never reused, and a 🔴 draft is **blocked**: the connector refuses to mark it installed. `/security N` shows the full report.
6. In Claude, say "review my skill inbox". Claude reads the draft through the connector, critiques it, and shows it as a skill card you can save. Third-party skill content is treated as untrusted and flagged.

## What you can send

| Send | What happens |
|---|---|
| Instagram / TikTok / YouTube / X link (+ optional note) | Downloads it, takes 4–16 frames, transcribes it, analyses it, and replies with a summary |
| A video or photo from your camera roll (up to 20 MB) | Same pipeline as a link |
| A voice note | Transcribed. If it starts with "todo …" it goes to Todoist; if it starts with "queue …" it goes to the overnight agents; anything else is saved as a note |
| A GitHub link | Reads the repo (README, stars, license), lists its Claude skills, and drafts a skill for you |
| An article / web link | Reads the page, summarises it, and finds the resources it mentions |
| Plain text | Saved as a note. `#tags` are picked up |
| `/resources`, `/skills`, `/find N`, `/skill N` | Browse found resources, see the skill inbox, re-run resource search, or force a skill draft |
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
| `GITHUB_TOKEN` | no | A read-only GitHub token. It raises the GitHub API limit from 60 to 5,000 requests an hour |
| `FIND_RESOURCES` | no | `1` (default) looks up resources automatically. `0` makes it on-demand only (`/find N`) |
| `MAX_REPO_SCREENS` | no | Maximum repos screened automatically per item (default 3) |
| `MAX_SEARCHES` | no | Maximum web searches per item (default 6, at about $0.01 per search) |
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
