"""Message routing and the processing pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from . import ai, costs, media, resources, security, todoist
from .config import settings
from .db import DB, now_iso
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
/find 12 — re-run resource search for an item · /skill 12 — draft a skill from it (on demand)
/security 12 — full security report (why + how to make it safer)
/cost — estimated AI spend today / this week
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
        if cmd == "/cost":
            return await self.tg.send(chat_id, self.cost_text())
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
            f"{ok(bool(settings.mcp_secret))} MCP connector\n"
            f"AI spend today (est.): ${costs.spent_today():.2f}"
            + (f" of ${settings.daily_spend_cap_usd:.2f} cap" if settings.daily_spend_cap_usd else "") + " · /cost"
        )

    def cost_text(self) -> str:
        s = costs.summary()
        lines = ["<b>Estimated AI spend</b> (from the token counts the API reports × list prices)"]
        for label, d in s.items():
            per = f" · ~${d['cost'] / d['items']:.3f}/item" if d["items"] else ""
            lines.append(f"<b>{label}</b>: ${d['cost']:.2f} — {d['calls']} calls, {d['items']} items, "
                         f"{d['searches']} web searches{per}")
        week = s["7 days"]["by_stage"]
        if week:
            lines.append("\n<b>Last 7 days by step</b>\n" + "\n".join(
                f"• {esc(st or 'other')}: ${c:.2f} ({n} calls)" for st, (c, n) in week.items()))
        cap = settings.daily_spend_cap_usd
        lines.append(f"\nDaily cap: ${cap:.2f}" + (" — <b>reached: cheap mode until midnight UTC</b>" if costs.over_cap() else "")
                     if cap else "\nDaily cap: off")
        lines.append(f"<i>Prices as of {costs.PRICES_AS_OF}. Estimates only; the Anthropic Console has the real bill. "
                     "Calls made before this tracking was added aren't counted.</i>")
        return "\n".join(lines)

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
        # Queue first, acknowledge second: a failed acknowledgement must never lose the save.
        self.enqueue(("url", {"item_id": item_id, "chat_id": chat_id, "mid": mid, "url": url, "note": note}))
        await self.tg.react(chat_id, mid)
        await self.tg.send(chat_id, f"⏳ Got it (#{item_id}). Downloading and analysing…", reply_to=mid)

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
        self.enqueue(
            ("file", {"item_id": item_id, "chat_id": chat_id, "mid": mid, "file_id": f["file_id"], "kind": kind, "note": note})
        )
        await self.tg.react(chat_id, mid)
        if kind != "voice":
            await self.tg.send(chat_id, f"⏳ Got it (#{item_id}). Analysing…", reply_to=mid)

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
        if isinstance(e, ai.AccountError):
            return await self._park_account_error(job, p, it, err)
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
                 "skill": "drafting a skill", "tag_note": "tagging the note",
                 "breakdown": "writing the breakdown"}.get(job, job)
        errs = [x for x in meta["errors"] if x["job"] == job][-settings.max_attempts:]
        lines = "\n".join(f"{x['attempt']}. <code>{esc(x['error'][:300])}</code>" for x in errs)
        if p.get("chat_id"):
            await self.tg.send(
                p["chat_id"],
                f"❌ <b>#{item_id} failed {settings.max_attempts} times</b> while {label}. Needs your look.\n\n{lines}\n\n"
                f"/retry {item_id} to try again · /failed lists everything waiting",
                reply_to=p.get("mid"),
            )

    async def _park_account_error(self, job: str, p: dict, it: dict, err: str) -> None:
        """Out of credit / bad API key: retrying can't help. Park the item (no retries) and tell Sahil once."""
        item_id = it["id"]
        meta = it.get("meta") or {}
        meta.setdefault("errors", []).append({"job": job, "attempt": int((meta.get("attempts") or {}).get(job, 0)) + 1,
                                              "error": err})
        meta["errors"] = meta["errors"][-9:]
        (meta.get("pending_jobs") or {}).pop(job, None)
        meta["needs_attention"] = True
        meta["parked"] = {"job": job, "reason": "account", "at": now_iso()}
        fields: dict[str, Any] = {"meta": meta}
        if job in self.ITEM_JOBS:
            fields.update(status="error", error="Parked: " + err)
        self.db.update(item_id, **fields)
        log.warning("job %s #%s parked (account/billing error, not retried): %s", job, item_id, err)
        chat_id = p.get("chat_id") or next(iter(sorted(self.owner_ids())), None)
        last = self.db.kv_get("account_alert_at")
        if last:
            try:
                from datetime import datetime, timezone
                age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                if age_h < settings.billing_alert_hours:
                    return
            except ValueError:
                pass
        if not chat_id:
            return
        self.db.kv_set("account_alert_at", now_iso())
        credit = "credit" in err.lower() or "billing" in err.lower()
        what = ("⚠️ <b>Anthropic API credits are out</b> — add funds at console.anthropic.com → Billing, "
                "then send /retry failed." if credit else
                "⚠️ <b>The Anthropic API key was rejected</b> (invalid or not allowed) — fix ANTHROPIC_API_KEY, "
                "then send /retry failed.")
        await self.tg.send(chat_id, f"{what}\n\nI've saved #{item_id} and parked it without retrying; anything else "
                                    f"you send is saved and parked the same way. I won't repeat this message for "
                                    f"{settings.billing_alert_hours:g} hours.", reply_to=p.get("mid"))

    async def worker(self) -> None:
        while True:
            job, p = await self.queue.get()
            costs.current_item.set(p.get("item_id"))
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
                elif job == "breakdown":
                    await self.process_breakdown(**p)
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
            # Orphans: created as "processing" but never queued (e.g. #62 on 2026-11-04). Re-queue link saves.
            # (mark_interrupted runs first at startup, so these arrive as status=error "Interrupted…".)
            interrupted = it["status"] == "error" and str(it.get("error", "")).startswith("Interrupted")
            if not pending and interrupted and it.get("source_url") and not it.get("summary"):
                self.db.update(it["id"], status="processing", error=None)
                owners = sorted(self.owner_ids())
                if owners:
                    self.enqueue(("url", {"item_id": it["id"], "chat_id": owners[0], "mid": None,
                                          "url": it["source_url"], "note": it.get("user_note") or ""}))
                    n += 1
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
                if media.instagram_shortcode(url):  # photo / carousel post: get the images instead of the page
                    try:
                        ph = await media.instagram_photos(url, work)
                    except media.MediaError as pe:
                        log.info("instagram photos #%d: %s", item_id, pe)
                        return await self.process_page_link(item_id, chat_id, mid, url, note, f"{e}; {pe}")
                    log.info("instagram photos #%d: %d of %d slide(s) via %s", item_id, len(ph["paths"]),
                             ph["total_slides"], ph["source"])
                    return await self._process_media(
                        item_id, chat_id, mid, ph["paths"][0], note, url=ph["webpage_url"], title="",
                        caption=ph["caption"], author=ph["author"], force_kind="image", images=ph["paths"],
                        meta={"extractor": "Instagram photos", "slides": len(ph["paths"]),
                              "total_slides": ph["total_slides"], "video_slides": ph["video_slides"],
                              "photo_source": ph["source"]},
                    )
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
        await self.tg.send(chat_id, f"🔎 Reading {esc(info.get('full_name'))} and screening it — full breakdown in a few minutes (#{item_id}).", reply_to=mid)
        await self.tg.typing(chat_id)
        sec = await security.screen_repo(info.get("url") or url)
        res[0]["security"] = sec
        imported = self._skill_files(info) if sec["verdict"] != "red" else []
        self.db.update(item_id, resources=res, meta=self._meta(item_id) | {"imported_skills": imported, "security_checked": True})
        nxt = "skill" if settings.anthropic_key and self._auto_skill(chat_id) and (imported or it.get("skill_candidate")) else "breakdown"
        self.enqueue((nxt, {"item_id": item_id, "chat_id": chat_id, "mid": mid}))

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
            except ai.AccountError:
                raise
            except Exception as e:
                log.warning("page analysis failed: %s", e)
        self.db.update(item_id, **fields)
        it = self.db.get(item_id)
        if self._maybe_queue_followups(item_id, chat_id, mid, a):
            return await self.tg.send(chat_id, self._ack(it), reply_to=mid)
        prefix = "" if page else f"🔗 Saved #{item_id}, but I couldn't read the page.\n\n"
        await self.tg.send(chat_id, prefix + self.render(it), reply_to=mid)

    def _maybe_queue_followups(self, item_id: int, chat_id: int, mid: int, a: dict) -> bool:
        """Queue resource lookup / skill drafting. Returns True if a follow-up (and so a final breakdown) is coming."""
        if not settings.anthropic_key:
            return False
        it = self.db.get(item_id) or {}
        slugs = resources.find_slugs(self.item_text(it)) if it else []
        if settings.find_resources and (a.get("resources") or slugs):
            self.enqueue(("resources", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))
            return True
        if a.get("skill_candidate"):
            nxt = "skill" if self._auto_skill(chat_id) else "breakdown"
            self.enqueue((nxt, {"item_id": item_id, "chat_id": chat_id, "mid": mid}))
            return True
        return False

    # ------------------------------------------------------------------ cost controls
    def _auto_skill(self, chat_id: int | None = None) -> bool:
        """Draft skills automatically? Off by default (on demand with /skill 12); never when over the daily cap."""
        return settings.auto_skill_draft and not self._cheap_mode(chat_id)

    @staticmethod
    def _fire(coro: Any) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # no loop (sync caller): drop the notice rather than crash
            coro.close()

    def _cheap_mode(self, chat_id: int | None = None) -> bool:
        """True when today's estimated spend has reached the cap. Tells Sahil once per UTC day."""
        if not costs.over_cap():
            return False
        today = now_iso()[:10]
        if chat_id and self.db.kv_get("cap_alert_day") != today:
            self.db.kv_set("cap_alert_day", today)
            self._fire(self.tg.send(chat_id, (
                f"💸 Today's estimated AI spend reached ${costs.spent_today():.2f} "
                f"(daily cap ${settings.daily_spend_cap_usd:.2f}). Until midnight UTC I'll only do the cheap steps: "
                "summary, exact repo links and the automatic safety scan. No web searches, no AI security review, "
                "no skill drafts. /find 12 or /skill 12 still run a step on request. /cost for details.")))
        return True

    @staticmethod
    def _ack(it: dict) -> str:
        return (f"✅ Got it (#{it['id']}): <b>{esc(it.get('title') or 'saved')}</b>\n"
                "🔎 Finding the links and screening them for safety — "
                "you'll get one full breakdown in a few minutes.")

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
        slugs = resources.find_slugs(item_text)[:10]
        log.info("resources #%s: %d extracted, %d resolved directly, %d slugs in post", item_id, len(extracted), len(resolved), len(slugs))
        for owner, repo in slugs:
            if repo.lower() in have:
                continue
            repo_meta = await resources.verify_repo(owner, repo)
            log.info("  slug %s/%s -> %s", owner, repo, (repo_meta or {}).get("url"))
            if repo_meta:
                have.add(repo.lower())
                resolved.append({"name": repo_meta["full_name"], "type": "github", "found": True, "url": repo_meta["url"],
                                 "confidence": "high", "description": repo_meta["description"],
                                 "verified": "repo slug shown in the post, confirmed on GitHub"})
        # 3) Well-known products get their official link for free; web search (paid) only for specific named
        #    things still unresolved, never in cheap mode unless Sahil asked (/find), and reject generic results.
        searchable = []
        for r in rest:
            wk = resources.well_known(r)
            if wk:
                resolved.append(wk)
            elif resources.needs_search(r):
                searchable.append(r)
            else:
                resolved.append({**r, "found": False, "url": "", "confidence": "low",
                                 "note": "Too generic to look up (no name, handle or repo shown)."})
        rest = searchable
        if rest and self._cheap_mode(chat_id) and not force:
            resolved += [{**r, "found": False, "url": "", "confidence": "low",
                          "note": f"Not searched: daily AI spending cap reached. /find {item_id} to search now."}
                         for r in rest]
            rest = []
        if rest:
            web = await ai.resolve_resources(rest, context)
            orig = {(r.get("name") or "").lower(): r for r in rest}
            for w in web:  # keep what the post itself said (identifiers, how to get it)
                o = orig.get((w.get("name") or "").lower(), {})
                merged = {k: v for k, v in o.items() if k in ("identifiers", "how_to_get", "context")} | w
                resolved.append(await resources.validate_resolved(merged))
        imported = list(meta.get("imported_skills") or [])
        screened = 0
        resolved = self._dedupe(resolved)
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
                        r["security"] = {"verdict": "unscreened", "summary": "Not screened yet (per-item limit).",
                                         "why": [], "make_safer": ["Send the repo link to the bot on its own to screen it."]}
                    if r["security"]["verdict"] != "red":
                        imported += self._skill_files(info)
        meta["imported_skills"] = imported[:12]
        meta["security_checked"] = True
        self.db.update(item_id, resources=resolved, meta=meta)
        need_skill = bool(imported or it.get("skill_candidate")) and self._auto_skill(chat_id)
        self.enqueue(("skill" if need_skill else "breakdown", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))

    @staticmethod
    def _dedupe(resolved: list[dict]) -> list[dict]:
        """One entry per repo/URL, so the same repo is never looked up or screened twice in one item."""
        out, seen = [], {}
        for r in resolved:
            gh = resources.parse_github(r.get("url") or "")
            key = ("gh:" + "/".join(gh).lower()) if gh else (r.get("url") or "").rstrip("/").lower()
            if key and key in seen:
                prev = seen[key]
                if r.get("name") and r.get("name") != prev.get("name"):
                    prev.setdefault("aliases", []).append(r["name"])
                continue
            if key:
                seen[key] = r
            out.append(r)
        return out

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
        if not it:
            return
        if it.get("skill_draft") and not force:  # already drafted: just send the breakdown
            return self.enqueue(("breakdown", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))
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
        self.enqueue(("breakdown", {"item_id": item_id, "chat_id": chat_id, "mid": mid}))

    # ------------------------------------------------------------------ the one final message
    async def process_breakdown(self, item_id: int, chat_id: int, mid: int | None = None, force: bool = False) -> None:
        it = self.db.get(item_id)
        if not it:
            return
        res = it.get("resources") or []
        names = [r.get("name") or "" for r in res]
        bd: dict[str, Any] = {}
        if settings.anthropic_key:
            try:
                bd = await ai.compose_breakdown(self.item_text(it), names)
            except ai.AccountError:
                raise
            except Exception as e:
                log.warning("breakdown compose failed, using fallback: %s", e)
        if not bd.get("items"):
            bd = {"headline": it.get("title") or "Saved item",
                  "items": [{"label": "", "what_it_does": k, "resource_names": []} for k in (it.get("key_points") or [])[:8]],
                  "for_you": [it["skill_idea"]] if it.get("skill_idea") else []}
        meta = it.get("meta") or {}
        meta["breakdown"] = bd
        self.db.update(item_id, meta=meta)
        await self.tg.send(chat_id, self.render_breakdown(self.db.get(item_id), bd), reply_to=mid)

    def _res_compact(self, r: dict) -> str:
        sec = r.get("security") or {}
        v = sec.get("verdict")
        if r.get("url"):
            label = (resources.parse_github(r["url"]) and "/".join(resources.parse_github(r["url"]))) or r.get("name")
            line = f'🔗 <a href="{esc(r["url"])}">{esc(label)}</a>'
        else:
            line = f"❗ {esc(r.get('name'))}: exact link not found"
            if r.get("how_to_get"):
                line += f" (creator says: {esc(r['how_to_get'])})"
            if r.get("lead"):
                line += f' · <a href="{esc(r["lead"])}">unverified lead</a>'
        gh = r.get("github") or {}
        if gh.get("stars") is not None:
            st = gh["stars"]
            line += f" · ★{st / 1000:.1f}K" if st >= 1000 else f" · ★{st}"
        if v:
            verdict_txt = {"red": "don't install yet", "yellow": "use with care", "green": "looks safe",
                           "unscreened": "not screened yet"}.get(v, v)
            line += f"\n   {security.ICON.get(v, '⚪')} <b>{verdict_txt}</b>"
            if sec.get("summary") and v != "green":
                line += f": {esc(sec['summary'])}"
            if v in ("red", "yellow") and sec.get("make_safer"):
                line += f"\n   ↳ Safer: {esc(sec['make_safer'][0])}"
        return line

    def render_breakdown(self, it: dict, bd: dict) -> str:
        res = it.get("resources") or []
        by_name = {(r.get("name") or "").lower(): r for r in res}
        for r in res:
            for a in r.get("aliases") or []:
                by_name.setdefault(a.lower(), r)
        used: set[int] = set()
        icon = {"video": "🎬", "image": "🖼", "link": "🔗", "note": "📝", "voice": "🎙"}.get(it.get("kind"), "•")
        head = f"{icon} <b>#{it['id']} · {esc(bd.get('headline') or it.get('title') or '')}</b>"
        if it.get("author") or it.get("source_url"):
            src = esc(it.get("author") or "")
            if it.get("source_url"):
                src += f' · <a href="{esc(it["source_url"])}">source</a>'
            head += "\n" + src.strip(" ·")
        parts = [head]
        if (it.get("meta") or {}).get("phi_flag"):
            parts.append(f"🚨 <b>Possible patient information detected.</b> Consider /delete {it['id']}")
        rows = []
        for n, item in enumerate(bd.get("items") or [], 1):
            label = f"<b>{n}. {esc(item.get('label'))}</b> — " if item.get("label") else f"<b>{n}.</b> "
            row = label + esc(item.get("what_it_does"))
            for nm in item.get("resource_names") or []:
                r = by_name.get(nm.lower()) or next((x for x in res if nm.lower() in (x.get("name") or "").lower()
                                                     or (x.get("name") or "").lower() in nm.lower()), None)
                if r is not None and id(r) not in used:
                    used.add(id(r))
                    row += "\n   " + self._res_compact(r)
            rows.append(row)
        if rows:
            parts.append("\n\n".join(rows))
        leftovers = [r for r in res if id(r) not in used]
        if leftovers:
            parts.append("<b>Other links</b>\n" + "\n".join("• " + self._res_compact(r) for r in leftovers))
        if bd.get("for_you"):
            parts.append("<b>For you</b>\n" + "\n".join(f"• {esc(x)}" for x in bd["for_you"][:3]))
        d = it.get("skill_draft") or {}
        if d.get("worth_it"):
            sec = d.get("security") or {}
            line = f"🧠 <b>Skill draft:</b> {esc(d.get('name'))} {security.ICON.get(sec.get('verdict'), '')}\n{esc(d.get('description'))}"
            line += ("\n🔴 Blocked by security screening. /security " + str(it["id"]) if d.get("status") == "blocked"
                     else "\nTo install, tell Claude: “review my skill inbox”.")
            parts.append(line)
        elif not d and it.get("skill_candidate") and it.get("skill_idea"):
            parts.append(f"💡 <b>Skill idea:</b> {esc(it['skill_idea'])}\nSend /skill {it['id']} to have it drafted.")
        cav = (bd.get("caveats") or [])[:2]
        if cav:
            parts.append("<i>⚠️ " + " · ".join(esc(c) for c in cav) + "</i>")
        parts.append(f"<i>/security {it['id']} full safety reports · /get {it['id']} summary + transcript</i>")
        return "\n\n".join(parts)

    async def cmd_retry(self, chat_id: int, arg: str, mid: int) -> None:
        if arg.strip().lower() == "photos":  # Instagram posts saved before photo support: images never read
            targets = [it for it in self.db.recent(1000) if it.get("kind") == "link"
                       and media.instagram_shortcode(it.get("source_url") or "") and not (it.get("meta") or {}).get("frames")]
            if not targets:
                return await self.tg.send(chat_id, "✅ No Instagram photo posts waiting to be re-read.")
        elif arg.strip().lower() == "failed":
            targets = [it for it in self.db.recent(300)
                       if it["status"] == "error" or (it.get("meta") or {}).get("needs_attention")]
        elif arg.lstrip("#").isdigit() and self.db.get(int(arg.lstrip("#"))):
            targets = [self.db.get(int(arg.lstrip("#")))]
        else:
            return await self.tg.send(chat_id, "Usage: /retry 12  ·  /retry failed  ·  /retry photos")
        n = 0
        for it in targets:
            meta = it.get("meta") or {}
            meta.pop("needs_attention", None)
            meta["attempts"] = {}
            self.db.update(it["id"], meta=meta)
            redo_photo = (it.get("kind") == "link" and media.instagram_shortcode(it.get("source_url") or "")
                          and not (it.get("meta") or {}).get("frames"))
            if it["status"] != "error" and not redo_photo:  # item itself fine: a follow-up job failed (resources / skill)
                last = next((x["job"] for x in reversed(meta.get("errors") or [])
                             if x["job"] in ("resources", "skill", "breakdown")), None)
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
        caption: str, author: str, meta: dict, force_kind: str | None = None, images: list[Path] | None = None,
    ) -> None:
        work = path.parent
        mdir = self._mediadir(item_id)
        kind = force_kind or "video"
        if kind == "image":
            frames = []
            for i, img in enumerate(images or [path]):
                try:
                    frames.append(await media.image_to_jpeg(img, mdir / f"frame_{i:02d}.jpg"))
                except media.MediaError as e:
                    log.info("slide %d unreadable: %s", i, e)
            if not frames:
                raise media.MediaError("None of the images could be read.")
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
            "error": None,
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
        if not fields["meta"].get("phi_flag") and self._maybe_queue_followups(item_id, chat_id, mid, a):
            return await self.tg.send(chat_id, self._ack(it), reply_to=mid)
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
