"""Model calls: transcription (OpenAI Whisper or local faster-whisper) and Claude analysis."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("hub.ai")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_client = httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0))
_local_model = None

CATEGORIES = ["medical-education", "ai-technique", "content-creation", "productivity", "research", "business", "other"]

ANALYZE_SYSTEM = """You are the analysis engine of Sahil's personal knowledge hub. Sahil is a physician (internal medicine, \
headed to cardiology/electrophysiology) who also builds medical-education simulators, makes short-form EKG/cardiology \
teaching videos, and develops reusable AI skills and prompts. He sends you short videos, images and links he found \
useful. You receive sampled frames (in time order), an audio transcript when available, the post caption, and his note.

Extract what is reusable. Be concrete and faithful to the content: never invent claims that are not shown or said. \
If the content makes medical claims that are wrong or outdated, say so in `accuracy_flags`. If anything visible looks \
like real patient-identifying information (names, MRNs, dates of birth, faces in a clinical setting), set `phi_flag` true.

Record your analysis with the save_analysis tool, using these fields:
- title: short descriptive title (max 80 chars)
- summary: 2-4 sentence summary of what the content shows/teaches
- key_points: list of the specific facts, steps or claims (max 10)
- techniques: list of reusable techniques worth copying (teaching method, editing/visual style, hook structure, prompt pattern, workflow). Each item one sentence.
- tags: 3-8 lowercase tags
- category: one of """ + ", ".join(CATEGORIES) + """
- skill_candidate: true if this could become or improve a reusable AI skill/prompt/workflow
- skill_idea: if skill_candidate, one or two sentences describing that skill; else ""
- accuracy_flags: list of concerns about accuracy (empty if none)
- phi_flag: boolean
"""

ASK_SYSTEM = """You answer Sahil's questions using ONLY the saved items from his knowledge hub provided below. \
Cite items inline as [#id]. If the items don't answer the question, say so plainly and suggest what to save or search. \
Be concise: this is read on a phone in Telegram. Plain text with short paragraphs or simple dashes; no markdown tables."""


class AIError(RuntimeError):
    pass


# ---------------- transcription ----------------
async def transcribe(audio: Path) -> tuple[str, str]:
    """Return (transcript, backend_used). Empty transcript if unavailable."""
    backend = settings.transcriber
    if backend == "none":
        return "", "none"
    if backend == "openai":
        try:
            return await _transcribe_openai(audio), "openai"
        except Exception as e:  # fall back to local if OpenAI fails
            log.warning("openai transcription failed, trying local: %s", e)
    try:
        return await asyncio.to_thread(_transcribe_local, audio), "local"
    except Exception as e:
        log.warning("local transcription failed: %s", e)
        return "", f"failed: {e}"[:120]


async def _transcribe_openai(audio: Path) -> str:
    with audio.open("rb") as f:
        r = await _client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {settings.openai_key}"},
            data={"model": settings.openai_transcribe_model, "response_format": "text"},
            files={"file": (audio.name, f, "audio/mpeg")},
        )
    if r.status_code >= 400:
        raise AIError(f"OpenAI transcription {r.status_code}: {r.text[:200]}")
    return r.text.strip()


def _transcribe_local(audio: Path) -> str:
    global _local_model
    from faster_whisper import WhisperModel  # optional heavy dependency

    if _local_model is None:
        _local_model = WhisperModel(
            settings.local_whisper_model, device="cpu", compute_type="int8",
            download_root=str(settings.data_dir / "models"),
        )
    import subprocess

    import numpy as np

    pcm = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(audio), "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
        capture_output=True, check=True, timeout=600,
    ).stdout
    samples = np.frombuffer(pcm, dtype=np.float32)
    if samples.size < 1600:
        return ""
    segments, _info = _local_model.transcribe(samples, vad_filter=True, beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


# ---------------- Claude ----------------
ANALYSIS_TOOL = {
    "name": "save_analysis",
    "description": "Save the structured analysis of the content.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "key_points": {"type": "array", "items": {"type": "string"}},
            "techniques": {"type": "array", "items": {"type": "string"}},
            "tags": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string", "enum": CATEGORIES},
            "skill_candidate": {"type": "boolean"},
            "skill_idea": {"type": "string"},
            "accuracy_flags": {"type": "array", "items": {"type": "string"}},
            "phi_flag": {"type": "boolean"},
        },
        "required": ["title", "summary", "key_points", "techniques", "tags", "category",
                     "skill_candidate", "skill_idea", "accuracy_flags", "phi_flag"],
    },
}


async def claude(system: str, content: list[dict] | str, max_tokens: int = 2000, tool: dict | None = None) -> str:
    """Call Claude. With `tool`, force that tool and return its input as a JSON string."""
    if not settings.anthropic_key:
        raise AIError("ANTHROPIC_API_KEY is not set.")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    body = {
        "model": settings.claude_model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    if tool:
        body["tools"] = [tool]
        body["tool_choice"] = {"type": "tool", "name": tool["name"]}
    headers = {
        "x-api-key": settings.anthropic_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    for attempt in range(3):
        r = await _client.post(ANTHROPIC_URL, headers=headers, json=body)
        if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
            await asyncio.sleep(4 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise AIError(f"Claude API {r.status_code}: {r.text[:300]}")
        data = r.json()
        if tool:
            for b in data.get("content", []):
                if b.get("type") == "tool_use":
                    return json.dumps(b.get("input", {}))
            log.warning("no tool_use block; stop_reason=%s", data.get("stop_reason"))
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    raise AIError("Claude API unavailable after retries.")


def parse_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        log.warning("non-JSON model output: %r", text[:300])
        raise AIError("Model did not return JSON.")
    return json.loads(m.group(0))


def _img_block(p: Path) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(p.read_bytes()).decode()},
    }


async def analyze(
    *, kind: str, frames: list[Path], transcript: str, caption: str, title: str, author: str,
    user_note: str, url: str, duration: float | None,
) -> dict[str, Any]:
    content: list[dict] = []
    for i, f in enumerate(frames):
        if kind == "video" and duration:
            t = duration * (i + 0.5) / len(frames)
            content.append({"type": "text", "text": f"Frame {i + 1} at {t:.1f}s"})
        content.append(_img_block(f))
    ctx = [
        f"Content type: {kind}",
        f"Source URL: {url or '(sent directly)'}",
        f"Original title: {title or '(none)'}",
        f"Author: {author or '(unknown)'}",
        f"Duration: {f'{duration:.0f}s' if duration else 'n/a'}",
        f"Caption:\n{caption[:3000] or '(none)'}",
        f"Sahil's note: {user_note or '(none)'}",
        f"Transcript:\n{transcript[:20000] or '(no audio / no transcript)'}",
    ]
    content.append({"type": "text", "text": "\n\n".join(ctx)})
    raw = await claude(ANALYZE_SYSTEM, content, max_tokens=2000, tool=ANALYSIS_TOOL)
    data = parse_json(raw)
    cat = str(data.get("category", "other")).lower()
    data["category"] = cat if cat in CATEGORIES else "other"
    for k in ("key_points", "techniques", "tags", "accuracy_flags"):
        v = data.get(k) or []
        data[k] = [str(x) for x in v] if isinstance(v, list) else [str(v)]
    data["tags"] = [t.lower().strip().lstrip("#") for t in data["tags"]][:8]
    return data


async def analyze_text(text: str, url: str = "") -> dict[str, Any]:
    """Light-weight tagging for notes and links that couldn't be downloaded."""
    raw = await claude(
        ANALYZE_SYSTEM,
        f"Content type: text note\nSource URL: {url or '(none)'}\nText:\n{text[:8000]}",
        max_tokens=800, tool=ANALYSIS_TOOL,
    )
    return parse_json(raw)


async def answer(question: str, items: list[dict]) -> str:
    blocks = []
    for it in items:
        blocks.append(
            f"[#{it['id']}] {it.get('title') or '(untitled)'} — {it.get('kind')} — {it.get('created_at', '')[:10]}\n"
            f"URL: {it.get('source_url') or '-'}\n"
            f"Summary: {it.get('summary') or ''}\n"
            f"Key points: {'; '.join(it.get('key_points') or [])}\n"
            f"Techniques: {'; '.join(it.get('techniques') or [])}\n"
            f"Note: {it.get('user_note') or ''}\n"
            f"Transcript excerpt: {(it.get('transcript') or '')[:1500]}"
        )
    prompt = f"Question: {question}\n\nSaved items:\n\n" + "\n\n---\n\n".join(blocks)
    return await claude(ASK_SYSTEM, prompt, max_tokens=1200)
