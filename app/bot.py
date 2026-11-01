"""Message routing and the processing pipeline."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from . import ai, media, todoist
from .config import settings
from .db import DB
from .telegram import BOT_API_FILE_LIMIT, Telegram, esc

log = logging.getLogger("hub.bot")

URL_RE = re.compile(r"https?://\S+", re.I)

HELP = """<b>Your knowledge hub</b>

<b>Send me</b>
• an Instagram / TikTok / YouTube link → I download it, pull frames + transcript, and file a summary
• a video, photo or voice note straight from your phone
• any text → saved as a note (add <code>#tags</code> if you like)

<b>Commands</b>
/save &lt;link&gt; [note] — save a link with a note
/note &lt;text&gt; — save a note
/todo &lt;text&gt; — add a Todoist task
/queue &lt;text&gt; — add a task for the overnight agents
/ask &lt;question&gt; — answer from everything saved
/recent — latest items · /get 12 · /delete 12
/status — health check

Voice notes starting with “todo …” or “queue …” go to Todoist.
⚠️ No patient information through this bot."""


class Hub:
    def __init__(self, db: DB, tg: Telegram):
        self.db = db
        self.tg = tg
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._seen_updates: list[int] = []

    # ------------------------------------------------------------------ auth
    def owner_ids(self) -> set[int]:
        ids = set(settings.allowed_user_ids)
        claimed = self.db.kv_get("owner_id")
        if claimed:
            ids.add(int(claimed))
        return ids

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.owner_ids()

    # ------------------------------------------------------------------ entry
    async def handle_update(self, update: dict[str, Any]) -> None:
        uid = update.get("update_id")
        if uid is not None:
            if uid in self._seen_updates:
                return
            self._seen_updates = (self._seen_updates + [uid])[-200:]
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        user = msg.get("from") or {}
        chat_id = msg["chat"]["id"]
        if msg["chat"].get("type") != "private":
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()

        if not self.is_owner(user.get("id", 0)):
            await self._handle_stranger(chat_id, user, text)
            return

        try:
            await self.route(msg, chat_id, text)
        except Exception as e:  # never let one message kill the webhook
            log.exception("route failed")
            await self.tg.send(chat_id, f"⚠️ Something went wrong: <code>{esc(e)}</code>")

    async def _handle_stranger(self, chat_id: int, user: dict, text: str) -> None:
        parts = text.split()
        if (
            parts and parts[0].split("@")[0] == "/start" and len(parts) > 1
            and settings.claim_code and parts[1] == settings.claim_code
            and not self.owner_ids()
        ):
            self.db.kv_set("owner_id", str(user["id"]))
            log.info("bot claimed by %s", user.get("id"))
            await self.tg.send(chat_id, "✅ This bot is now yours. Only you can use it.\n\n" + HELP)
            return
        log.info("ignored message from non-owner %s", user.get("id"))
        if not self.owner_ids():
            await self.tg.send(chat_id, "This is a private bot. To claim it, open the setup link you were given.")

    async def route(self, msg: dict, chat_id: int, text: str) -> None:
        mid = msg["message_id"]
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower() if cmd.startswith("/") else ""
        arg = arg.strip()

        if cmd in ("/start", "/help"):
            return await self.tg.send(chat_id, HELP)
        if cmd == "/status":
            return await self.tg.send(chat_id, self.status_text())
        if cmd == "/recent":
            return await self.cmd_recent(chat_id, arg)
        if cmd == "/get":
            return await self.cmd_get(chat_id, arg)
        if cmd == "/delete":
            return await self.cmd_delete(chat_id, arg)
        if cmd == "/ask":
            return await self.cmd_ask(chat_id, arg, mid)
        if cmd == "/todo":
            return await self.cmd_todo(chat_id, arg, mid, queue=False)
        if cmd == "/queue":
            return await self.cmd_todo(chat_id, arg, mid, queue=True)
        if cmd == "/note":
            return await self.save_note(chat_id, arg, mid)

        # Media sent directly
        video = msg.get("video") or msg.get("video_note") or msg.get("animation")
        doc = msg.get("document") or {}
        if not video and str(doc.get("mime_type", "")).startswith("video/"):
            video = doc
        if video:
            return await self.enqueue_file(chat_id, mid, video, "video", text)
        photo = msg.get("photo")
        if not photo and str(doc.get("mime_type", "")).startswith("image/"):
            photo = [doc]
        if photo:
            return await self.enqueue_file(chat_id, mid, photo[-1], "image", text)
        voice = msg.get("voice") or msg.get("audio")
        if voice:
            return await self.enqueue_file(chat_id, mid, voice, "voice", text)

        # Links
        if cmd == "/save" or URL_RE.search(text):
            m = URL_RE.search(arg if cmd == "/save" else text)
            if not m:
                return await self.tg.send(chat_id, "Send /save followed by a link.")
            url = m.group(0).rstrip(").,>]")
            note = (arg if cmd == "/save" else text).replace(m.group(0), "").strip()
            return await self.enqueue_url(chat_id, mid, url, note)

        if cmd:
            return await self.tg.send(chat_id, f"Unknown command {esc(cmd)}. Try /help.")

        if not text:
            return await self.tg.send(chat_id, "I can't use that type of message yet. Try /help.")
        if settings.default_text_action == "todo":
            return await self.cmd_todo(chat_id, text, mid, queue=False)
        return await self.save_note(chat_id, text, mid)

    # ------------------------------------------------------------------ commands
    def status_text(self) -> str:
        s = self.db.stats()
        total = sum(s.values())
        def ok(b: bool) -> str:
            return "✅" if b else "—"
        return (
            "<b>Status</b>\n"
            f"Items saved: {total} " + (f"({', '.join(f'{k}: {v}' for k, v in s.items())})" if s else "") + "\n"
            f"Queue: {self.queue.qsize()} waiting\n"
            f"{ok(bool(settings.anthropic_key))} Claude analysis ({esc(settings.claude_model)})\n"
            f"{ok(settings.transcriber != 'none')} Transcription: {esc(settings.transcriber)}\n"
            f"{ok(bool(settings.todoist_token))} Todoist\n"
            f"{ok(bool(settings.ig_cookies))} Instagram cookies\n"
            f"{ok(bool(settings.mcp_secret))} MCP connector"
        )

    async def cmd_recent(self, chat_id: int, arg: str) -> None:
        n = int(arg) if arg.isdigit() else 10
        items = self.db.recent(min(n, 30))
        if not items:
            return await self.tg.send(chat_id, "Nothing saved yet.")
        lines = [self._line(it) for it in items]
        await self.tg.send(chat_id, "<b>Recent</b>\n" + "\n".join(lines))

    @staticmethod
    def _line(it: dict) -> str:
        icon = {"video": "🎬", "image": "🖼", "link": "🔗", "note": "📝", "voice": "🎙"}.get(it["kind"], "•")
        status = "" if it["status"] == "done" else f" <i>({esc(it['status'])})</i>"
        title = it.get("title") or (it.get("user_note") or it.get("summary") or "")[:60] or "(untitled)"
        return f"{icon} <b>#{it['id']}</b> {esc(title)}{status}"

    async def cmd_get(self, chat_id: int, arg: str) -> None:
        if not arg.lstrip("#").isdigit():
            return await self.tg.send(chat_id, "Usage: /get 12")
        it = self.db.get(int(arg.lstrip("#")))
        if not it:
            return await self.tg.send(chat_id, "No item with that number.")
        await self.tg.send(chat_id, self.render(it, full=True))

    async def cmd_delete(self, chat_id: int, arg: str) -> None:
        if not arg.lstrip("#").isdigit():
            return await self.tg.send(chat_id, "Usage: /delete 12")
        item_id = int(arg.lstrip("#"))
        it = self.db.get(item_id)
        if it and it.get("media_dir"):
            shutil.rmtree(it["media_dir"], ignore_errors=True)
        ok = self.db.delete(item_id)
        await self.tg.send(chat_id, f"🗑 Deleted #{item_id}." if ok else "No item with that number.")

    async def cmd_ask(self, chat_id: int, question: str, mid: int) -> None:
        if not question:
            return await self.tg.send(chat_id, "Usage: /ask what did I save about Brugada?")
        await self.tg.typing(chat_id)
        hits = self.db.search(question, limit=8)
        if not hits:
            return await self.tg.send(chat_id, "Nothing in the archive matches that yet.", reply_to=mid)
        if not settings.anthropic_key:
            return await self.tg.send(chat_id, "<b>Matches</b>\n" + "\n".join(self._line(h) for h in hits), reply_to=mid)
        reply = await ai.answer(question, hits)
        await self.tg.send(chat_id, esc(reply), reply_to=mid)

    async def cmd_todo(self, chat_id: int, text: str, mid: int, queue: bool) -> None:
        if not text:
            return await self.tg.send(chat_id, f"Usage: /{'queue' if queue else 'todo'} what needs doing")
        labels = [settings.todoist_capture_label] if settings.todoist_capture_label else []
        if queue:
            labels.append(settings.todoist_queue_label)
        try:
            task = await todoist.add_task(text, labels=labels, description="Captured via Telegram")
        except todoist.TodoistError as e:
            return await self.tg.send(chat_id, f"⚠️ {esc(e)}", reply_to=mid)
        where = "the overnight queue" if queue else "Todoist"
        link = task.get("url") or ""
        await self.tg.send(
            chat_id, f"✅ Added to {where}: {esc(text)}" + (f"\n<a href=\"{esc(link)}\">Open</a>" if link else ""),
            reply_to=mid,
        )

    async def save_note(self, chat_id: int, text: str, mid: int, kind: str = "note", **extra: Any) -> int:
        if not text:
            await self.tg.send(chat_id, "Usage: /note something worth keeping")
            return 0
        tags = [t.lower() for t in re.findall(r"#([\w\-]+)", text)]
        item_id = self.db.create(kind, user_note=text, tags=tags, title=text[:80], status="done", **extra)
        await self.tg.send(chat_id, f"📝 Saved as #{item_id}.", reply_to=mid)
        if settings.anthropic_key and len(text) > 140:
            self.queue.put_nowait(("tag_note", {"item_id": item_id}))
        return item_id

    # ------------------------------------------------------------------ queueing
    async def enqueue_url(self, chat_id: int, mid: int, url: str, note: str) -> None:
        item_id = self.db.create("link", status="processing", source_url=url, user_note=note)
        await self.tg.react(chat_id, mid)
        await self.tg.send(chat_id, f"⏳ Got it (#{item_id}). Downloading and analysing…", reply_to=mid)
        self.queue.put_nowait(("url", {"item_id": item_id, "chat_id": chat_id, "mid": mid, "url": url, "note": note}))

    async def enqueue_file(self, chat_id: int, mid: int, f: dict, kind: str, note: str) -> None:
        size = int(f.get("file_size") or 0)
        if size > BOT_API_FILE_LIMIT:
            await self.tg.send(
                chat_id,
                f"That file is {size / 1e6:.0f} MB; Telegram only lets bots fetch files up to 20 MB. "
                "Send a link instead, or a shorter/compressed version.",
                reply_to=mid,
            )
            return
        item_id = self.db.create(kind, status="processing", user_note=note, meta={"telegram_file_id": f["file_id"]})
        await self.tg.react(chat_id, mid)
        if kind != "voice":
            await self.tg.send(chat_id, f"⏳ Got it (#{item_id}). Analysing…", reply_to=mid)
        self.queue.put_nowait(
            ("file", {"item_id": item_id, "chat_id": chat_id, "mid": mid, "file_id": f["file_id"], "kind": kind, "note": note})
        )

    async def worker(self) -> None:
        while True:
            job, p = await self.queue.get()
            try:
                if job == "url":
                    await self.process_url(**p)
                elif job == "file":
                    await self.process_file(**p)
                elif job == "tag_note":
                    await self.process_note_tags(**p)
            except Exception as e:
                log.exception("job failed")
                if "item_id" in p:
                    self.db.update(p["item_id"], status="error", error=str(e)[:1000])
                if "chat_id" in p:
                    await self.tg.send(
                        p["chat_id"], f"⚠️ #{p.get('item_id')} failed: <code>{esc(str(e)[:600])}</code>",
                        reply_to=p.get("mid"),
                    )
            finally:
                self.queue.task_done()

    # ------------------------------------------------------------------ pipeline
    def _workdir(self, item_id: int) -> Path:
        d = settings.data_dir / "tmp" / str(item_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _mediadir(self, item_id: int) -> Path:
        d = settings.data_dir / "media" / str(item_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def process_url(self, item_id: int, chat_id: int, mid: int, url: str, note: str) -> None:
        work = self._workdir(item_id)
        try:
            try:
                info = await media.download_url(url, work)
            except media.MediaError as e:
                # Not downloadable: keep the link, tag it from whatever text we have.
                fields: dict[str, Any] = {"status": "done", "error": f"Download failed: {e}", "title": url[:80]}
                if settings.anthropic_key and note:
                    try:
                        a = await ai.analyze_text(note, url)
                        fields.update(self._analysis_fields(a))
                    except Exception:
                        pass
                self.db.update(item_id, **fields)
                await self.tg.send(
                    chat_id,
                    f"🔗 Saved the link as #{item_id}, but couldn't download the media:\n<code>{esc(str(e)[:400])}</code>",
                    reply_to=mid,
                )
                return
            await self._process_media(
                item_id, chat_id, mid, Path(info["path"]), note,
                url=info["webpage_url"], title=info["title"], caption=info["caption"], author=info["author"],
                meta={k: info[k] for k in ("extractor", "upload_date") if info.get(k)},
            )
        finally:
            media.cleanup(work)

    async def process_file(self, item_id: int, chat_id: int, mid: int, file_id: str, kind: str, note: str) -> None:
        work = self._workdir(item_id)
        try:
            path = await self.tg.download_file(file_id, work / "source.bin")
            if kind == "voice":
                await self._process_voice(item_id, chat_id, mid, path, note)
            else:
                await self._process_media(item_id, chat_id, mid, path, note, url="", title="", caption="", author="",
                                          meta={"telegram_file_id": file_id}, force_kind=kind)
        finally:
            media.cleanup(work)

    async def _process_voice(self, item_id: int, chat_id: int, mid: int, path: Path, note: str) -> None:
        audio = await media.extract_audio(path, path.with_name("audio.mp3")) or path
        text, backend = await ai.transcribe(audio)
        if not text:
            self.db.update(item_id, status="error", error=f"No transcript ({backend})")
            await self.tg.send(chat_id, "⚠️ I couldn't transcribe that voice note.", reply_to=mid)
            return
        low = text.lower().lstrip()
        for prefix, queue in (("to do", False), ("todo", False), ("to-do", False), ("queue", True)):
            if low.startswith(prefix):
                task = text[len(prefix):].lstrip(" ,.:;-")
                self.db.delete(item_id)
                await self.cmd_todo(chat_id, task, mid, queue=queue)
                return
        tags = [t.lower() for t in re.findall(r"#([\w\-]+)", note)]
        self.db.update(item_id, status="done", transcript=text, user_note=(note + "\n" + text).strip(),
                       title=text[:80], tags=tags)
        await self.tg.send(chat_id, f"🎙 Saved as #{item_id}:\n<i>{esc(text[:1500])}</i>", reply_to=mid)
        if settings.anthropic_key and len(text) > 140:
            self.queue.put_nowait(("tag_note", {"item_id": item_id}))

    async def _process_media(
        self, item_id: int, chat_id: int, mid: int, path: Path, note: str, *, url: str, title: str,
        caption: str, author: str, meta: dict, force_kind: str | None = None,
    ) -> None:
        work = path.parent
        mdir = self._mediadir(item_id)
        kind = force_kind or "video"
        if kind == "image":
            frames = [await media.image_to_jpeg(path, mdir / "frame_00.jpg")]
            duration, transcript, backend = None, "", "n/a"
        else:
            pr = await media.probe(path)
            duration = pr["duration"]
            if not pr["has_video"]:
                self.db.update(item_id, kind="voice")
                return await self._process_voice(item_id, chat_id, mid, path, note)
            frames = await media.extract_frames(path, mdir, duration)
            transcript, backend = "", "none"
            if pr["has_audio"]:
                audio = await media.extract_audio(path, work / "audio.mp3")
                if audio:
                    transcript, backend = await ai.transcribe(audio)
            if settings.keep_video:
                shutil.copy(path, mdir / f"video{path.suffix}")

        fields: dict[str, Any] = {
            "kind": kind, "source_url": url or None, "title": title[:200] or None, "caption": caption or None,
            "author": author or None, "duration_s": duration, "transcript": transcript or None,
            "media_dir": str(mdir), "meta": {**meta, "transcriber": backend, "frames": len(frames)},
        }
        if settings.anthropic_key:
            a = await ai.analyze(kind=kind, frames=frames, transcript=transcript, caption=caption, title=title,
                                 author=author, user_note=note, url=url, duration=duration)
            fields.update(self._analysis_fields(a))
            fields["meta"]["accuracy_flags"] = a.get("accuracy_flags", [])
            fields["meta"]["phi_flag"] = bool(a.get("phi_flag"))
        else:
            fields["summary"] = "(Not analysed: ANTHROPIC_API_KEY not set.)"
        fields["status"] = "done"
        self.db.update(item_id, **fields)
        it = self.db.get(item_id)
        await self.tg.send(chat_id, self.render(it), reply_to=mid)

    @staticmethod
    def _analysis_fields(a: dict) -> dict[str, Any]:
        out = {
            "summary": a.get("summary"), "key_points": a.get("key_points") or [],
            "techniques": a.get("techniques") or [], "tags": a.get("tags") or [],
            "category": a.get("category"), "skill_candidate": bool(a.get("skill_candidate")),
            "skill_idea": a.get("skill_idea") or None,
        }
        if a.get("title"):
            out["title"] = str(a["title"])[:200]
        return out

    async def process_note_tags(self, item_id: int) -> None:
        it = self.db.get(item_id)
        if not it:
            return
        a = await ai.analyze_text(it.get("user_note") or "", it.get("source_url") or "")
        fields = self._analysis_fields(a)
        fields["tags"] = sorted(set((it.get("tags") or []) + (fields["tags"] or [])))
        self.db.update(item_id, **fields)

    # ------------------------------------------------------------------ rendering
    def render(self, it: dict, full: bool = False) -> str:
        icon = {"video": "🎬", "image": "🖼", "link": "🔗", "note": "📝", "voice": "🎙"}.get(it["kind"], "•")
        parts = [f"{icon} <b>#{it['id']} {esc(it.get('title') or '(untitled)')}</b>"]
        if it.get("author") or it.get("source_url"):
            src = esc(it.get("author") or "")
            if it.get("source_url"):
                src += f" · <a href=\"{esc(it['source_url'])}\">source</a>"
            parts.append(src.strip(" ·"))
        meta = it.get("meta") or {}
        if meta.get("phi_flag"):
            parts.append("🚨 <b>Possible patient information detected.</b> Consider /delete " + str(it["id"]))
        if it.get("summary"):
            parts.append(esc(it["summary"]))
        if it.get("key_points"):
            kp = it["key_points"] if full else it["key_points"][:5]
            parts.append("<b>Key points</b>\n" + "\n".join(f"• {esc(k)}" for k in kp))
        if it.get("techniques"):
            parts.append("<b>Techniques worth stealing</b>\n" + "\n".join(f"• {esc(t)}" for t in it["techniques"]))
        if meta.get("accuracy_flags"):
            parts.append("<b>⚠️ Accuracy flags</b>\n" + "\n".join(f"• {esc(t)}" for t in meta["accuracy_flags"]))
        if it.get("skill_candidate") and it.get("skill_idea"):
            parts.append(f"💡 <b>Skill idea:</b> {esc(it['skill_idea'])}")
        tail = []
        if it.get("category"):
            tail.append(esc(it["category"]))
        if it.get("tags"):
            tail.append(" ".join(f"#{esc(t)}" for t in it["tags"]))
        if tail:
            parts.append("<i>" + " · ".join(tail) + "</i>")
        if not it.get("transcript") and it["kind"] == "video":
            parts.append("<i>(no transcript)</i>")
        if full:
            if it.get("user_note"):
                parts.append(f"<b>Your note</b>\n{esc(it['user_note'])}")
            if it.get("transcript"):
                parts.append(f"<b>Transcript</b>\n<i>{esc(it['transcript'][:2500])}</i>")
            if it.get("error"):
                parts.append(f"<b>Error</b> <code>{esc(it['error'])}</code>")
            parts.append(f"<i>Saved {esc(it.get('created_at', '')[:16].replace('T', ' '))} UTC</i>")
        return "\n\n".join(p for p in parts if p)
