"""Per-item cost model for the Telegram Knowledge Hub, before vs after the cost-optimization branch.

Run: python3 tools/cost_model.py
Every number here is an ESTIMATE built from the code (prompt sizes, frame counts, max_tokens, caps) and list prices.
The only measured figures are the Anthropic Console totals quoted in COST_REPORT.md. Change the assumptions below
and re-run; after deploying, /cost replaces these guesses with real per-call token counts.

Prices (USD per million tokens), https://platform.claude.com/docs/en/about-claude/pricing, read 2026-11-07:
  Sonnet 5.5  in $2.00  out $10.00 ; Haiku 5.5 (<=100k prompt) in $0.10  out $0.50 ; web search $10 per 1,000.
Thinking: Sonnet 5.5 thinks by default (effort "high"); thinking is billed as output and counts toward max_tokens.
Images: ceil(w/28) * ceil(h/28) tokens. A 768x1365 reel frame = 1,372 tokens; a 1080x1350 IG photo = 1,911.
"""
from __future__ import annotations

SONNET = (2.00, 10.00)
HAIKU = (0.10, 0.50)
SEARCH = 0.01


def call(model, inp, out, searches=0):
    return (inp * model[0] + out * model[1]) / 1e6 + searches * SEARCH


# ---------------------------------------------------------------- assumptions (typical item)
FRAME = 1372          # tokens per video frame (768 px wide vertical reel)
PHOTO = 1911          # tokens per IG photo (1080x1350)
VIDEO_FRAMES = 9      # 45 s reel -> round(45/5) = 9 frames (code: 4..16)
PROMPT_OVERHEAD = 1800  # system prompt + tool schema + tool-use preamble + caption/transcript/context text

A = {
    # share of items that trigger each step (before = observed behaviour in code; after = new defaults)
    "p_resolve_before": 0.55,   # items with unresolved resources -> web-search call (fits 174 searches / ~105 items)
    "p_resolve_after": 0.40,    # well-known tools / bare generic words no longer searched
    "searches_before": 3.0,     # searches per resolve call (cap was 6)
    "searches_after": 2.0,      # cap now 3
    "resolve_in_before": 30_000,  # search results re-read on every internal step; biggest uncertainty
    "resolve_in_after": 20_000,
    "resolve_out_before": 2_500,  # report + thinking at effort high
    "resolve_out_after": 1_200,   # effort low
    "repos_per_item": 1.5,      # GitHub repos screened per item with resources (old cap 8, new cap 3)
    "p_repos": 0.5,             # share of items that end up with >=1 GitHub repo
    "rescreen_factor_before": 1.3,  # same repo screened again (job retries, duplicates; seen 3x in one item)
    "cache_hit_after": 0.25,    # share of screens answered from the new cache (repeat repos across items)
    "review_in_before": 10_000,  # ~35k chars of JSON (findings + up to 6x7k chars of SKILL.md), cap 90k chars
    "review_in_after": 7_000,    # capped at 40k chars
    "review_out_before": 2_100,  # verdict + thinking at effort high
    "review_out_after": 1_500,   # effort medium
    "p_skill_before": 0.6,      # automatic skill draft (skill_candidate or imported skill files)
    "p_skill_after": 0.10,      # on demand: assume Sahil asks for 1 in 10
    "skill_in": 8_000, "skill_out_before": 4_500, "skill_out_after": 3_800,
    "skillscreen_in": 6_000, "skillscreen_out_before": 2_000, "skillscreen_out_after": 1_500,
    "p_breakdown": 0.85,
    "breakdown_in": 2_500, "breakdown_out_before": 1_500, "breakdown_out_after": 900,
    "analyze_out_before": 1_800,  # JSON (~800) + thinking; max_tokens was 2000 -> frequent truncation
    "analyze_out_after": 1_100,
    "nudge_rate_before": 0.25,    # share of calls that end without the tool call and get re-sent in full
    "nudge_rate_after": 0.05,     # max_tokens raised, low effort
}


def follow_ups(before: bool, a=A) -> dict[str, float]:
    b = "before" if before else "after"
    res = a[f"p_resolve_{b}"] * call(SONNET, a[f"resolve_in_{b}"], a[f"resolve_out_{b}"], a[f"searches_{b}"])
    n_screens = a["p_repos"] * a["repos_per_item"] * (a["rescreen_factor_before"] if before else (1 - a["cache_hit_after"]))
    sec = n_screens * call(SONNET, a[f"review_in_{b}"], a[f"review_out_{b}"])
    skill = a[f"p_skill_{b}"] * (call(SONNET, a["skill_in"], a[f"skill_out_{b}"])
                                 + call(SONNET, a["skillscreen_in"], a[f"skillscreen_out_{b}"]))
    bd_model = SONNET if before else HAIKU
    bd = a["p_breakdown"] * call(bd_model, a["breakdown_in"], a[f"breakdown_out_{b}"])
    return {"resolve+search": res, "security review": sec, "skill draft+screen": skill, "breakdown": bd}


def analyze(before: bool, image_tokens: int, a=A) -> float:
    model = SONNET if before else HAIKU
    b = "before" if before else "after"
    one = call(model, image_tokens + PROMPT_OVERHEAD, a[f"analyze_out_{b}"])
    return one * (1 + a[f"nudge_rate_{b}"])


def item(kind: str, before: bool) -> dict[str, float]:
    if kind == "video reel":
        out = {"analyze (frames)": analyze(before, VIDEO_FRAMES * FRAME)}
        out.update(follow_ups(before))
    elif kind == "photo post":
        out = {"analyze (1 image)": analyze(before, PHOTO)}
        out.update(follow_ups(before))
    elif kind == "GitHub link":
        b = "before" if before else "after"
        model = SONNET if before else HAIKU
        # analyze_text had max_tokens=800: with high-effort thinking this often truncated -> ~2 calls
        calls = 2.0 if before else 1.05
        out = {"tag/analyze text": calls * call(model, 2_600, 900 if before else 1_000)}
        fresh = 1.3 if before else (1 - A["cache_hit_after"])
        out["security review"] = fresh * call(SONNET, A[f"review_in_{b}"], A[f"review_out_{b}"])
        p_skill = 0.8 if before else A["p_skill_after"]
        out["skill draft+screen"] = p_skill * (call(SONNET, A["skill_in"], A[f"skill_out_{b}"])
                                               + call(SONNET, A["skillscreen_in"], A[f"skillscreen_out_{b}"]))
        out["breakdown"] = call(SONNET if before else HAIKU, A["breakdown_in"], A[f"breakdown_out_{b}"])
    else:  # article / other link, note tagging
        model = SONNET if before else HAIKU
        out = {"tag/analyze text": call(model, 3_500, 1_000) * (1.6 if before else 1.05)}
        f = follow_ups(before)
        out.update({k: v * 0.5 for k, v in f.items()})
    return out


MIX = {"video reel": 0.60, "photo post": 0.20, "GitHub link": 0.10, "other link / note": 0.10}


def main() -> None:
    print(f"{'item type':20s} {'before':>8s} {'after':>8s} {'saving':>7s}")
    avg_b = avg_a = 0.0
    for kind, share in MIX.items():
        b, a = sum(item(kind, True).values()), sum(item(kind, False).values())
        avg_b += share * b
        avg_a += share * a
        print(f"{kind:20s} ${b:7.3f} ${a:7.3f} {100 * (1 - a / b):6.0f}%")
    print(f"{'weighted average':20s} ${avg_b:7.3f} ${avg_a:7.3f} {100 * (1 - avg_a / avg_b):6.0f}%")
    print("\nVideo reel, by step:")
    vb, va = item("video reel", True), item("video reel", False)
    for k in vb:
        print(f"  {k:22s} ${vb[k]:.4f} -> ${va[k]:.4f}")
    print(f"\n~105 items/week -> before ${avg_b * 105:.2f}/wk, after ${avg_a * 105:.2f}/wk")


if __name__ == "__main__":
    main()
