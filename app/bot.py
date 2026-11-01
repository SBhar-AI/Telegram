"""Message routing and the processing pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from . import ai, media, resources, security, todoist
from .config import settings
from .db import DB
from .telegram import BOT_API_FILE_LIMIT, Telegram, esc

log = logging.getLogger("hub.bot")

URL_RE = re.compile(r"https?://\S+", re.I)

HELP = """<b>Your knowledge hub</b>

<b>Send me</b>
• an Instagram / TikTok / YouTube link → I download it, pull frames + transcript, file a summary, then hunt down every repo, skill or resource it mentions
• a GitHub or article link → I read it, list any Claude skills inside, and draft one for you
• a video, photo or voice note straight from your phone
• any text → saved as a note (add <code>#tags</code> if you like)

<b>Commands</b>
/save &lt;link&gt; [note] — save a link with a note
/note &lt;text&gt; — save a note
/todo &lt;text&gt; — add a Todoist task
/queue &lt;text&gt; — add a task for the overnight agents
/ask &lt;question&gt; — answer from everything saved
/resources — links, repos and skills found in your saves
/skills — skill drafts waiting for review
/find 12 — re-run resource search for an item · /skill 12 — draft a skill from it
/security 12 — full security report (why + how to make it safer)
Failed jobs retry automatically (3 tries). /failed — items that still failed · /retry 12 or /retry failed — try again
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
        if cmd == "/resources":
            return await self.cmd_resources(chat_id, arg)
        if cmd == "/failed":
            return await self.cmd_failed(chat_id)
        if cmd == "/retry":
            return await self.cmd_retry(chat_id, arg, mid)
        if cmd == "/security":
            return await self.cmd_security(chat_id, arg)
        if cmd == "/skills":
            return await self.cmd_skills(chat_id)
        if cmd in ("/find", "/skill"):
            if not arg.lstrip("#").isdigit() or not self.db.get(int(arg.lstrip("#"))):
                return await self.tg.send(chat_id, f"Usage: {cmd} 12 (an item number)")
            job = "resources" if cmd == "/find" else "skill"
            self.enqueue((job, {"item_id": int(arg.lstrip("#")), "chat_id": chat_id, "mid": mid, "force": True}))
            return await self.tg.send(chat_id, "⏳ On it.", reply_to=mid)
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
            self.enqueue(("tag_note", {"item_id": item_id}))
        return item_id

    # ------------------------------------------------------------------ queueing
    async def enqueue_url(self, chat_id: int, mid: int, url: str, note: str) -> None:
        item_id = self.db.create("link", status="processing", source_url=url, user_note=note)
        await self.tg.react(chat_id, mid)
        await self.tg.send(chat_id, f"⏳ Got it (#{item_id}). Downloading and analysing…", reply_to=mid)
        self.enqueue(("url", {"item_id": item_id, "chat_id": chat_id, "mid": mid, "url": url, "note": note}))

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
        self.enqueue(
            ("file", {"item_id": item_id, "chat_id": chat_id, "mid": mid, "file_id": f["file_id"], "kind": kind, "note": note})
        )

    # ------------------------------------------------------------------ jobs, retries, resume
    ITEM_JOBS = ("url", "file")  # jobs that own the item's status

    def enqueue(self, jp: tuple[str, dict[str, Any]]) -> None:
        """Queue a job and remember it on the item so it survives restarts and can be retried."""
        job, p = jp
        if "item_id" in p:
            it = self.db.get(p["item_id"])
            if it:
                meta = it.get("meta") or {}
                pending = meta.get("pending_jobs") or {}
                pending[job] = {k: v for k, v in p.items() if k != "force"} | ({"force": True} if p.get("force") else {})
                meta["pending_jobs"] = pending
                self.db.update(p["item_id"], meta=meta)
        self.queue.put_nowait((job, p))

    def _meta(self, item_id: int) -> dict[str, Any]:
        it = self.db.get(item_id)
        return dict((it or {}).get("meta") or {})

    def _job_done(self, job: str, item_id: int | None) -> None:
        if item_id is None:
            return
        it = self.db.get(item_id)
        if not it:
            return
        meta = it.get("meta") or {}
        (meta.get("pending_jobs") or {}).pop(job, None)
        (meta.get("attempts") or {}).pop(job, None)
        self.db.update(item_id, meta=meta)

    async def _later(self, delay: float, job: str, p: dict) -> None:
        await asyncio.sleep(delay)
        self.queue.put_nowait((job, p))

    async def _handle_failure(self, job: str, p: dict, e: Exception) -> None:
        item_id = p.get("item_id")
        err = f"{type(e).__name__}: {str(e)[:500]}"
        it = self.db.get(item_id) if item_id is not None else None
        if not it:
            return
        meta = it.get("meta") or {}
        attempts = meta.setdefault("attempts", {})
        n = int(attempts.get(job, 0)) + 1
        attempts[job] = n
        meta.setdefault("errors", []).append({"job": job, "attempt": n, "error": err})
        meta["errors"] = meta["errors"][-9:]
        if n < settings.max_attempts:
            delay = settings.retry_delays[min(n - 1, len(settings.retry_delays) - 1)]
            log.warning("job %s #%s failed (attempt %d/%d); retrying in %ss: %s", job, item_id, n, settings.max_attempts, delay, err)
            if job in self.ITEM_JOBS:
                self.db.update(item_id, status="processing", error=f"retrying ({n}/{settings.max_attempts}): {err}", meta=meta)
            else:
                self.db.update(item_id, meta=meta)
            asyncio.create_task(self._later(delay, job, p))
            return
        # Out of attempts: stop, flag for Sahil, report every error once.
        (meta.get("pending_jobs") or {}).pop(job, None)
        meta["needs_attention"] = True
        fields: dict[str, Any] = {"meta": meta}
        if job in self.ITEM_JOBS:
            fields.update(status="error", error=err)
        self.db.update(item_id, **fields)
        label = {"url": "processing the link", "file": "processing the file", "resources": "finding resources",
                 "skill": "drafting a skill", "tag_note": "tagging the note"}.get(job, job)
        errs = [x for x in meta["errors"] if x["job"] == job][-settings.max_attempts:]
        lines = "\n".join(f"{x['attempt']}. <code>{esc(x['error'][:300])}</code>" for x in errs)
        if p.get("chat_id"):
            await self.tg.send(
                p["chat_id"],
                f"❌ <b>#{item_id} failed {settings.max_attempts} times</b> while {label}. Needs your look.\n\n{lines}\n\n"
                f"/retry {item_id} to try again · /failed lists everything waiting",
                reply_to=p.get("mid"),
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
                elif job == "resources":
                    await self.process_resources(**p)
                elif job == "skill":
                    await self.process_skill(**p)
                self._job_done(job, p.get("item_id"))
            except Exception as e:
                log.exception("job %s failed", job)
                try:
                    await self._handle_failure(job, p, e)
                except Exception:
                    log.exception("failure handler crashed")
            finally:
                self.queue.task_done()

    def resume_pending(self) -> int:
        """After a restart, re-queue every job that was queued, running or waiting to retry."""
        n = 0
        for it in self.db.recent(1000):
            pending = (it.get("meta") or {}).get("pending_jobs") or {}
            for job, p in pending.items():
                if "chat_id" not in p:
                    owners = sorted(self.owner_ids())
                    if not owners:
                        continue
                    p["chat_id"] = owners[0]
                self.queue.put_nowait((job, p))
                n += 1
            if pending and it["status"] == "error" and str(it.get("error", "")).startswith("Interrupted"):
                self.db.update(it["id"], status="processing", error=None)
        return n

    async def cmd_failed(self, chat_id: int) -> None:
        rows = [it for it in self.db.recent(300) if (it.get("meta") or {}).get("needs_attention")]
        if not rows:
            return await self.tg.send(chat_id, "✅ Nothing waiting — no items have failed 3 times.")
        lines = ["<b>Needs your look</b> (failed 3×)"]
        for it in rows[:20]:
            last = ((it.get("meta") or {}).get("errors") or [{}])[-1]
            lines.append(f"{self._line(it)}\n    <code>{esc(str(last.get('error', ''))[:160])}</code>")
        lines.append("\n/retry 12 re-runs one · /retry failed re-runs all")
        await self.tg.send(chat_id, "\n".join(lines))

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
        if resources.parse_github(url):
            return await self.process_github_link(item_id, chat_id, mid, url, note)
        work = self._workdir(item_id)
        try:
            try:
                info = await media.download_url(url, work)
            except media.MediaError as e:
                return await self.process_page_link(item_id, chat_id, mid, url, note, str(e))
            await self._process_media(
                item_id, chat_id, mid, Path(info["path"]), note,
                url=info["webpage_url"], title=info["title"], caption=info["caption"], author=info["author"],
                meta={k: info[k] for k in ("extractor", "upload_date") if info.get(k)},
            )
        finally:
            media.cleanup(work)

    async def process_github_link(self, item_id: int, chat_id: int, mid: int, url: str, note: str) -> None:
        info = await resources.github_info(url) or {}
        if info.get("error"):
            self.db.update(item_id, status="done", title=url[:80], error=info["error"])
            return await self.tg.send(chat_id, f"🔗 Saved #{item_id}, but GitHub said: {esc(info['error'])}", reply_to=mid)
        text = resources.describe_github(info)
        fields: dict[str, Any] = {"status": "done", "title": info.get("full_name"), "caption": text,
                                  "source_url": info.get("url") or url}
        if settings.anthropic_key:
            a = await ai.analyze_text(f"{note}\n\n{text}".strip(), url)
            fields.update(self._analysis_fields(a))
            fields["title"] = info.get("full_name") or fields.get("title")
        res = [{"name": info.get("full_name"), "type": "github", "found": True, "url": info.get("url"),
                "confidence": "high", "description": info.get("description") or "", "github": self._gh_summary(info)}]
        fields["resources"] = res
        self.db.update(item_id, **fields)
        it = self.db.get(item_id)
        await self.tg.send(chat_id, self.render(it), reply_to=mid)
        await self.tg.typing(chat_id)
        sec = await security.screen_repo(info.get("url") or url)
        res[0]["security"] = sec
        imported = self._skill_files(info) if sec["verdict"] != "red" else []
        self.db.update(item_id, resources=res, meta=self._meta(item_id) | {"imported_skills": imported, "security_checked": True})
        await self.tg.send(chat_id, security.render(sec, info.get("full_name") or ""), reply_to=mid)
        if settings.anthropic_key and (imported or it.get("skill_candidate")):
            self.enqueue(("skill", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))

    async def process_page_link(self, item_id: int, chat_id: int, mid: int, url: str, note: str, err: str) -> None:
        page = await resources.fetch_page(url)
        fields: dict[str, Any] = {"status": "done", "title": (page or {}).get("title") or url[:80]}
        if page and page.get("text"):
            fields["caption"] = page["text"][:8000]
        else:
            fields["error"] = f"Couldn't download or read this page: {err[:300]}"
        a: dict[str, Any] = {}
        if settings.anthropic_key and (note or page):
            try:
                a = await ai.analyze_text(f"{note}\n\nPage title: {fields['title']}\n\n{fields.get('caption', '')}".strip(), url)
                fields.update(self._analysis_fields(a))
                fields["meta"] = self._meta(item_id) | {"extracted_resources": a.get("resources") or []}
            except Exception as e:
                log.warning("page analysis failed: %s", e)
        self.db.update(item_id, **fields)
        it = self.db.get(item_id)
        prefix = "" if page else f"🔗 Saved #{item_id}, but I couldn't read the page.\n\n"
        await self.tg.send(chat_id, prefix + self.render(it), reply_to=mid)
        self._maybe_queue_followups(item_id, chat_id, mid, a)

    def _maybe_queue_followups(self, item_id: int, chat_id: int, mid: int, a: dict) -> None:
        if not settings.anthropic_key:
            return
        it = self.db.get(item_id) or {}
        slugs = resources.find_slugs(self.item_text(it)) if it else []
        if settings.find_resources and (a.get("resources") or slugs):
            self.enqueue(("resources", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))
        elif a.get("skill_candidate"):
            self.enqueue(("skill", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))

    @staticmethod
    def _gh_summary(info: dict) -> dict:
        return {k: info.get(k) for k in ("full_name", "stars", "pushed_at", "license", "archived", "skill_count")
                } | {"skill_paths": (info.get("skill_paths") or [])[:12]}

    @staticmethod
    def _skill_files(info: dict) -> list[dict]:
        return [{"repo": info.get("full_name"), "path": s["path"], "content": s["content"]}
                for s in info.get("skills") or []]

    async def process_resources(self, item_id: int, chat_id: int, mid: int | None = None, force: bool = False) -> None:
        it = self.db.get(item_id)
        if not it:
            return
        meta = it.get("meta") or {}
        extracted = meta.get("extracted_resources") or []
        if not extracted and resources.find_slugs(self.item_text(it)):
            extracted = []  # repo slugs in the post are resolved directly below
        elif force and not extracted:
            # Re-extract from what we know about the item.
            a = await ai.analyze_text(self.item_text(it), it.get("source_url") or "")
            extracted = a.get("resources") or []
        if not extracted and not resources.find_slugs(self.item_text(it)):
            if force:
                await self.tg.send(chat_id, f"No resources mentioned in #{item_id}.", reply_to=mid)
            return
        await self.tg.typing(chat_id)
        context = "\n".join(filter(None, [
            f"Title: {it.get('title')}", f"Author/account: {it.get('author')}", f"URL: {it.get('source_url')}",
            f"Summary: {it.get('summary')}", f"Caption: {(it.get('caption') or '')[:1500]}",
            f"Transcript: {(it.get('transcript') or '')[:2000]}",
        ]))
        item_text = self.item_text(it)
        # 1) Exact match first: repo slugs read off the video/caption/transcript, confirmed on GitHub. No guessing.
        resolved, rest = await resources.direct_resolve(extracted, item_text)
        # 2) Repos visible in the post that the analysis didn't list as resources.
        have = {(resources.parse_github(r.get("url") or "") or ("", ""))[1].lower() for r in resolved}
        for owner, repo in resources.find_slugs(item_text)[:10]:
            if repo.lower() in have:
                continue
            meta = await resources.verify_repo(owner, repo)
            if meta:
                have.add(repo.lower())
                resolved.append({"name": meta["full_name"], "type": "github", "found": True, "url": meta["url"],
                                 "confidence": "high", "description": meta["description"],
                                 "verified": "repo slug shown in the post, confirmed on GitHub"})
        # 3) Web search only for what is still unresolved, and reject generic / non-matching results.
        if rest:
            web = await ai.resolve_resources(rest, context)
            orig = {(r.get("name") or "").lower(): r for r in rest}
            for w in web:  # keep what the post itself said (identifiers, how to get it)
                o = orig.get((w.get("name") or "").lower(), {})
                merged = {k: v for k, v in o.items() if k in ("identifiers", "how_to_get", "context")} | w
                resolved.append(await resources.validate_resolved(merged))
        imported = list(meta.get("imported_skills") or [])
        screened = 0
        for r in resolved:
            url = r.get("url") or ""
            if resources.parse_github(url):
                info = await resources.github_info(url)
                if info and not info.get("error"):
                    r["github"] = self._gh_summary(info)
                    if screened < settings.max_repo_screens:
                        screened += 1
                        r["security"] = await security.screen_repo(url)
                    else:
                        r["security"] = {"verdict": "yellow", "summary": "Not screened (per-item limit).",
                                         "why": [], "make_safer": [f"Send /security {item_id} or the repo link itself to screen it."]}
                    if r["security"]["verdict"] != "red":
                        imported += self._skill_files(info)
        meta["imported_skills"] = imported[:12]
        meta["security_checked"] = True
        self.db.update(item_id, resources=resolved, meta=meta)
        await self.tg.send(chat_id, self.render_resources(item_id, resolved), reply_to=mid)
        for r in resolved:
            sec = r.get("security") or {}
            if sec.get("why") or sec.get("verdict") in ("yellow", "red"):
                await self.tg.send(chat_id, security.render(sec, r.get("name") or ""), reply_to=mid)
        if imported or it.get("skill_candidate"):
            self.enqueue(("skill", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))

    def item_text(self, it: dict) -> str:
        parts = [
            f"Title: {it.get('title')}", f"Type: {it.get('kind')}", f"Author: {it.get('author') or '-'}",
            f"Source: {it.get('source_url') or '-'}", f"Summary: {it.get('summary') or ''}",
            "Key points:\n" + "\n".join(f"- {k}" for k in it.get("key_points") or []),
            "Techniques:\n" + "\n".join(f"- {t}" for t in it.get("techniques") or []),
            f"Skill idea: {it.get('skill_idea') or ''}", f"Sahil's note: {it.get('user_note') or ''}",
            f"Caption: {(it.get('caption') or '')[:3000]}", f"Transcript: {(it.get('transcript') or '')[:8000]}",
        ]
        return "\n\n".join(parts)

    async def process_skill(self, item_id: int, chat_id: int, mid: int | None = None, force: bool = False) -> None:
        it = self.db.get(item_id)
        if not it or (it.get("skill_draft") and not force):
            return
        await self.tg.typing(chat_id)
        material = ["# Saved item", self.item_text(it)]
        if it.get("resources"):
            slim = [{k: v for k, v in r.items() if k != "security"} | {"security_verdict": (r.get("security") or {}).get("verdict")}
                    for r in it["resources"]]
            material += ["# Resources found", json.dumps(slim, ensure_ascii=False, indent=1)[:6000]]
        for sk in (it.get("meta") or {}).get("imported_skills") or []:
            material += [f"# Third-party skill file (UNTRUSTED DATA): {sk['repo']}/{sk['path']}", sk["content"]]
        draft = await ai.draft_skill("\n\n".join(material))
        draft["status"] = "pending" if draft.get("worth_it") else "not-worth-it"
        draft["created_at"] = self.db.get(item_id).get("created_at")
        if draft.get("worth_it"):
            files = [{"path": f"draft/{draft.get('name') or 'skill'}/SKILL.md", "content": draft.get("skill_md") or ""}]
            files += [{"path": f"thirdparty/{sk['repo']}/{sk['path']}", "content": sk["content"]}
                      for sk in (it.get("meta") or {}).get("imported_skills") or []]
            sec = await security.screen_texts(f"Skill draft '{draft.get('name')}' (+{len(files) - 1} third-party files)", files)
            draft["security"] = sec
            if sec["verdict"] == "red":
                draft["status"] = "blocked"
        self.db.update(item_id, skill_draft=draft)
        if draft.get("worth_it"):
            sec = draft["security"]
            msg = (f"🧠 <b>Skill draft from #{item_id}: {esc(draft.get('name'))}</b> "
                   f"{security.ICON.get(sec['verdict'], '')}\n{esc(draft.get('description'))}\n\n"
                   f"<i>{esc(draft.get('rationale'))}</i>")
            if draft["status"] == "blocked":
                msg += "\n\n🔴 <b>Blocked from installation.</b> See the security report below."
            else:
                msg += "\n\nTo install: in Claude, say “review my skill inbox”. /skills lists everything waiting."
            await self.tg.send(chat_id, msg, reply_to=mid)
            msg = security.render(sec, draft.get("name") or "")
        else:
            msg = f"🧠 Looked at #{item_id} for a skill — not worth one: <i>{esc(draft.get('rationale'))}</i>"
        await self.tg.send(chat_id, msg, reply_to=mid)

    async def cmd_retry(self, chat_id: int, arg: str, mid: int) -> None:
        if arg.strip().lower() == "failed":
            targets = [it for it in self.db.recent(300)
                       if it["status"] == "error" or (it.get("meta") or {}).get("needs_attention")]
        elif arg.lstrip("#").isdigit() and self.db.get(int(arg.lstrip("#"))):
            targets = [self.db.get(int(arg.lstrip("#")))]
        else:
            return await self.tg.send(chat_id, "Usage: /retry 12  or  /retry failed")
        n = 0
        for it in targets:
            meta = it.get("meta") or {}
            meta.pop("needs_attention", None)
            meta["attempts"] = {}
            self.db.update(it["id"], meta=meta)
            if it["status"] != "error":  # item itself fine: a follow-up job failed (resources / skill)
                last = next((x["job"] for x in reversed(meta.get("errors") or []) if x["job"] in ("resources", "skill")), None)
                if last:
                    self.enqueue((last, {"item_id": it["id"], "chat_id": chat_id, "mid": mid, "force": True}))
                    n += 1
                continue
            fid = meta.get("telegram_file_id")
            note = it.get("user_note") or ""
            if it.get("source_url"):
                self.db.update(it["id"], status="processing", error=None, kind="link")
                self.enqueue(("url", {"item_id": it["id"], "chat_id": chat_id, "mid": mid,
                                               "url": it["source_url"], "note": note}))
            elif fid:
                kind = it["kind"] if it["kind"] in ("video", "image", "voice") else "video"
                self.db.update(it["id"], status="processing", error=None)
                self.enqueue(("file", {"item_id": it["id"], "chat_id": chat_id, "mid": mid,
                                                "file_id": fid, "kind": kind, "note": note}))
            else:
                continue
            n += 1
        await self.tg.send(chat_id, f"🔁 Re-running {n} item(s)." if n else "Nothing to retry.", reply_to=mid)

    async def cmd_security(self, chat_id: int, arg: str) -> None:
        if not arg.lstrip("#").isdigit():
            return await self.tg.send(chat_id, "Usage: /security 12")
        it = self.db.get(int(arg.lstrip("#")))
        if not it:
            return await self.tg.send(chat_id, "No item with that number.")
        reports = [security.render(r["security"], r.get("name") or "") for r in it.get("resources") or [] if r.get("security")]
        d = it.get("skill_draft") or {}
        if d.get("security"):
            reports.append(security.render(d["security"], f"skill draft {d.get('name')}"))
        if not reports:
            return await self.tg.send(chat_id, f"Nothing in #{it['id']} has been security-screened yet (no repos or skill drafts).")
        for rep in reports:
            await self.tg.send(chat_id, rep)

    async def cmd_resources(self, chat_id: int, arg: str) -> None:
        n = int(arg) if arg.isdigit() else 15
        rows = []
        for it in self.db.recent(200):
            for r in it.get("resources") or []:
                rows.append((it["id"], r))
            if len(rows) >= n:
                break
        if not rows:
            return await self.tg.send(chat_id, "No resources found yet. Send a reel that mentions a repo, tool or skill.")
        lines = ["<b>Recent resources</b>"] + [self._res_line(r, iid) for iid, r in rows[:n]]
        await self.tg.send(chat_id, "\n".join(lines))

    async def cmd_skills(self, chat_id: int) -> None:
        drafts = [it for it in self.db.recent(300) if (it.get("skill_draft") or {}).get("status") == "pending"]
        if not drafts:
            return await self.tg.send(chat_id, "No skill drafts waiting. Use /skill 12 to draft one from an item.")
        lines = ["<b>Skill inbox</b> (review in Claude: “review my skill inbox”)"]
        lines += [f"🧠 #{it['id']} {security.ICON.get((it['skill_draft'].get('security') or {}).get('verdict'), '')} "
                  f"<b>{esc(it['skill_draft'].get('name'))}</b> — {esc(it['skill_draft'].get('description'))}"
                  for it in drafts[:20]]
        blocked = [it for it in self.db.recent(300) if (it.get("skill_draft") or {}).get("status") == "blocked"]
        if blocked:
            lines.append(f"\n🔴 {len(blocked)} draft(s) blocked by security screening: " + ", ".join(f"#{b['id']}" for b in blocked[:10]))
        await self.tg.send(chat_id, "\n".join(lines))

    @staticmethod
    def _res_line(r: dict, item_id: int | None = None) -> str:
        icon = {"github": "🐙", "skill": "🧠", "prompt": "💬", "paper": "📄", "course": "🎓", "tool": "🛠",
                "website": "🌐", "dataset": "🗂", "template": "📐"}.get(str(r.get("type", "")).lower(), "🔗")
        name = esc(r.get("name") or "?")
        url = r.get("url") or ""
        head = f'{icon} <a href="{esc(url)}">{name}</a>' if url else f"{icon} {name}"
        bits = []
        if not r.get("found", True):
            pass
        elif r.get("confidence") and r.get("confidence") != "high":
            bits.append(f"{r['confidence']} confidence")
        gh = r.get("github") or {}
        if gh.get("stars") is not None:
            bits.append(f"★{gh['stars']}")
        if gh.get("pushed_at"):
            bits.append(f"updated {gh['pushed_at']}")
        if gh.get("skill_count"):
            bits.append(f"<b>{gh['skill_count']} skill file(s)</b>")
        if r.get("free") and r["free"] != "unknown":
            bits.append(esc(r["free"]))
        sec = r.get("security") or {}
        if sec.get("verdict"):
            head = f"{security.ICON.get(sec['verdict'], '')} " + head
        line = head + (f" — {' · '.join(bits)}" if bits else "")
        if r.get("description"):
            line += f"\n    {esc(r['description'])}"
        if not r.get("found", True):
            line += "\n    ❗ <b>Exact link not found.</b>"
            if r.get("lead"):
                line += f' Unverified lead: <a href="{esc(r["lead"])}">{esc(r["lead"][:60])}</a>'
            if r.get("how_to_get"):
                line += f"\n    To get it from the creator: {esc(r['how_to_get'])}"
        if r.get("note"):
            line += f"\n    <i>{esc(r['note'])}</i>"
        if item_id:
            line += f"  <i>(#{item_id})</i>"
        return line

    def render_resources(self, item_id: int, res: list[dict]) -> str:
        if not res:
            return f"🔎 #{item_id}: no resources could be identified."
        return f"🔎 <b>Resources from #{item_id}</b>\n" + "\n".join(self._res_line(r) for r in res)

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
            self.enqueue(("tag_note", {"item_id": item_id}))

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
            "media_dir": str(mdir), "meta": self._meta(item_id) | {**meta, "transcriber": backend, "frames": len(frames)},
        }
        if settings.anthropic_key:
            a = await ai.analyze(kind=kind, frames=frames, transcript=transcript, caption=caption, title=title,
                                 author=author, user_note=note, url=url, duration=duration)
            fields.update(self._analysis_fields(a))
            fields["meta"]["accuracy_flags"] = a.get("accuracy_flags", [])
            fields["meta"]["phi_flag"] = bool(a.get("phi_flag"))
            fields["meta"]["extracted_resources"] = a.get("resources") or []
        else:
            a = {}
            fields["summary"] = "(Not analysed: ANTHROPIC_API_KEY not set.)"
        fields["status"] = "done"
        self.db.update(item_id, **fields)
        it = self.db.get(item_id)
        await self.tg.send(chat_id, self.render(it), reply_to=mid)
        if not (fields["meta"].get("phi_flag")):
            self._maybe_queue_followups(item_id, chat_id, mid, a)

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
        if it.get("resources"):
            parts.append("<b>Resources</b>\n" + "\n".join(self._res_line(r) for r in it["resources"][:8]))
        elif (meta.get("extracted_resources")) and settings.find_resources:
            parts.append("🔎 <i>Looking up the resources it mentions…</i>")
        if (it.get("skill_draft") or {}).get("status") == "pending":
            parts.append(f"🧠 Skill draft waiting: <b>{esc(it['skill_draft'].get('name'))}</b>")
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
