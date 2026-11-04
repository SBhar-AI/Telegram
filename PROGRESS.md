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

## v3 (2026-11-01): security screening in the workflow
- app/scanner.py (static scan, same rules as tools/scan_skill.py) + app/security.py: pinned-SHA tarball fetch with safe extract
  (no links, no path escapes, size caps), provenance flags, independent Claude reviewer (security_verdict tool) -> verdict +
  why + make_safer; the scanner tier sets a floor (HIGH/BLOCK can't be rated green).
- Wired into GitHub links, resource resolution (max 3 repos/item) and skill drafts (red -> status blocked); /security N;
  MCP mark_skill_draft refuses red; skill_inbox and list_resources expose verdicts. 9 tests passing.
- skill-security-screen skill saved by the user (Claude-side fresh review before install).

## v4 (2026-11-01): fix + automatic retries
- Fixed: claude-sonnet-5-5 rejects forced tool_choice (400) -> tool_choice=auto + instruction + one nudge.
- Jobs retry automatically (MAX_ATTEMPTS=3, RETRY_DELAYS=30,120); after the 3rd failure one Telegram message lists all errors
  and sets needs_attention (/failed). Pending jobs are persisted in item meta and resumed after restarts.
  Item meta is now merged, not overwritten. 14 tests passing.

## v5 (2026-11-01): exact links + one-message breakdown (approved by Sahil)
- Repo slugs in the post are confirmed on GitHub directly; web search only for unresolved items, with generic results
  demoted to "exact link not found" + how the creator says to get it. GitHub rate-limit fallback.
- Reply flow: quick ack -> resources -> security screening (all repos, max 8) -> skill draft -> ONE breakdown message
  (each item the video covers: what it does, link, stars, verdict + safer path; For you; skill draft; caveats).
- Debug: /view/<secret>/json/<id> and /view/<secret>/item/<id>/refind (use a cache-busting query when fetching).

## v6 (2026-11-04): reliability + Instagram photo posts
- Telegram calls retry network errors/429 and never crash handlers; saves are queued before the ack; orphaned
  "processing" link saves are re-queued on restart; long HTML replies split with balanced tags.
- Photo/carousel posts: yt-dlp fails on these, and the old page fallback only saw Instagram's login wall. Now
  media.instagram_photos() gets the slide images (IG media API if IG_COOKIES set -> embed page -> og:image preview)
  and they're analysed slide by slide. /retry photos re-reads old Instagram posts that never had images read.
- Known gap: a carousel whose first slide is a video is still handled by yt-dlp as that one video (other slides skipped).
- 20 tests passing.

## Waiting on Sahil
- Optional: GITHUB_TOKEN, IG_COOKIES, OPENAI_API_KEY.
- Connect the MCP URL in Claude Settings → Connectors so the skill inbox can be reviewed.

## Not yet verified live
- Telegram webhook registration, Claude analysis call, Todoist v1 endpoint, yt-dlp on Instagram, local whisper on Railway.
