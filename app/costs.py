"""Spend tracking: list prices, per-call cost from the API's `usage` fields, the daily cap.

Prices: USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing (read 2026-11-07).
These are estimates: we apply list prices to the token counts the API reports on every response. The Anthropic
Console is the source of truth for the bill.
"""
from __future__ import annotations

import contextvars
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import settings

log = logging.getLogger("hub.costs")

PRICES_AS_OF = "2026-11-07"
PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
# model prefix -> (input, output, 5-minute cache write, cache read), USD per million tokens
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),      # prompts up to 100k tokens
    "claude-haiku-5-5>100k": (0.50, 2.50, 0.625, 0.05),  # prompts over 100k tokens
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
}
WEB_SEARCH_USD = 10.0 / 1000  # per search

current_item: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_item", default=None)
_db: Any = None


def bind(db: Any) -> None:
    global _db
    _db = db


def db() -> Any:
    return _db


def _price(model: str, prompt_tokens: int) -> tuple[float, float, float, float]:
    m = (model or "").lower()
    if m.startswith("claude-haiku-5-5") and prompt_tokens > 100_000:
        return PRICES["claude-haiku-5-5>100k"]
    for key in sorted(PRICES, key=len, reverse=True):
        if not key.endswith(">100k") and m.startswith(key):
            return PRICES[key]
    return PRICES["claude-sonnet-5-5"]  # unknown model: assume the pricier common one


def cost_of(model: str, usage: dict[str, Any]) -> tuple[float, dict[str, int]]:
    """Cost of one API response from its `usage` block. Returns (usd, normalised counts)."""
    u = usage or {}
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cw = int(u.get("cache_creation_input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    searches = int(((u.get("server_tool_use") or {}).get("web_search_requests")) or 0)
    p_in, p_out, p_cw, p_cr = _price(model, inp + cw + cr)
    usd = (inp * p_in + out * p_out + cw * p_cw + cr * p_cr) / 1e6 + searches * WEB_SEARCH_USD
    return usd, {"input_tokens": inp, "output_tokens": out, "cache_write_tokens": cw,
                 "cache_read_tokens": cr, "web_searches": searches}


def record(stage: str, model: str, usage: dict[str, Any]) -> float:
    usd, counts = cost_of(model, usage)
    if _db is not None:
        try:
            _db.log_usage(item_id=current_item.get(), stage=stage, model=model, cost_usd=round(usd, 6), **counts)
        except Exception:  # accounting must never break the pipeline
            log.exception("usage log failed")
    return usd


def _day_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def spent_today() -> float:
    if _db is None:
        return 0.0
    return _db.spend_since(_day_start().isoformat())["cost"]


def over_cap() -> bool:
    cap = settings.daily_spend_cap_usd
    return bool(cap and cap > 0 and spent_today() >= cap)


def summary() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    out = {}
    for label, since in (("today", _day_start(now)), ("7 days", now - timedelta(days=7)), ("30 days", now - timedelta(days=30))):
        out[label] = _db.spend_since(since.replace(microsecond=0).isoformat()) if _db is not None else \
            {"cost": 0.0, "calls": 0, "items": 0, "searches": 0, "by_stage": {}}
    return out
