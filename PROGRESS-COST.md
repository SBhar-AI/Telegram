# PROGRESS-COST — cost-optimization branch

Branch: `cost-optimization` (never merge to main / deploy without Sahil). Tests: `python3 -m pytest -q tests` (36 pass).

## Done
- Mapped every paid call; pricing from https://platform.claude.com/docs/en/about-claude/pricing (2026-11-07);
  Sonnet 5.5 thinks by default at effort "high", billed as output (thinking/effort docs).
- app/costs.py (prices, usage logging -> usage_log table, spend today/7d/30d, daily cap). /cost + spend in /status.
- ai.py: per-stage model/effort (config.model_for/effort_for), AccountError (credit/401/403) never retried,
  one-shot fallback to CLAUDE_MODEL if an override is rejected, usage recorded on every call, resolve via _post.
- bot.py: account errors park the item + one alert per BILLING_ALERT_HOURS; cheap mode at cap (no web search, no
  AI review, no auto skill drafts, one notice/day); AUTO_SKILL_DRAFT=0 default (skill idea + /skill N in breakdown);
  well-known tools / generic names not web-searched; per-item repo dedupe; /retry covers breakdown.
- security.py: repo_head (meta+SHA) before download; screen_cache by repo@sha (also size rejections); skip
  >MAX_REPO_SIZE_MB; reviewer input <=40k chars; AccountError re-raised (no fake verdicts cached).
- resources.py: junk slug filter (units, brand pairs, single-letter owners; github.com URLs always kept).
- tools/cost_model.py (before $0.21 -> after $0.05 per item, estimated); COST_REPORT.md; README variables.

## In progress
- Nothing.

## Next (needs Sahil / live API)
- Review + merge to main (deploys). Check Railway vars don't override new defaults (MAX_SEARCHES, MAX_REPO_SCREENS).
- After credit: /retry failed; compare Haiku link-reading on 5-10 reels; /cost after a day; optionally try
  CLAUDE_MODEL_RESOLVE=claude-haiku-5-5.
