"""Sealed-envelope queue. Server stores ciphertext only; desk drains via staff auth."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    envelope_json TEXT NOT NULL,
    drained_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_envelopes_pending ON envelopes(drained_at);
"""


def _db_path() -> Path:
    raw = os.environ.get("CIPHER_QUEUE_DB") or "/data/logs/cipher_queue.sqlite"
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        fallback = Path(__file__).resolve().parent.parent / "data" / "logs" / "cipher_queue.sqlite"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def put(envelope: dict[str, Any], source: str = "booker") -> dict[str, Any]:
    env_id = str(envelope.get("id") or "").strip() or uuid.uuid4().hex
    envelope["id"] = env_id
    ct_b64 = str(envelope.get("ct") or "")
    import base64

    size = len(base64.b64decode(ct_b64))
    row = {
        "id": env_id,
        "kind": str(envelope.get("kind") or ""),
        "content_hash": str(envelope.get("content_hash") or ""),
        "bytes": int(envelope.get("bytes") or size),
        "created": str(envelope.get("created") or _now()),
        "source": source,
    }
    with _connect() as conn:
        conn.execute(
            "INSERT INTO envelopes (id, kind, content_hash, bytes, created, source, envelope_json, drained_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                row["id"],
                row["kind"],
                row["content_hash"],
                row["bytes"],
                row["created"],
                row["source"],
                json.dumps(envelope, separators=(",", ":")),
            ),
        )
    return row


def list_pending(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, kind, content_hash, bytes, created, source, envelope_json"
            " FROM envelopes WHERE drained_at IS NULL ORDER BY created LIMIT ?",
            (max(1, min(int(limit), 200)),),
        ).fetchall()
    out = []
    for r in rows:
        try:
            env = json.loads(r["envelope_json"])
        except Exception:
            env = {}
        out.append(
            {
                "id": r["id"],
                "kind": r["kind"],
                "content_hash": r["content_hash"],
                "bytes": r["bytes"],
                "created": r["created"],
                "source": r["source"],
                "envelope": env,
            }
        )
    return out


def pending_count() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM envelopes WHERE drained_at IS NULL").fetchone()
    return int(row["n"] if row else 0)


def get_envelope(env_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        r = conn.execute(
            "SELECT envelope_json, drained_at FROM envelopes WHERE id = ?", (env_id,)
        ).fetchone()
    if not r:
        return None
    try:
        env = json.loads(r["envelope_json"])
    except Exception:
        return None
    env["_drained"] = bool(r["drained_at"])
    return env


def mark_drained(env_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE envelopes SET drained_at = ? WHERE id = ? AND drained_at IS NULL",
            (_now(), env_id),
        )
    return cur.rowcount > 0
