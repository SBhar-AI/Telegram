"""Minimal Telegram Bot API client (no SDK dependency)."""
from __future__ import annotations

import html
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("hub.telegram")

BOT_API_FILE_LIMIT = 20 * 1024 * 1024  # standard Bot API getFile limit
MAX_MESSAGE = 4000


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


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
        r = await self.client.post(f"{self.base}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            log.warning("telegram %s failed: %s", method, data)
            return None
        return data.get("result")

    async def send(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        """Send HTML-formatted text, splitting long messages."""
        chunks = [text[i : i + MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)] or [""]
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
            if res is None:  # HTML parse failure fallback: send as plain text
                params.pop("parse_mode", None)
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
