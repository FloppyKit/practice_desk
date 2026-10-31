"""Pending visit drafts from edge-scribe. Bodies never logged by callers."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scribe_drafts (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    template TEXT NOT NULL DEFAULT 'blank',
    title TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    client_id TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    duration_minutes INTEGER NOT NULL DEFAULT 0,
    room TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    claimed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scribe_drafts_pending ON scribe_drafts(claimed_at, created);
"""


def _db_path() -> Path:
    raw = os.environ.get("SCRIBE_DRAFTS_DB") or "/data/logs/scribe_drafts.sqlite"
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(__file__).resolve().parent.parent / "data" / "logs" / "scribe_drafts.sqlite"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(scribe_drafts)")}
    if "event_id" not in cols:  # visit link (2026-10-30)
        conn.execute("ALTER TABLE scribe_drafts ADD COLUMN event_id TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE scribe_drafts ADD COLUMN link_status TEXT NOT NULL DEFAULT ''")
    return conn


def set_link(draft_id: str, event_id: str, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE scribe_drafts SET event_id=?, link_status=? WHERE id=?", (event_id, status, draft_id))


def rows_for_link(only_unset: bool = True) -> list[dict[str, Any]]:
    q = "SELECT id, created, ended_at, duration_minutes, client_id, email, event_id, link_status FROM scribe_drafts"
    if only_unset:
        q += " WHERE link_status=''"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(q)]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def put(
    *,
    note: str = "",
    transcript: str = "",
    title: str = "",
    template: str = "blank",
    client_id: str = "",
    email: str = "",
    name: str = "",
    duration_minutes: int = 0,
    room: str = "",
    ended_at: str = "",
) -> dict[str, Any]:
    body = (note or "").strip() or (transcript or "").strip()
    if not body:
        raise ValueError("need note or transcript")
    draft_id = uuid.uuid4().hex
    created = _now()
    tid = (template or "blank").strip().lower() or "blank"
    with _connect() as conn:
        conn.execute(
            "INSERT INTO scribe_drafts "
            "(id, created, template, title, note, transcript, client_id, email, name, "
            "duration_minutes, room, ended_at, claimed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                draft_id,
                created,
                tid,
                (title or "Visit note").strip()[:200],
                body[:200000],
                (transcript or "").strip()[:200000],
                (client_id or "").strip()[:80],
                (email or "").strip()[:200],
                (name or "").strip()[:200],
                max(0, int(duration_minutes or 0)),
                (room or "").strip()[:80],
                (ended_at or "").strip()[:64],
            ),
        )
    return {"id": draft_id, "created": created, "template": tid}


def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, created, template, title, client_id, email, name, "
            "duration_minutes, room, ended_at, "
            "length(note) AS note_len, length(transcript) AS tr_len "
            "FROM scribe_drafts WHERE claimed_at IS NULL "
            "ORDER BY created DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "created": r["created"],
            "template": r["template"],
            "title": r["title"],
            "client_id": r["client_id"],
            "email": r["email"],
            "name": r["name"],
            "duration_minutes": int(r["duration_minutes"] or 0),
            "room": r["room"],
            "ended_at": r["ended_at"],
            "has_body": bool((r["note_len"] or 0) or (r["tr_len"] or 0)),
        }
        for r in rows
    ]


def get(draft_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        r = conn.execute(
            "SELECT * FROM scribe_drafts WHERE id = ?",
            ((draft_id or "").strip(),),
        ).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "created": r["created"],
        "template": r["template"],
        "title": r["title"],
        "note": r["note"] or "",
        "transcript": r["transcript"] or "",
        "client_id": r["client_id"],
        "email": r["email"],
        "name": r["name"],
        "duration_minutes": int(r["duration_minutes"] or 0),
        "room": r["room"],
        "ended_at": r["ended_at"],
        "claimed_at": r["claimed_at"],
    }


def ack(draft_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE scribe_drafts SET claimed_at = ? "
            "WHERE id = ? AND claimed_at IS NULL",
            (_now(), (draft_id or "").strip()),
        )
        return cur.rowcount > 0
