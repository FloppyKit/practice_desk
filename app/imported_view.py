"""Read-only views of rows imported from Carepatron (imported_* tables). Staff only.

Never books, reminds, messages or charges from these rows. Returns None when the tables are absent.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import clients_db

TZ = ZoneInfo("America/Chicago")
TABLES = ("imported_notes", "imported_appointments", "imported_client_clinical")
CLINICAL_LABELS = {
    "medications": "Medications", "medications_1": "Medications (2)", "discontinued_medications": "Discontinued medications",
    "supplements": "Supplements", "preferred_pharmacy": "Preferred pharmacy", "other_recommendations": "Other recommendations",
    "dx_code": "DX codes", "ins_info": "Insurance", "client_notes": "Client notes", "note": "Note",
    "referred_by": "Referred by", "date_first_seen": "Date first seen", "status": "Carepatron status",
}


def _con() -> sqlite3.Connection | None:
    path = clients_db.DB_PATH
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _have(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] in TABLES}


def _local(utc: str) -> tuple[str, str]:
    try:
        dt = datetime.fromisoformat(utc).astimezone(TZ)
    except (TypeError, ValueError):
        return "", ""
    return dt.isoformat(), dt.strftime("%a %b %-d, %Y %-I:%M %p CT")


def _note_title(content: str, source_file: str) -> str:
    for line in (content or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s[:120]
    stem = (source_file or "").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return stem.split(" - ")[0][:120] or "Note"


def _appt(r: sqlite3.Row, now: str) -> dict[str, Any]:
    iso, label = _local(r["start_utc"])
    try:
        lines = [{k: (x.get(k) or "") for k in ("service", "code", "price", "invoice", "dx")} for x in json.loads(r["lines_json"] or "[]")]
    except ValueError:
        lines = []
    return {"id": r["id"], "start_local": iso, "when_label": label, "status": r["status"] or "",
            "service": r["service"] or "", "duration_minutes": r["duration_min"], "lines": lines,
            "upcoming": (r["start_utc"] or "") > now, "imported": True, "read_only": True}


def for_client(client_id: str) -> dict[str, Any] | None:
    if not client_id:
        return None
    con = _con()
    if con is None:
        return None
    try:
        have = _have(con)
        if not have:
            return None
        out: dict[str, Any] = {"notes": [], "appointments": {"upcoming": [], "past": []}, "clinical": []}
        if "imported_notes" in have:
            for r in con.execute("SELECT id, dos, source_file, content FROM imported_notes WHERE client_id=? "
                                 "ORDER BY dos DESC, source_file DESC", (client_id,)):
                out["notes"].append({"id": r["id"], "dos": r["dos"] or "", "title": _note_title(r["content"], r["source_file"]),
                                     "body": r["content"] or ""})
        if "imported_appointments" in have:
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            for r in con.execute("SELECT * FROM imported_appointments WHERE client_id=? ORDER BY start_utc DESC", (client_id,)):
                a = _appt(r, now)
                out["appointments"]["upcoming" if a["upcoming"] else "past"].append(a)
            out["appointments"]["upcoming"].reverse()  # soonest first
        if "imported_client_clinical" in have:
            order = list(CLINICAL_LABELS)
            rows = con.execute("SELECT field, value FROM imported_client_clinical WHERE client_id=?", (client_id,)).fetchall()
            for r in sorted(rows, key=lambda r: order.index(r["field"]) if r["field"] in order else 99):
                out["clinical"].append({"field": r["field"], "label": CLINICAL_LABELS.get(r["field"], r["field"]), "value": r["value"]})
        if not (out["notes"] or out["clinical"] or out["appointments"]["upcoming"] or out["appointments"]["past"]):
            return None
        return out
    finally:
        con.close()


def week(start: str, days: int = 7) -> list[dict[str, Any]] | None:
    """Imported appointments (with client names) in [start, start+days) Central. None if table absent."""
    con = _con()
    if con is None:
        return None
    try:
        if "imported_appointments" not in _have(con):
            return None
        try:
            d0 = datetime.strptime(start[:10], "%Y-%m-%d").replace(tzinfo=TZ)
        except ValueError:
            d0 = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        d1 = d0 + timedelta(days=max(1, min(int(days or 7), 31)))
        lo, hi = d0.astimezone(timezone.utc).isoformat(), d1.astimezone(timezone.utc).isoformat()
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        rows = con.execute(
            "SELECT a.*, c.display_name AS client_name FROM imported_appointments a LEFT JOIN clients c ON c.id=a.client_id "
            "WHERE a.start_utc >= ? AND a.start_utc < ? ORDER BY a.start_utc", (lo, hi)).fetchall()
        return [dict(_appt(r, now), client_id=r["client_id"] or "", client_name=r["client_name"] or "") for r in rows]
    finally:
        con.close()
