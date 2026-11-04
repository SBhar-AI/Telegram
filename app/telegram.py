"""Minimal Telegram Bot API client (no SDK dependency)."""
from __future__ import annotations

import asyncio
import html
import logging
import re
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("hub.telegram")

BOT_API_FILE_LIMIT = 20 * 1024 * 1024  # standard Bot API getFile limit
MAX_MESSAGE = 4000


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


_TAG = re.compile(r"<(/?)([a-zA-Z0-9-]+)([^>]*)>")


def _balance(chunk: str, carry: list[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """Re-open tags left open by the previous chunk and close any still open at the end, so each chunk is valid
    Telegram HTML on its own. Splitting mid-tag caused the 2026-11-04 "can't parse entities" failures."""
    head = "".join(f"<{n}{a}>" for n, a in carry)
    stack = list(carry)
    for m in _TAG.finditer(chunk):
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if closing:
            for j in range(len(stack) - 1, -1, -1):
                if stack[j][0] == name:
                    del stack[j:]
                    break
        elif not attrs.rstrip().endswith("/"):
            stack.append((name, attrs))
    tail = "".join(f"</{n}>" for n, _ in reversed(stack))
    return head + chunk + tail, stack


def split_html(text: str, limit: int = MAX_MESSAGE - 300) -> list[str]:
    """Split on line breaks (never inside a tag or entity), then balance tags per chunk."""
    if len(text) <= MAX_MESSAGE:
        return [text]
    raw: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > limit:  # a single very long line: cut outside tags/entities
            cut = limit
            while cut > 0 and (line.rfind("<", 0, cut) > line.rfind(">", 0, cut) or line.rfind("&", 0, cut) > line.rfind(";", 0, cut)):
                cut -= 1
            cut = cut or limit
            if buf:
                raw.append(buf); buf = ""
            raw.append(line[:cut]); line = line[cut:]
        if len(buf) + len(line) + 1 > limit and buf:
            raw.append(buf); buf = ""
        buf = f"{buf}\n{line}" if buf else line
    if buf:
        raw.append(buf)
    out, carry = [], []
    for r in raw:
        chunk, carry = _balance(r, carry)
        out.append(chunk)
    return out


def to_plain(text: str) -> str:
    text = re.sub(r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', r"\2 (\1)", text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class Telegram:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    async def call(self, method: str, **params: Any) -> Any:
        if not self.enabled:
            log.info("telegram disabled; would call %s %s", method, params)
            return None
        # Network hiccups to api.telegram.org must never crash a handler (a ConnectError in setMessageReaction
        # on 2026-11-04 aborted routing, so item #62 was created but never queued). Retry, then give up quietly.
        for attempt in range(3):
            try:
                r = await self.client.post(f"{self.base}/{method}", json=params)
                data = r.json()
            except (httpx.TransportError, ValueError) as e:
                if attempt < 2:
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                log.warning("telegram %s gave up after network errors: %s", method, type(e).__name__)
                return None
            if not data.get("ok"):
                if data.get("error_code") == 429 and attempt < 2:
                    await asyncio.sleep(int((data.get("parameters") or {}).get("retry_after", 3)))
                    continue
                log.warning("telegram %s failed: %s", method, data)
                return None
            return data.get("result")
        return None

    async def send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        """Send HTML-formatted text, splitting long messages."""
        chunks = split_html(text)
        for i, chunk in enumerate(chunks):
            params: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if reply_to and i == 0:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            res = await self.call("sendMessage", **params)
            if res is None:  # HTML parse failure fallback: send readable plain text, not raw tags
                params.pop("parse_mode", None)
                params["text"] = to_plain(chunk)[:MAX_MESSAGE]
                await self.call("sendMessage", **params)

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", chat_id=chat_id, action="typing")

    async def react(self, chat_id: int, message_id: int, emoji: str = "👀") -> None:
        await self.call(
            "setMessageReaction", chat_id=chat_id, message_id=message_id,
            reaction=[{"type": "emoji", "emoji": emoji}],
        )

    async def download_file(self, file_id: str, dest: Path) -> Path:
        info = await self.call("getFile", file_id=file_id)
        if not info or "file_path" not in info:
            raise RuntimeError("Telegram would not hand over that file (files over 20 MB can't be fetched by bots).")
        url = f"{self.file_base}/{info['file_path']}"
        suffix = Path(info["file_path"]).suffix or dest.suffix
        dest = dest.with_suffix(suffix)
        async with self.client.stream("GET", url) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
        return dest

    async def set_webhook(self, url: str, secret: str) -> Any:
        return await self.call(
            "setWebhook", url=url, secret_token=secret,
            allowed_updates=["message", "edited_message"], drop_pending_updates=False,
        )

    async def set_commands(self) -> None:
        cmds = [
            ("help", "What I can do"),
            ("save", "Save a link (reel, TikTok, YouTube, article) + optional note"),
            ("note", "Save a text note to the archive"),
            ("todo", "Add a task to Todoist"),
            ("queue", "Add a task for the overnight agents"),
            ("ask", "Ask a question about everything saved"),
            ("recent", "Show the latest saved items"),
            ("get", "Show one item in full: /get 12"),
            ("delete", "Delete an item: /delete 12"),
            ("status", "Service health and config"),
        ]
        await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in cmds])
