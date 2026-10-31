"""Carry the most recent note forward into a new draft, and update a draft from a transcript.

carry(client_id)            -> {source, dos, fields}   signed desk note first, else latest imported note
latest_transcript(client_id)-> str                    newest scribe transcript for this client (48 h)
update_from_transcript(fields, transcript) -> {changed: {id: text}}   Bedrock (AWS, under BAA); click only
Visit-specific sections (date, interval history, addendum) are never carried. Nothing signs here.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import clients_db

NEVER_CARRY = {"dos", "subjective", "interval_update", "addendum", "body", "event_id", "visit"}
UPDATABLE = ("subjective", "interval_update", "medications", "supplements", "assessment", "plan",
             "recommendations", "return_visit")


def carry(client_id: str) -> dict[str, Any] | None:
    if not client_id:
        return None
    with clients_db.connect() as con:
        try:
            r = con.execute("SELECT dos, fields_json FROM signed_notes WHERE client_id=? "
                            "ORDER BY signed_at DESC, rowid DESC LIMIT 1", (client_id,)).fetchone()
        except Exception:
            r = None
        if r:
            f = json.loads(r["fields_json"] or "{}")
            fields = {k: str(v) for k, v in f.items() if k not in NEVER_CARRY and str(v or "").strip()}
            return {"source": "signed", "dos": r["dos"] or "", "fields": fields}
        try:
            r = con.execute("SELECT dos, content FROM imported_notes WHERE client_id=? "
                            "ORDER BY dos DESC, rowid DESC LIMIT 1", (client_id,)).fetchone()
        except Exception:
            r = None
    if not r:
        return None
    from .note_parse import parse_structured

    parsed = parse_structured(r["content"] or "")
    fields = {k: v for k, v in parsed.items() if k not in NEVER_CARRY and str(v or "").strip()}
    return {"source": "imported", "dos": r["dos"] or "", "fields": fields}


def latest_transcript(client_id: str, *, hours: int = 48) -> str:
    if not client_id:
        return ""
    from . import scribe_drafts

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        with scribe_drafts._connect() as con:
            r = con.execute("SELECT transcript FROM scribe_drafts WHERE client_id=? AND created>=? "
                            "AND transcript!='' ORDER BY created DESC LIMIT 1", (client_id, since)).fetchone()
    except Exception:
        return ""
    return (r[0] if r else "") or ""


_SYSTEM = (
    "You update a psychiatrist's draft visit note from today's visit transcript. Input is JSON: "
    '{"draft": {section_id: text}, "transcript": "..."}. Change ONLY what the transcript supports: '
    "interval history/subjective, symptoms, medication changes discussed, plan, recommendations, return visit. "
    "Keep everything else word for word. Do not invent facts. Reply with JSON only: "
    '{"changed": {section_id: "full new text"}} listing only sections you changed, using the given section ids.'
)


def update_from_transcript(fields: dict[str, str], transcript: str,
                           *, call: Callable[[str, str], str] | None = None) -> dict[str, str]:
    allowed = {k for k in fields} | set(UPDATABLE)
    allowed -= {"dos", "addendum", "body"}
    payload = json.dumps({"draft": {k: v for k, v in fields.items() if k in allowed},
                          "transcript": transcript[:60000]})
    if call is None:
        from .refills import _bedrock_text  # desk_agent Bedrock setup (AWS BAA)

        def call(sys_: str, txt: str) -> str:
            return _bedrock_text(sys_, txt, max_tokens=4000)
    raw = call(_SYSTEM, payload) or ""
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    ch = data.get("changed") if isinstance(data.get("changed"), dict) else {}
    return {k: str(v)[:8000] for k, v in ch.items()
            if k in allowed and k in fields and str(v).strip() != str(fields.get(k, "")).strip()}
