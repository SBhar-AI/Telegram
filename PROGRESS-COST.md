# PROGRESS-COST — cost-optimization branch

Branch: `cost-optimization` (never merge to main / deploy without Sahil). Tests: `python3 -m pytest -q tests`.

## Done
- Read code; mapped every paid call (ai.py: analyze, analyze_text, resolve_resources [web search], draft_skill,
  compose_breakdown, answer; security.py: review via ai.claude).
- Pricing pulled 2026-11-07 from https://platform.claude.com/docs/en/about-claude/pricing (Sonnet 5.5 $2/$10,
  Haiku 5.5 $0.10/$0.50 up to 100k prompt, web search $10/1k). Thinking pages: Sonnet 5.5 thinks by default at
  effort "high"; thinking billed as output and counts toward max_tokens.

## In progress
- (see Next)

## Next (planned order)
1. app/costs.py: price table, usage logging (DB table usage_log), spend today/week, daily cap.
2. ai.py: per-stage model + effort config, AccountError (credit/auth) no-retry, record usage, safe fallback on
   400 from an override, resolve_resources via _post.
3. bot.py: park items on AccountError + one alert per N hours; /cost; cap -> cheap mode; AUTO_SKILL_DRAFT.
4. security.py: screen cache by repo@sha (DB), size pre-check, smaller reviewer input, re-raise AccountError.
5. resources.py: junk slug filter; skip web search for well-known tools.
6. Tests for each; COST_REPORT.md; tools/cost_model.py.
