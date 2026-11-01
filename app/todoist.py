"""Todoist task capture (unified API v1, with REST v2 fallback)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("hub.todoist")

ENDPOINTS = ["https://api.todoist.com/api/v1/tasks", "https://api.todoist.com/rest/v2/tasks"]


class TodoistError(RuntimeError):
    pass


async def add_task(content: str, labels: list[str] | None = None, description: str = "") -> dict[str, Any]:
    if not settings.todoist_token:
        raise TodoistError("TODOIST_API_TOKEN is not set.")
    body: dict[str, Any] = {"content": content[:500], "labels": labels or []}
    if description:
        body["description"] = description[:16000]
    if settings.todoist_project_id:
        body["project_id"] = settings.todoist_project_id
    headers = {"Authorization": f"Bearer {settings.todoist_token}"}
    async with httpx.AsyncClient(timeout=30) as c:
        last = ""
        for url in ENDPOINTS:
            r = await c.post(url, json=body, headers=headers)
            if r.status_code in (404, 410):
                last = f"{r.status_code} at {url}"
                continue
            if r.status_code >= 400:
                raise TodoistError(f"Todoist {r.status_code}: {r.text[:200]}")
            return r.json()
    raise TodoistError(f"Todoist endpoint not found ({last}).")
