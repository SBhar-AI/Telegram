# Cost report: Telegram Knowledge Hub (@UhuraSRBBot)

Branch `cost-optimization`. Nothing here is deployed. Railway only deploys `main`, so none of this is live until you
merge it.

## What was checked, and what wasn't

- **Measured (from your Anthropic Console, read Nov 6, 23:49 UTC):** $18.52 over 30 days. That is $16.78 for tokens
  plus $1.74 for web searches (174 searches), all on Claude Sonnet 5.5. Daily token cost was about $1 on Nov 1, $2 on
  Nov 2, $4 on Nov 3, $8.7 on Nov 4 and $0.9 on Nov 5. These were read off a bar chart, so they are approximate.
  About 105 items were saved Nov 1–6, which works out to roughly **$0.15–0.20 per item**.
- **Looked up today (Nov 7) from the official price page**
  (https://platform.claude.com/docs/en/about-claude/pricing):

  | | Input | Output | Cache read | Notes |
  |---|---|---|---|---|
  | Claude Sonnet 5.5 | $2.00 / million tokens | $10.00 / M | $0.10 / M | 5-minute cache write $2.50 / M |
  | Claude Haiku 5.5 | $0.10 / M | $0.50 / M | $0.01 / M | Prompts over 100k tokens: $0.50 / $2.50 |
  | Web search | $10 per 1,000 searches | | | Failed searches aren't billed |
  | Batch API | 50% off input and output | | | Results can take up to 24 h |

- **Also looked up** (https://platform.claude.com/docs/en/build-with-claude/thinking and `/effort`): **Sonnet 5.5
  "thinks" before answering on every request, and its default effort is "high". Those thinking tokens are hidden but
  are billed as output at $10/M**, and they count toward each call's output limit. This is a big part of the cost,
  and the bot never controlled it.
- **Estimated (not measured):** everything per step below. There was no per-call usage log, so the split between
  steps comes from the code: prompt sizes, how many frames are sent, output limits and caps. The model is in
  `tools/cost_model.py` (run `python3 tools/cost_model.py`), with every assumption written out.
- **Not done:** I didn't call the Anthropic API (the account has no credit), so **none of the new settings have
  been tried against the real API.** That includes Haiku for reading posts and the lower effort levels. There is a
  built-in safety net for that (see change 4). I didn't read Railway's variables. **If Railway already sets
  `MAX_SEARCHES`, `MAX_REPO_SCREENS` or `CLAUDE_MODEL`, those values override the new defaults.** Check them before
  merging.

## Where the money goes: every paid call, per item

| Step | When it runs | Model (before) | Rough size | Est. cost before |
|---|---|---|---|---|
| Read the post (`analyze`) | every video/photo | Sonnet | video: 4–16 frames × 1,372 tokens, plus ~1.8k of text; photo: ~1.9k tokens per slide (up to 12). Output limit 2,000, which the hidden thinking often used up, so ~25% of calls were sent a **second time with all the frames again** | video $0.058, photo $0.03 |
| Find links (`resolve`, web search) | when some resource wasn't found directly (~55% of items) | Sonnet + up to 6 searches | search results are re-read at every internal step: ~30k tokens in, 2.5k out, ~3 searches | $0.063 per item on average ($0.115 when it runs) |
| Safety review (`security`) | each GitHub repo, up to 8 per item, **no memory**, so the same repo was re-checked on retries | Sonnet | up to 90k characters (~10k tokens typical), ~2k out | $0.04 per item on average |
| Skill draft + its safety check | automatic when the post looked skill-worthy (~60%) | Sonnet ×2 | 8k in, ~4.5k out (draft) + 6k in, 2k out (check) | $0.056 per item on average |
| Final message (`breakdown`) | almost every item | Sonnet | 2.5k in, 1.5k out | $0.017 |
| GitHub link: tag the text | each GitHub/article link | Sonnet | output limit was **800**, too small once thinking is counted, so ~2 calls | $0.02 |

**Cost-model check against what you actually paid:**

| | Model estimate | Measured |
|---|---|---|
| Per item, weighted mix (60% reels, 20% photos, 10% GitHub, 10% other) | **$0.21** | $0.15–0.20 |
| Web searches per item | 1.65 | 1.66 (174 ÷ ~105) |
| Token spend for ~105 items | ~$20 | $16.78 |

The model comes out about 10–20% high. That's within its uncertainty, and the biggest unknown is how many tokens
web-search results add. Two things make the measured per-item figure fuzzy:

1. Items sent after the credit ran out (Nov 5, ~02:10 UTC) cost $0, because failed calls aren't billed. They're
   still counted in the ~105, which makes the measured average look lower than the true cost of an item that
   finished.
2. The Nov 4 spike ($8.7) matches the day photo support shipped and old Instagram posts were re-read
   (`/retry photos`). That means some spend was re-processing, not new items. I can't confirm this from the chart.

## What I changed (branch `cost-optimization`)

Expected savings are per item and estimated with the model above. "Before → after" is for a typical reel.

| # | Change | Why | Expected saving |
|---|---|---|---|
| 1 | **Haiku 5.5 reads the post and writes the final message** (`CLAUDE_MODEL_ANALYZE`, `CLAUDE_MODEL_BREAKDOWN`). Sonnet still does security verdicts, skill drafts and link-finding | Haiku is 20× cheaper per token. Reading frames is the single biggest token bill | read-post $0.058 → $0.002; final message $0.017 → $0.001 |
| 2 | **Effort per step** (`EFFORT_*`): `low` for reading, the final message and link-finding; `medium` for security and skills | Cuts hidden thinking, which is billed as output | included in the rows of this table |
| 3 | **Output limits raised** to fit the thinking (reading 2,000 → 4,000; tagging 800 → 2,500) | Calls that ran out of room were re-sent with every frame. You only pay for tokens actually used | fewer re-sends (~25% → ~5%) |
| 4 | **Safety net for the new settings:** if the API rejects a model or effort override, the bot retries once on `CLAUDE_MODEL` without it and keeps doing that until the next restart | So an untested setting can't break the bot | none, it's protection |
| 5 | **Out of credit / bad API key → no retries.** The item is "parked", and you get ONE message: "Anthropic API credits are out — add funds…, then send /retry failed". It isn't repeated for 6 hours (`BILLING_ALERT_HOURS`). Other errors still retry 3 times, as before | Since Nov 5, every item failed 3 times and messaged you each time | stops message spam; failed calls cost $0 anyway |
| 6 | **Safety screens are remembered by repo + commit** (new `screen_cache` table). A repo is reviewed once per version. Too-big results are remembered too | The same repo was being screened up to 3× in one item | ~25%+ of screens (more on retries) |
| 7 | **Repo size checked before downloading.** Repos GitHub reports as >80 MB (`MAX_REPO_SIZE_MB`) are skipped without fetching. Over-40 MB downloads are stopped and remembered | Big repos were downloaded and then failed | bandwidth and time; little $ |
| 8 | **Smaller security-review input:** at most 40k characters instead of 90k. It always includes every scanner finding (HIGH first) and the start of each SKILL.md/README, with long files trimmed first. **The static scanner still reads every file in full, and its "can't be rated green" floor is unchanged** | Most of the 90k was repeated file text | review $0.040 → $0.016 per item (with change 6) |
| 9 | **Fewer, smarter web searches:** limit 6 → 3 (`MAX_SEARCHES`). Well-known products (ChatGPT, Claude, Cursor, Notion…) get their official link for free. Bare generic words ("Skills") aren't searched | Searches cost $0.01 each, plus large result text | link-finding $0.063 → $0.029 |
| 10 | **Junk "repo" names filtered** ("L/min", "ChatGPT/Claude", "iOS/Android", "mg/kg"). Anything written as a github.com link is always kept | Wasted GitHub lookups and triggered pointless follow-up steps | GitHub rate limit; small $ |
| 11 | **The same repo is listed and screened only once per item** | Duplicates came from different name spellings | small |
| 12 | **Repo screens per item 8 → 3** (`MAX_REPO_SCREENS`). Extra repos show "not screened yet: send the link on its own" | Long listicles could trigger 8 Sonnet reviews | worst case ~$0.30 → ~$0.10 |
| 13 | **Skill drafts are on demand** (`AUTO_SKILL_DRAFT=0`). The final message shows "💡 Skill idea: … Send /skill 12 to have it drafted." Set it to `1` to go back to automatic | The priciest step (two Sonnet calls) and only sometimes wanted. It isn't needed for "idea + exact link + safety verdict" | $0.056 → ~$0.008 (if you ask for 1 in 10) |
| 14 | **Daily spend cap** (`DAILY_SPEND_CAP_USD`, default $1.00/day, estimated from each response's token counts). Once reached, only cheap steps run: summary, exact repo links, final message, and the static safety scan without the AI review. No web search, no automatic skill drafts. You're told once per day. `/find 12` and `/skill 12` still run on request | A hard ceiling on bad days like Nov 4 | caps the worst case |
| 15 | **`/cost` command**, plus a spend line in `/status`: estimated spend today / 7 days / 30 days, per step, per item. Every paid call is now logged (`usage_log` table) with the real token counts | So we measure instead of guess from now on | none, it's visibility |

**Result (estimated): about $0.21 → $0.05 per item (−76%). At ~105 items a week, that's ~$22 → ~$5.**

| Item type | Before | After |
|---|---|---|
| Video reel | $0.234 | $0.056 |
| Photo post | $0.208 | $0.055 |
| GitHub link | $0.176 | $0.031 |
| Other link / note | $0.115 | $0.028 |

After the change, the biggest remaining costs are Sonnet link-finding with web search (~$0.03) and Sonnet safety
reviews (~$0.016). Both are what produce "the exact link + a safety verdict", so I left them automatic.

Tests: 36 pass (`python3 -m pytest -q tests`). That's the 20 original tests plus 16 new ones covering each change.

## What I deliberately did NOT change

- **Prompt caching:** not added. Each system prompt is only ~250–700 tokens, which is below the minimum size the API
  will cache. Calls for the same step are usually minutes apart, longer than the 5-minute cache life. A cache
  *write* also costs 25% more than normal input, so it would raise the bill.
- **Batch API (50% off):** not used. Results can take up to 24 hours, which breaks the quick "send it, get a
  breakdown" loop. It could be worth it later for on-demand skill drafts.
- **Haiku for link-finding:** not switched. The docs don't say whether Haiku 5.5 supports web search. Try
  `CLAUDE_MODEL_RESOLVE=claude-haiku-5-5` once you have credit. That would cut link-finding by roughly 90% more.
- **Security verdicts and skill drafts stay on Sonnet.** Those need judgment, and a red verdict still blocks
  install.
- **Frame count and frame size:** unchanged (4–16 frames at 768 px). On Haiku they now cost about $0.0014 per
  reel, so trimming them would save almost nothing and risk missing on-screen repo names.
- **Unchanged behaviour you approved:** one consolidated breakdown message; 3 automatic retries for genuine
  temporary errors; red security verdicts block skill install (the static scanner floor is untouched).

## Remaining risks

1. **Haiku reading on-screen text.** The exact repo name is often only visible in a frame. If Haiku misreads it more
   often than Sonnet, you'll get more "exact link not found". Check on 5–10 past reels with `/retry 12` after
   adding credit. If it's worse, set `CLAUDE_MODEL_ANALYZE=claude-sonnet-5-5` (still with `EFFORT_ANALYZE=low`;
   about $0.03 per reel).
2. **The new settings haven't been tried against the real API.** The safety net (change 4) falls back to Sonnet if
   a setting is rejected. Check the logs for "API rejected model/effort override" after the first item.
3. **The spend figures are estimates** (list prices × reported tokens). Calls made before this change aren't logged.
   The Console bill is the truth. The cap also doesn't stop the cheap Haiku steps, so a day can go slightly over it.
4. **Size pre-check:** GitHub's "size" includes the whole history, so a few repos above 80 MB could have fit under
   the 40 MB download limit. They're now skipped and shown as "too big to screen automatically". You can raise
   `MAX_REPO_SIZE_MB`.
5. **On-demand skill drafts change what you see.** You'll get "💡 Skill idea … /skill 12" instead of a finished
   draft. If you prefer the old way, set `AUTO_SKILL_DRAFT=1` (about +$0.05 per item).
6. The cache keeps a verdict for a specific commit. When a repo changes, it's screened again, which is the intended
   behaviour.

## Recommended settings (Railway → Variables)

Most of these are already the new defaults. Only set them if you want something different, and **remove any old
`MAX_SEARCHES` / `MAX_REPO_SCREENS` / `CLAUDE_MODEL` values that would override them.**

| Variable | Recommended | |
|---|---|---|
| `DAILY_SPEND_CAP_USD` | `1.0` | ~20 items a day at ~$0.05 each |
| `AUTO_SKILL_DRAFT` | `0` | Ask with `/skill 12` |
| `MAX_SEARCHES` | `3` | |
| `MAX_REPO_SCREENS` | `3` | |
| `CLAUDE_MODEL_ANALYZE` / `CLAUDE_MODEL_BREAKDOWN` | `claude-haiku-5-5` | Switch to Sonnet only if link-reading gets worse |
| `GITHUB_TOKEN` | set one (read-only) | Free. Avoids GitHub rate limits, which otherwise turn into "couldn't screen" |
| Anthropic Console → auto-reload or a monthly limit | turn on | So credit doesn't silently run out again |

After you merge and add credit: send `/retry failed` to re-run the parked items, then `/cost` after a day for real
per-item numbers.
