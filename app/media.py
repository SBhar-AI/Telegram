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
