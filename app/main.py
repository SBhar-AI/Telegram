"""FastAPI entrypoint: Telegram webhook, MCP connector, and a tiny private item viewer."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import mcp_server
from .bot import Hub
from .config import settings
from .db import DB
from .telegram import Telegram, esc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("hub")
# httpx logs full request URLs at INFO, which would include the Telegram bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

db = DB(settings.db_path)
tg = Telegram(settings.telegram_token)
hub = Hub(db, tg)
mcp_server.bind(hub)
mcp_app = mcp_server.mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    n = db.mark_interrupted()
    resumed = hub.resume_pending()
    log.info("resumed %d pending job(s); %d interrupted item(s) without a resumable job", resumed, n)
    worker = asyncio.create_task(hub.worker())
    if tg.enabled:
        import re as _re
        tok = settings.telegram_token
        log.info("telegram token check: length=%d secret_part=%d well_formed=%s", len(tok),
                 len(tok.split(":", 1)[-1]), bool(_re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{35}", tok)))
        me = await tg.call("getMe")
        log.info("telegram getMe -> %s", (me or {}).get("username") if me else "FAILED (token rejected by Telegram)")
    if tg.enabled and settings.public_url:
        res = await tg.set_webhook(f"{settings.public_url}/telegram/webhook", settings.webhook_secret)
        log.info("setWebhook -> %s", res)
        await tg.set_commands()
    else:
        log.warning("Telegram not configured (token or public URL missing); webhook not set.")
    async with mcp_server.mcp.session_manager.run():
        yield
    worker.cancel()


app = FastAPI(title="Telegram Knowledge Hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

if settings.mcp_secret:
    app.mount(f"/s/{settings.mcp_secret}", mcp_app)


@app.get("/")
async def root():
    return {"service": "telegram-knowledge-hub", "ok": True}


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "telegram": tg.enabled,
        "public_url": bool(settings.public_url),
        "claude": bool(settings.anthropic_key),
        "transcriber": settings.transcriber,
        "todoist": bool(settings.todoist_token),
        "mcp": bool(settings.mcp_secret),
        "owner_claimed": bool(hub.owner_ids()),
        "queue": hub.queue.qsize(),
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    got = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not settings.webhook_secret or not hmac.compare_digest(got, settings.webhook_secret):
        raise HTTPException(status_code=403)
    update = await request.json()
    asyncio.create_task(hub.handle_update(update))  # answer Telegram immediately
    return JSONResponse({"ok": True})


def _check_secret(secret: str) -> None:
    if not settings.mcp_secret or not hmac.compare_digest(secret, settings.mcp_secret):
        raise HTTPException(status_code=404)


@app.get("/view/{secret}/item/{item_id}", response_class=HTMLResponse)
async def view_item(secret: str, item_id: int):
    _check_secret(secret)
    it = db.get(item_id)
    if not it:
        raise HTTPException(status_code=404)
    frames = sorted(Path(it["media_dir"]).glob("frame_*.jpg")) if it.get("media_dir") else []
    imgs = "".join(f'<img src="/view/{secret}/item/{item_id}/frame/{i}" loading="lazy">' for i in range(len(frames)))
    # render() already escapes all content and emits a small safe tag set (b, i, code, a href)
    body = hub.render(it, full=True).replace("\n", "<br>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>#{item_id}</title><style>body{{font:16px/1.5 system-ui;max-width:760px;margin:24px auto;padding:0 16px;background:#fafafa;color:#111}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:8px;margin:16px 0}}img{{width:100%;border-radius:6px}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}}}}</style></head>
<body><div class="g">{imgs}</div><div>{body}</div></body></html>"""


@app.get("/view/{secret}/json/{item_id}")
async def view_item_json(secret: str, item_id: int):
    """Raw stored record (debugging what the bot extracted and resolved)."""
    _check_secret(secret)
    it = db.get(item_id)
    if not it:
        raise HTTPException(status_code=404)
    it = dict(it)
    for d in [it.get("skill_draft") or {}] + [r.get("security") or {} for r in it.get("resources") or []]:
        d.pop("sha256", None)
    return JSONResponse(it)


@app.get("/view/{secret}/item/{item_id}/refind")
async def refind(secret: str, item_id: int):
    """Re-run resource resolution for an item (owner gets the result in Telegram)."""
    _check_secret(secret)
    if not db.get(item_id) or not hub.owner_ids():
        raise HTTPException(status_code=404)
    hub.enqueue(("resources", {"item_id": item_id, "chat_id": sorted(hub.owner_ids())[0], "mid": None, "force": True}))
    return {"queued": item_id}


@app.get("/view/{secret}/item/{item_id}/dump")
async def dump(secret: str, item_id: int):
    """Write an item's exact skill draft + third-party skill files to the server log (base64 chunks), so they can be
    read verbatim via the Railway log API for an independent security review."""
    import base64
    import json as _json
    _check_secret(secret)
    it = db.get(item_id)
    if not it:
        raise HTTPException(status_code=404)
    payload = _json.dumps({"skill_draft": it.get("skill_draft") or {},
                           "imported_skills": (it.get("meta") or {}).get("imported_skills") or []}, ensure_ascii=False)
    b = base64.b64encode(payload.encode()).decode()
    chunks = [b[i:i + 3000] for i in range(0, len(b), 3000)]
    for i, c in enumerate(chunks):
        log.info("DUMP%d %d/%d %s", item_id, i + 1, len(chunks), c)
    return {"item": item_id, "chunks": len(chunks)}


@app.get("/view/{secret}/item/{item_id}/frame/{n}")
async def view_frame(secret: str, item_id: int, n: int):
    _check_secret(secret)
    it = db.get(item_id)
    frames = sorted(Path(it["media_dir"]).glob("frame_*.jpg")) if it and it.get("media_dir") else []
    if n < 0 or n >= len(frames):
        raise HTTPException(status_code=404)
    return FileResponse(frames[n], media_type="image/jpeg")
