"""SQLite archive with FTS5 full-text search. One file on the Railway volume."""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,              -- video | image | link | note | voice
    status TEXT NOT NULL DEFAULT 'done',  -- processing | done | error
    source_url TEXT,
    title TEXT,
    author TEXT,
    caption TEXT,
    user_note TEXT,
    summary TEXT,
    transcript TEXT,
    key_points TEXT,                 -- JSON list
    techniques TEXT,                 -- JSON list
    tags TEXT,                       -- JSON list
    category TEXT,
    skill_candidate INTEGER DEFAULT 0,
    skill_idea TEXT,
    duration_s REAL,
    media_dir TEXT,
    error TEXT,
    meta TEXT,                       -- JSON
    resources TEXT,                  -- JSON list of resolved resources
    skill_draft TEXT                 -- JSON skill draft
);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""

FTS_COLS = "title, author, caption, user_note, summary, transcript, key_points, techniques, tags, category, resources"

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, author, caption, user_note, summary, transcript, key_points, techniques, tags, category, resources,
    content='items', content_rowid='id', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, title, author, caption, user_note, summary, transcript, key_points, techniques, tags, category, resources)
  VALUES (new.id, new.title, new.author, new.caption, new.user_note, new.summary, new.transcript, new.key_points, new.techniques, new.tags, new.category, new.resources);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, author, caption, user_note, summary, transcript, key_points, techniques, tags, category, resources)
  VALUES ('delete', old.id, old.title, old.author, old.caption, old.user_note, old.summary, old.transcript, old.key_points, old.techniques, old.tags, old.category, old.resources);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, title, author, caption, user_note, summary, transcript, key_points, techniques, tags, category, resources)
  VALUES ('delete', old.id, old.title, old.author, old.caption, old.user_note, old.summary, old.transcript, old.key_points, old.techniques, old.tags, old.category, old.resources);
  INSERT INTO items_fts(rowid, title, author, caption, user_note, summary, transcript, key_points, techniques, tags, category, resources)
  VALUES (new.id, new.title, new.author, new.caption, new.user_note, new.summary, new.transcript, new.key_points, new.techniques, new.tags, new.category, new.resources);
END;
"""

JSON_FIELDS = ("key_points", "techniques", "tags", "meta", "resources", "skill_draft")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(items)")}
        for col in ("resources", "skill_draft"):
            if col not in cols:
                self._conn.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT")
        fts_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(items_fts)")}
        if fts_cols and "resources" not in fts_cols:
            self._conn.executescript(
                "DROP TRIGGER IF EXISTS items_ai; DROP TRIGGER IF EXISTS items_ad; DROP TRIGGER IF EXISTS items_au;"
                "DROP TABLE IF EXISTS items_fts;"
            )
        self._conn.executescript(FTS_SCHEMA)
        if fts_cols and "resources" not in fts_cols:
            self._conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")

    # ---------- helpers ----------
    def _row(self, r: sqlite3.Row | None) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        for f in JSON_FIELDS:
            if d.get(f):
                try:
                    d[f] = json.loads(d[f])
                except (TypeError, ValueError):
                    pass
            else:
                d[f] = {} if f in ("meta", "skill_draft") else []
        d["skill_candidate"] = bool(d.get("skill_candidate"))
        return d

    @staticmethod
    def _encode(fields: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for k, v in fields.items():
            if k in JSON_FIELDS and not isinstance(v, str) and v is not None:
                v = json.dumps(v, ensure_ascii=False)
            if k == "skill_candidate":
                v = 1 if v else 0
            out[k] = v
        return out

    # ---------- items ----------
    def create(self, kind: str, **fields: Any) -> int:
        fields = self._encode({"kind": kind, "created_at": now_iso(), **fields})
        cols = ",".join(fields)
        qs = ",".join("?" for _ in fields)
        with self._lock:
            cur = self._conn.execute(f"INSERT INTO items ({cols}) VALUES ({qs})", list(fields.values()))
            self._conn.commit()
            return int(cur.lastrowid)

    def update(self, item_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields = self._encode(fields)
        sets = ",".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE items SET {sets} WHERE id=?", [*fields.values(), item_id])
            self._conn.commit()

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        return self._row(r)

    def delete(self, item_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM items WHERE id=?", (item_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def recent(self, limit: int = 10, category: str | None = None, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM items WHERE 1=1", []
        if category:
            q += " AND category=?"
            args.append(category)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def fts_query(text: str) -> str:
        terms = [t for t in re.findall(r"[\w\-']+", text.lower()) if len(t) > 1]
        stop = {"the", "and", "for", "what", "did", "about", "with", "how", "that", "this", "save", "saved",
                "any", "are", "was", "have", "from", "you", "can", "show", "me", "my", "is", "of", "to", "in", "on"}
        terms = [t.replace("'", "") for t in terms if t not in stop] or terms
        return " OR ".join(f'"{t}"*' if len(t) > 3 else f'"{t}"' for t in terms[:12])

    def search(self, text: str, limit: int = 8) -> list[dict]:
        q = self.fts_query(text)
        if not q:
            return []
        sql = (
            "SELECT items.*, snippet(items_fts, -1, '[', ']', '…', 18) AS snippet, bm25(items_fts) AS score "
            "FROM items_fts JOIN items ON items.id = items_fts.rowid "
            "WHERE items_fts MATCH ? ORDER BY score LIMIT ?"
        )
        with self._lock:
            try:
                rows = self._conn.execute(sql, (q, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return [self._row(r) for r in rows]

    def mark_interrupted(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE items SET status='error', error='Interrupted by a restart; please resend.' WHERE status='processing'"
            )
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT kind, COUNT(*) c FROM items GROUP BY kind").fetchall()
        return {r["kind"]: r["c"] for r in rows}

    # ---------- key/value ----------
    def kv_get(self, k: str) -> str | None:
        with self._lock:
            r = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO kv(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self._conn.commit()
