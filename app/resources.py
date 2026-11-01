"""Resource enrichment: GitHub repo metadata, README and Claude skill (SKILL.md) discovery; plain web pages."""
from __future__ import annotations

import base64
import html as htmlmod
import logging
import re
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("hub.resources")

GH_RE = re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", re.I)
MAX_SKILLS = 6
MAX_SKILL_BYTES = 20000


def parse_github(url: str) -> tuple[str, str] | None:
    m = GH_RE.match(url or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    repo = re.sub(r"\.git$", "", repo)
    if owner.lower() in {"orgs", "topics", "search", "marketplace", "features", "collections", "sponsors"}:
        return None
    return owner, repo


def _gh_headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "telegram-knowledge-hub"}
    if settings.github_token:
        h["Authorization"] = f"Bearer {settings.github_token}"
    return h


async def github_info(url: str) -> dict[str, Any] | None:
    """Repo metadata, README excerpt and any SKILL.md files (contents included, size-capped)."""
    parsed = parse_github(url)
    if not parsed:
        return None
    owner, repo = parsed
    api = f"https://api.github.com/repos/{owner}/{repo}"
    async with httpx.AsyncClient(timeout=30, headers=_gh_headers(), follow_redirects=True) as c:
        r = await c.get(api)
        if r.status_code != 200:
            log.info("github %s/%s -> %s", owner, repo, r.status_code)
            return {"url": f"https://github.com/{owner}/{repo}", "error": f"GitHub returned {r.status_code}"}
        meta = r.json()
        info: dict[str, Any] = {
            "url": meta.get("html_url"),
            "full_name": meta.get("full_name"),
            "description": meta.get("description") or "",
            "stars": meta.get("stargazers_count"),
            "pushed_at": (meta.get("pushed_at") or "")[:10],
            "license": (meta.get("license") or {}).get("spdx_id"),
            "topics": meta.get("topics") or [],
            "archived": meta.get("archived"),
        }
        branch = meta.get("default_branch") or "main"
        rr = await c.get(f"{api}/readme")
        if rr.status_code == 200:
            try:
                info["readme"] = base64.b64decode(rr.json().get("content", "")).decode("utf-8", "replace")[:6000]
            except Exception:
                pass
        tr = await c.get(f"{api}/git/trees/{branch}", params={"recursive": "1"})
        skills: list[dict[str, Any]] = []
        if tr.status_code == 200:
            paths = [t["path"] for t in tr.json().get("tree", []) if t.get("type") == "blob"]
            skill_paths = [p for p in paths if p.rsplit("/", 1)[-1].lower() == "skill.md"]
            info["skill_count"] = len(skill_paths)
            info["skill_paths"] = skill_paths[:50]
            for p in skill_paths[:MAX_SKILLS]:
                raw = await c.get(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{p}")
                if raw.status_code == 200:
                    skills.append({"path": p, "content": raw.text[:MAX_SKILL_BYTES]})
        info["skills"] = skills
        return info


def _strip_html(text: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        title = htmlmod.unescape(m.group(1)).strip()
    text = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = htmlmod.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return title, text.strip()


async def fetch_page(url: str) -> dict[str, str] | None:
    """Fetch an ordinary web page as plain text (best effort)."""
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 (knowledge-hub)"}) as c:
            r = await c.get(url)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "") and "text" not in r.headers.get("content-type", ""):
            return None
        title, text = _strip_html(r.text)
        return {"title": title, "text": text[:15000], "url": str(r.url)}
    except Exception as e:
        log.info("fetch_page failed %s: %s", url, e)
        return None


def describe_github(info: dict[str, Any]) -> str:
    """Compact text for prompts and the archive."""
    if not info or info.get("error"):
        return ""
    parts = [
        f"Repo: {info.get('full_name')} — {info.get('description')}",
        f"Stars: {info.get('stars')} · last push {info.get('pushed_at')} · license {info.get('license')}"
        + (" · ARCHIVED" if info.get("archived") else ""),
    ]
    if info.get("skill_count"):
        parts.append(f"Contains {info['skill_count']} SKILL.md file(s): " + ", ".join(info.get("skill_paths", [])[:15]))
    if info.get("readme"):
        parts.append("README excerpt:\n" + info["readme"][:2500])
    return "\n".join(parts)


# ------------------------------------------------------------------ exact-match resolution
SLUG_RE = re.compile(r"(?<![\w.@/-])(?:https?://)?(?:www\.)?(?:github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})(?![\w/])")
NOT_SLUGS = {"and/or", "w/o", "i/o", "n/a", "24/7", "km/h", "on/off", "yes/no", "input/output", "client/server", "read/write"}
GENERIC = {"skill", "skills", "claude", "code", "the", "open", "source", "free", "tool", "tools", "app", "ai", "agent",
           "agents", "github", "repo", "plugin", "plugins", "prompt", "prompts", "official", "and", "for", "with", "pack",
           "template", "templates", "mcp", "server", "best", "top", "new"}


def find_slugs(text: str) -> list[tuple[str, str]]:
    out = []
    for m in SLUG_RE.finditer(text or ""):
        owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2).rstrip(".,;:)"))
        if f"{owner}/{repo}".lower() in NOT_SLUGS or owner.isdigit() or repo.isdigit() or len(repo) < 2:
            continue
        if repo.lower().endswith((".png", ".jpg", ".html", ".md")):
            continue
        if (owner, repo) not in out:
            out.append((owner, repo))
    return out


def name_tokens(name: str) -> set[str]:
    toks = {t for t in re.findall(r"[a-z0-9]+", (name or "").lower()) if len(t) >= 3}
    return toks - GENERIC


async def verify_repo(owner: str, repo: str) -> dict[str, Any] | None:
    async with httpx.AsyncClient(timeout=20, headers=_gh_headers(), follow_redirects=True) as c:
        r = await c.get(f"https://api.github.com/repos/{owner}/{repo}")
    if r.status_code != 200:
        return None
    m = r.json()
    return {"url": m.get("html_url"), "full_name": m.get("full_name"), "description": m.get("description") or ""}


def matches(resource: dict, url: str, title: str = "", extra: str = "") -> bool:
    """Does this URL/page plausibly refer to this specific resource?"""
    hay = f"{url} {title} {extra}".lower()
    for ident in resource.get("identifiers") or []:
        ident = ident.lower().strip()
        if len(ident) >= 4 and ident.replace("https://", "").replace("http://", "") in hay:
            return True
    toks = name_tokens(resource.get("name", ""))
    if not toks:  # name is all generic words ("Skills") -> can't verify by name
        return False
    return any(t in hay for t in toks)


async def direct_resolve(resources: list[dict], item_text: str) -> tuple[list[dict], list[dict]]:
    """Resolve resources whose GitHub slug is visible (on screen, caption, transcript) without web search.
    Returns (resolved, unresolved)."""
    all_slugs = find_slugs(item_text)
    checked: dict[str, dict | None] = {}
    resolved, unresolved = [], []
    for r in resources:
        own = find_slugs(" ".join([r.get("url") or "", r.get("name") or "", *(r.get("identifiers") or []), r.get("context") or ""]))
        toks = name_tokens(r.get("name", ""))
        candidates = own + [s for s in all_slugs if s not in own and toks and any(
            t in s[1].lower() or s[1].lower() in t for t in toks)]
        hit = None
        for owner, repo in candidates[:6]:
            key = f"{owner}/{repo}".lower()
            if key not in checked and len(checked) < 20:
                checked[key] = await verify_repo(owner, repo)
            if checked.get(key):
                hit = checked[key]
                break
        if hit:
            resolved.append({**r, "found": True, "url": hit["url"], "confidence": "high",
                             "description": r.get("context") or hit["description"],
                             "verified": f"exact repo {hit['full_name']} read from the post and confirmed on GitHub"})
        else:
            unresolved.append(r)
    return resolved, unresolved


async def validate_resolved(resource: dict) -> dict:
    """Reject generic/non-matching search results; never present them as the answer."""
    url = resource.get("url") or ""
    if not resource.get("found") or not url:
        return resource
    gh = parse_github(url)
    if gh:
        meta = await verify_repo(*gh)
        if not meta:
            return {**resource, "found": False, "url": "", "confidence": "low",
                    "note": f"Search suggested {url}, but that repo doesn't exist.", "lead": url}
        if matches(resource, meta["url"], meta["full_name"], meta["description"]):
            return {**resource, "url": meta["url"], "verified": "repo confirmed on GitHub and name matches"}
        return {**resource, "found": False, "url": "", "confidence": "low", "lead": meta["url"],
                "note": f"Search returned {meta['full_name']}, which doesn't match “{resource.get('name')}”. Shown as a lead only."}
    page = await fetch_page(url)
    title = (page or {}).get("title", "")
    if matches(resource, url, title):
        return {**resource, "verified": "page name matches"}
    return {**resource, "found": False, "url": "", "confidence": "low", "lead": url,
            "note": f"Search returned a page (“{title[:80] or url}”) that doesn't name “{resource.get('name')}”, likely generic. Lead only."}
