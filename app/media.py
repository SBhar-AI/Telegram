"""Downloading and taking videos apart: yt-dlp for links, ffmpeg for frames and audio."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .config import settings

log = logging.getLogger("hub.media")


class MediaError(RuntimeError):
    pass


async def run(*args: str, timeout: int = 600) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise MediaError(f"{args[0]} timed out")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _cookie_file(work: Path) -> str | None:
    if not settings.ig_cookies:
        return None
    p = work / "cookies.txt"
    text = settings.ig_cookies.replace("\\n", "\n")
    if not text.startswith("# "):
        text = "# Netscape HTTP Cookie File\n" + text
    p.write_text(text)
    return str(p)


def _ytdlp_download(url: str, work: Path) -> dict[str, Any]:
    import yt_dlp  # imported lazily so tests don't need network

    opts: dict[str, Any] = {
        "outtmpl": str(work / "source.%(ext)s"),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "max_filesize": settings.max_video_mb * 1024 * 1024,
        "socket_timeout": 30,
        "retries": 2,
    }
    cookies = _cookie_file(work)
    if cookies:
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if info and info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    files = sorted(p for p in work.glob("source.*") if p.suffix not in {".part", ".ytdl", ".txt"})
    if not files:
        raise MediaError("Download produced no media file.")
    return {
        "path": str(files[0]),
        "title": info.get("title") or "",
        "caption": info.get("description") or "",
        "author": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or "",
        "upload_date": info.get("upload_date") or "",
    }


async def download_url(url: str, work: Path) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_ytdlp_download, url, work)
    except MediaError:
        raise
    except Exception as e:  # yt-dlp raises many types
        msg = str(e)
        if "instagram" in url.lower() and ("login" in msg.lower() or "cookies" in msg.lower() or "rate" in msg.lower()):
            msg += " — Instagram wants a logged-in session. Add IG_COOKIES (see README)."
        raise MediaError(msg[:500])


async def probe(path: Path) -> dict[str, Any]:
    code, out, err = await run(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path), timeout=60
    )
    if code != 0:
        raise MediaError(f"ffprobe failed: {err[:300]}")
    data = json.loads(out or "{}")
    streams = data.get("streams", [])
    dur = float(data.get("format", {}).get("duration") or 0) or None
    return {
        "duration": dur,
        "has_video": any(s.get("codec_type") == "video" for s in streams),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def frame_count(duration: float | None) -> int:
    if not duration:
        return 6
    return int(min(16, max(4, round(duration / 5))))


async def extract_frames(path: Path, out_dir: Path, duration: float | None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = frame_count(duration)
    frames: list[Path] = []
    if not duration:
        duration = 30.0
    for i in range(n):
        t = duration * (i + 0.5) / n
        dest = out_dir / f"frame_{i:02d}.jpg"
        code, _, _ = await run(
            "ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
            "-frames:v", "1", "-vf", "scale='min(768,iw)':-2", "-q:v", "4", str(dest), timeout=60,
        )
        if code == 0 and dest.exists() and dest.stat().st_size > 0:
            frames.append(dest)
    return frames


async def extract_audio(path: Path, dest: Path) -> Path | None:
    code, _, err = await run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "32k", str(dest), timeout=900,
    )
    if code != 0 or not dest.exists() or dest.stat().st_size < 1000:
        log.info("no audio extracted: %s", err[:200])
        return None
    return dest


async def image_to_jpeg(path: Path, dest: Path) -> Path:
    code, _, err = await run(
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-vf", "scale='min(1280,iw)':-2", str(dest), timeout=60
    )
    if code != 0:
        raise MediaError(f"Could not read image: {err[:200]}")
    return dest


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------- Instagram photo / carousel posts
# yt-dlp only handles video. For a photo or carousel post it raises "There is no video in this post" or
# "No video formats found!", and the old fallback then read the post's web page as text - which Instagram serves as a
# login wall, so the images (often slides listing the actual repos/tools) were never seen. This path fetches the
# images themselves.

import html as _html
import re as _re

_IG_POST = _re.compile(r"instagram\.com/(?:[A-Za-z0-9_.]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", _re.I)
_SC_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/130.0 Safari/537.36")
IG_APP_ID = "936619743392459"  # Instagram web app id, sent by instagram.com itself
MAX_SLIDES = 12
MAX_IMAGE_BYTES = 15 * 1024 * 1024


def instagram_shortcode(url: str) -> str | None:
    m = _IG_POST.search(url or "")
    return m.group(1) if m else None


def shortcode_to_media_id(code: str) -> int:
    n = 0
    for ch in code[:11]:
        n = n * 64 + _SC_ALPHABET.index(ch)
    return n


def _cookie_header() -> str:
    """IG_COOKIES (Netscape cookies.txt) -> 'name=value; ...' for instagram.com."""
    pairs = []
    for line in (settings.ig_cookies or "").replace("\\n", "\n").splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 7 and "instagram.com" in parts[0]:
            pairs.append(f"{parts[5]}={parts[6]}")
    return "; ".join(pairs)


def _best_candidate(node: dict) -> str | None:
    cands = ((node.get("image_versions2") or {}).get("candidates")) or []
    if cands:
        return max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0)).get("url")
    return node.get("display_url")


def _parse_api(data: dict) -> dict[str, Any] | None:
    items = data.get("items") or []
    if not items:
        return None
    it = items[0]
    nodes = it.get("carousel_media") or [it]
    urls, videos = [], 0
    for n in nodes:
        if n.get("video_versions"):
            videos += 1
        u = _best_candidate(n)  # for a video slide this is its cover image
        if u:
            urls.append(u)
    return {"image_urls": urls, "caption": ((it.get("caption") or {}).get("text")) or "",
            "author": ((it.get("user") or {}).get("username")) or "", "video_slides": videos, "source": "api"}


def _parse_embed(page: str) -> dict[str, Any] | None:
    urls: list[str] = []
    caption, author = "", ""
    m = _re.search(r'"contextJSON":("(?:[^"\\]|\\.)*")', page)
    if m:
        try:
            ctx = json.loads(json.loads(m.group(1)))
            sm = ((ctx.get("gql_data") or {}).get("shortcode_media")) or {}
            edges = ((sm.get("edge_sidecar_to_children") or {}).get("edges")) or []
            urls = [e["node"]["display_url"] for e in edges if (e.get("node") or {}).get("display_url")]
            if not urls and sm.get("display_url"):
                urls = [sm["display_url"]]
            caps = ((sm.get("edge_media_to_caption") or {}).get("edges")) or []
            caption = caps[0]["node"]["text"] if caps else ""
            author = (sm.get("owner") or {}).get("username") or ""
        except Exception as e:
            log.info("embed contextJSON unreadable: %s", e)
    if not urls:
        urls = [_html.unescape(u) for u in _re.findall(r'class="EmbeddedMediaImage"[^>]*?src="([^"]+)"', page)]
        if not urls:
            urls = [_html.unescape(u) for u in _re.findall(r'<img[^>]+class="EmbeddedMediaImage"[^>]*>', page)
                    for u in _re.findall(r'src="([^"]+)"', u)]
    if not caption:
        c = _re.search(r'<div class="Caption">(.*?)</div>', page, _re.S)
        if c:
            caption = _html.unescape(_re.sub(r"<[^>]+>", " ", c.group(1)))
            caption = _re.sub(r"\s+", " ", caption).strip()
    if not author:
        a = _re.search(r'class="UsernameText">([^<]+)<', page)
        author = a.group(1).strip() if a else ""
    return {"image_urls": urls, "caption": caption, "author": author, "video_slides": 0, "source": "embed"} if urls else None


def _parse_og(page: str) -> dict[str, Any] | None:
    img = _re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
    if not img:
        return None
    desc = _re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', page)
    return {"image_urls": [_html.unescape(img.group(1))], "caption": _html.unescape(desc.group(1)) if desc else "",
            "author": "", "video_slides": 0, "source": "preview (first image only)"}


async def instagram_photos(url: str, work: Path, client: Any = None) -> dict[str, Any]:
    """Download the images of an Instagram photo/carousel post. Tries, in order: Instagram's own media API (only with
    IG_COOKIES; gets every slide), the public embed page (usually every slide, no login), and the link-preview tags
    (first image only). Raises MediaError if none work."""
    import httpx

    code = instagram_shortcode(url)
    if not code:
        raise MediaError("Not an Instagram post link.")
    own = client is None
    client = client or httpx.AsyncClient(timeout=25, follow_redirects=True)
    tried: list[str] = []
    found: dict[str, Any] | None = None
    try:
        cookie = _cookie_header()
        if cookie:
            try:
                r = await client.get(f"https://i.instagram.com/api/v1/media/{shortcode_to_media_id(code)}/info/",
                                     headers={"User-Agent": _BROWSER_UA, "X-IG-App-ID": IG_APP_ID, "Cookie": cookie})
                found = _parse_api(r.json()) if r.status_code == 200 else None
                tried.append(f"api {r.status_code}")
            except Exception as e:
                tried.append(f"api {type(e).__name__}")
        if not found:
            try:
                r = await client.get(f"https://www.instagram.com/p/{code}/embed/captioned/",
                                     headers={"User-Agent": _BROWSER_UA})
                found = _parse_embed(r.text) if r.status_code == 200 else None
                tried.append(f"embed {r.status_code}")
            except Exception as e:
                tried.append(f"embed {type(e).__name__}")
        if not found:
            try:
                r = await client.get(f"https://www.instagram.com/p/{code}/",
                                     headers={"User-Agent": "facebookexternalhit/1.1"})
                found = _parse_og(r.text) if r.status_code == 200 else None
                tried.append(f"preview {r.status_code}")
            except Exception as e:
                tried.append(f"preview {type(e).__name__}")
        if not found:
            raise MediaError("Couldn't get this post's images (" + ", ".join(tried) + ").")

        paths: list[Path] = []
        seen: set[str] = set()
        for i, u in enumerate(found["image_urls"][:MAX_SLIDES]):
            if u in seen:
                continue
            seen.add(u)
            try:
                r = await client.get(u, headers={"User-Agent": _BROWSER_UA})
            except Exception as e:
                log.info("slide %d download failed: %s", i, e)
                continue
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/") \
                    or len(r.content) > MAX_IMAGE_BYTES:
                continue
            raw = work / f"slide_{i:02d}.img"
            raw.write_bytes(r.content)
            paths.append(raw)
        if not paths:
            raise MediaError("Found the post's images but couldn't download them (" + ", ".join(tried) + ").")
        return {"paths": paths, "caption": found["caption"], "author": found["author"],
                "total_slides": len(found["image_urls"]), "video_slides": found["video_slides"],
                "source": found["source"], "webpage_url": f"https://www.instagram.com/p/{code}/"}
    finally:
        if own:
            await client.aclose()
