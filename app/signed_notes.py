"""Signed after-visit summaries: one row per sign (structured fields + saved PDF paths).

Lives in the clients DB next to billings/share_links. Bodies are never logged.
Used to prefill the next note for a client and to share the patient AVS PDF.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from . import clients_db

# Fields carried to the next visit's note (patient AVS content only; never S/O/A).
PREFILL_FIELDS = ("return_visit", "medications", "supplements", "recommendations")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS signed_notes (
          id TEXT PRIMARY KEY,
          client_id TEXT NOT NULL,
          dos TEXT NOT NULL,
          template TEXT NOT NULL,
          fields_json TEXT NOT NULL,
          avs_path TEXT,
          avs_name TEXT,
          chart_path TEXT,
          signed_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_signed_notes_client ON signed_notes(client_id, signed_at)")


def record(*, client_id: str, dos: str, template: str, fields: dict[str, Any], avs_path: str,
           avs_name: str, chart_path: str = "") -> str:
    nid = uuid.uuid4().hex[:16]
    keep = {k: str(v or "")[:8000] for k, v in (fields or {}).items() if isinstance(k, str)}
    with clients_db.connect() as con:
        _ensure(con)
        con.execute(
            "INSERT INTO signed_notes (id, client_id, dos, template, fields_json, avs_path, avs_name, chart_path, signed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (nid, client_id, dos, template, json.dumps(keep), avs_path, avs_name, chart_path, _now()),
        )
        con.commit()
    return nid


def _row(r) -> dict[str, Any]:
    d = {k: r[k] for k in r.keys()}
    try:
        d["fields"] = json.loads(d.pop("fields_json") or "{}")
    except json.JSONDecodeError:
        d["fields"] = {}
    return d


def get(note_id: str) -> dict[str, Any] | None:
    with clients_db.connect() as con:
        _ensure(con)
        r = con.execute("SELECT * FROM signed_notes WHERE id = ?", (note_id,)).fetchone()
    return _row(r) if r else None


def last_for_client(client_id: str) -> dict[str, Any] | None:
    """Most recent signed note for this client that has patient AVS content."""
    if not client_id:
        return None
    with clients_db.connect() as con:
        _ensure(con)
        rows = con.execute(
            "SELECT * FROM signed_notes WHERE client_id = ? ORDER BY signed_at DESC, rowid DESC LIMIT 20",
            (client_id,),
        ).fetchall()
    for r in rows:
        d = _row(r)
        if any(str(d["fields"].get(k) or "").strip() for k in PREFILL_FIELDS):
            return d
    return None


def prefill(note: dict[str, Any] | None) -> dict[str, str]:
    if not note:
        return {}
    f = note.get("fields") or {}
    return {k: str(f.get(k) or "") for k in PREFILL_FIELDS if str(f.get(k) or "").strip()}
