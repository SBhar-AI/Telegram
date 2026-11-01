"""End-to-end tests with Telegram, Claude, Todoist and transcription mocked. ffmpeg runs for real."""
import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp()
os.environ.update({
    "DATA_DIR": TMP, "TELEGRAM_BOT_TOKEN": "", "CLAIM_CODE": "abc123", "ANTHROPIC_API_KEY": "test",
    "TODOIST_API_TOKEN": "test", "MCP_SECRET": "s3cret", "TRANSCRIBE_BACKEND": "openai", "OPENAI_API_KEY": "x",
    "PUBLIC_URL": "https://example.test",
})

from app import ai, bot, media, todoist  # noqa: E402
from app.main import app, db, hub  # noqa: E402

SENT: list[str] = []
OWNER = 555


@pytest.fixture(autouse=True)
def mocks(monkeypatch):
    SENT.clear()

    async def send(chat_id, text, reply_to=None):
        SENT.append(text)

    async def noop(*a, **k):
        return None

    async def fake_claude(system, content, max_tokens=2000):
        if system == ai.ASK_SYSTEM:
            return "You saved a reel on wide-complex tachycardia [#1]."
        return json.dumps({
            "title": "Brugada vs RBBB in 30 seconds", "summary": "Shows V1-V2 coved ST elevation.",
            "key_points": ["Coved ST elevation ≥2 mm in V1-V2"], "techniques": ["Split-screen comparison"],
            "tags": ["ekg", "brugada"], "category": "medical-education", "skill_candidate": True,
            "skill_idea": "EKG side-by-side explainer template", "accuracy_flags": [], "phi_flag": False,
        })

    async def fake_transcribe(audio):
        return "todo email the cardiology chair about wait times", "openai"

    TASKS.clear()

    async def fake_task(content, labels=None, description=""):
        TASKS.append((content, labels))
        return {"id": "1", "url": "https://todoist.com/t/1"}

    monkeypatch.setattr(hub.tg, "send", send)
    monkeypatch.setattr(hub.tg, "react", noop)
    monkeypatch.setattr(hub.tg, "typing", noop)
    monkeypatch.setattr(ai, "claude", fake_claude)
    monkeypatch.setattr(ai, "transcribe", fake_transcribe)
    monkeypatch.setattr(todoist, "add_task", fake_task)


TASKS: list = []


def make_video(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=12:size=640x360:rate=24",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path)],
        check=True,
    )
    return path


def upd(text="", uid=OWNER, **extra):
    msg = {"message_id": 1, "from": {"id": uid}, "chat": {"id": uid, "type": "private"}, "text": text, **extra}
    return {"update_id": abs(hash((text, uid, json.dumps(extra, sort_keys=True)))) % 10**9, "message": msg}


async def drain():
    while not hub.queue.empty():
        job, p = hub.queue.get_nowait()
        if job == "url":
            await hub.process_url(**p)
        elif job == "file":
            await hub.process_file(**p)
        elif job == "tag_note":
            await hub.process_note_tags(**p)


def test_claim_and_allowlist():
    async def go():
        await hub.handle_update(upd("/help", uid=999))
        assert "private bot" in SENT[-1]
        await hub.handle_update(upd("/start wrong", uid=OWNER))
        assert not hub.owner_ids()
        await hub.handle_update(upd("/start abc123", uid=OWNER))
        assert hub.is_owner(OWNER)
        SENT.clear()
        await hub.handle_update(upd("/help", uid=999))  # stranger ignored silently once claimed
        assert SENT == []
    asyncio.run(go())


def test_video_link_pipeline(monkeypatch):
    async def fake_download(url, work):
        p = make_video(work / "source.mp4")
        return {"path": str(p), "title": "IG reel", "caption": "Learn Brugada #ekg", "author": "ekgteacher",
                "duration": 12, "webpage_url": url, "extractor": "Instagram", "upload_date": ""}
    monkeypatch.setattr(media, "download_url", fake_download)

    async def go():
        await hub.handle_update(upd("https://www.instagram.com/reel/ABC123/ great Brugada example"))
        await drain()
    asyncio.run(go())
    it = db.recent(1)[0]
    assert it["kind"] == "video" and it["status"] == "done", it
    assert it["user_note"] == "great Brugada example"
    assert it["category"] == "medical-education" and it["skill_candidate"]
    frames = list(Path(it["media_dir"]).glob("frame_*.jpg"))
    assert len(frames) == media.frame_count(12)
    assert "Brugada vs RBBB" in SENT[-1] and "Techniques worth stealing" in SENT[-1]
    assert db.search("brugada")[0]["id"] == it["id"]


def test_direct_video_and_size_limit(monkeypatch):
    async def fake_dl(file_id, dest):
        return make_video(dest.with_suffix(".mp4"))
    monkeypatch.setattr(hub.tg, "download_file", fake_dl)

    async def go():
        await hub.handle_update(upd("", video={"file_id": "F1", "file_size": 1000}, caption="my clip"))
        await drain()
        await hub.handle_update(upd("", video={"file_id": "F2", "file_size": 50_000_000}))
    asyncio.run(go())
    assert "20 MB" in SENT[-1]
    vids = db.recent(5, kind="video")
    assert any(v["user_note"] == "my clip" and v["status"] == "done" for v in vids)


def test_voice_todo_and_commands(monkeypatch):
    async def fake_dl(file_id, dest):
        p = dest.with_suffix(".ogg")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=2", str(p)], check=True)
        return p
    monkeypatch.setattr(hub.tg, "download_file", fake_dl)

    async def go():
        await hub.handle_update(upd("", voice={"file_id": "V1", "file_size": 2000}))
        await drain()
        assert TASKS[-1][0].startswith("email the cardiology chair"), TASKS
        await hub.handle_update(upd("/queue build the PREVENT HF filter"))
        assert TASKS[-1] == ("build the PREVENT HF filter", ["telegram", "agent"])
        await hub.handle_update(upd("Remember: lead with the mechanism, then the tracing #teaching"))
        assert "Saved as" in SENT[-1]
        await hub.handle_update(upd("/ask what did I save about wide complex tachycardia brugada"))
        assert "[#1]" in SENT[-1]
        await hub.handle_update(upd("/recent"))
        assert "<b>Recent</b>" in SENT[-1]
        await hub.handle_update(upd("/status"))
        assert "Items saved" in SENT[-1]
    asyncio.run(go())
    note = db.recent(1, kind="note")[0]
    assert "teaching" in note["tags"]


def test_failed_download_keeps_link(monkeypatch):
    async def bad(url, work):
        raise media.MediaError("login required")
    monkeypatch.setattr(media, "download_url", bad)

    async def go():
        await hub.handle_update(upd("/save https://www.instagram.com/p/XYZ/ check this"))
        await drain()
    asyncio.run(go())
    it = db.recent(1)[0]
    assert it["kind"] == "link" and it["status"] == "done" and "login required" in it["error"]


def test_http_endpoints_and_mcp():
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert c.get("/healthz").json()["mcp"] is True
        assert c.post("/telegram/webhook", json={}).status_code == 403
        vid = db.recent(10, kind="video")[0]["id"]
        assert c.get(f"/view/wrong/item/{vid}").status_code == 404
        page = c.get(f"/view/s3cret/item/{vid}")
        assert page.status_code == 200 and "<img" in page.text
        assert c.get(f"/view/s3cret/item/{vid}/frame/0").headers["content-type"] == "image/jpeg"

        h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
        def rpc(method, params, i):
            r = c.post("/s/s3cret/mcp", headers=h, json={"jsonrpc": "2.0", "id": i, "method": method, "params": params})
            assert r.status_code == 200, r.text
            return r.json()["result"]
        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}, 1)
        tools = {t["name"] for t in rpc("tools/list", {}, 2)["tools"]}
        assert {"search", "fetch", "get_frames", "list_recent", "add_note", "list_skill_candidates"} <= tools
        res = rpc("tools/call", {"name": "search", "arguments": {"query": "brugada"}}, 3)
        found = json.loads(res["content"][0]["text"])["results"]
        assert found and found[0]["title"]
        fr = rpc("tools/call", {"name": "get_frames", "arguments": {"id": str(vid), "max_frames": 3}}, 4)
        assert sum(1 for b in fr["content"] if b["type"] == "image") == 3
        doc = rpc("tools/call", {"name": "fetch", "arguments": {"id": str(vid)}}, 5)
        assert "Key points" in json.loads(doc["content"][0]["text"])["text"]
        assert c.post("/s/wrong/mcp", headers=h, json={}).status_code == 404
