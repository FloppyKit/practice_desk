"""Dot phrases — staff text expanders. Not PHI.

Shortcut names and stock lines (meds, supplements, recs). Inserted on the
desk by typing .name then space or tab. Bodies never go in logs here.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

STORE = Path(os.environ.get("DOT_PHRASES_PATH") or "/data/logs/dot-phrases.json")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
MAX_PHRASES = 200
MAX_BODY = 4000
MAX_TITLE = 80


def _norm_trigger(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s.startswith("."):
        s = s[1:]
    s = re.sub(r"[^a-z0-9_-]+", "-", s).strip("-_")
    return s[:41]


def _clean_one(item: Any) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    tid = _norm_trigger(item.get("id") or item.get("trigger") or "")
    if not ID_RE.match(tid):
        return None
    title = str(item.get("title") or "").strip()[:MAX_TITLE]
    body = str(item.get("body") or "").replace("\r\n", "\n")
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY]
    if not body.strip():
        return None
    return {"id": tid, "trigger": tid, "title": title, "body": body}


def _read_raw() -> list[Any]:
    if not STORE.is_file():
        return []
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("phrases"), list):
        return data["phrases"]
    if isinstance(data, list):
        return data
    return []


def list_phrases() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in _read_raw():
        cleaned = _clean_one(item)
        if not cleaned or cleaned["id"] in seen:
            continue
        seen.add(cleaned["id"])
        out.append(cleaned)
        if len(out) >= MAX_PHRASES:
            break
    return out


def get_phrase(tid: str) -> dict[str, str] | None:
    want = _norm_trigger(tid)
    if not ID_RE.match(want):
        return None
    for p in list_phrases():
        if p["id"] == want:
            return p
    return None


def _write(items: list[dict[str, str]]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"phrases": items}
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STORE)


def save_phrase(trigger: str, body: str, *, title: str = "") -> dict[str, str]:
    cleaned = _clean_one({"trigger": trigger, "title": title, "body": body})
    if not cleaned:
        raise ValueError("need a shortcut name and some text")
    items = list_phrases()
    found = False
    for i, p in enumerate(items):
        if p["id"] == cleaned["id"]:
            items[i] = cleaned
            found = True
            break
    if not found:
        if len(items) >= MAX_PHRASES:
            raise ValueError(f"cap is {MAX_PHRASES} phrases")
        items.append(cleaned)
    _write(items)
    return cleaned


def delete_phrase(tid: str) -> None:
    want = _norm_trigger(tid)
    if not ID_RE.match(want):
        raise ValueError("bad shortcut")
    items = list_phrases()
    kept = [p for p in items if p["id"] != want]
    if len(kept) == len(items):
        raise ValueError("no such phrase")
    _write(kept)


def save_order(ids: list[str]) -> list[str]:
    known = {p["id"]: p for p in list_phrases()}
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in ids:
        tid = _norm_trigger(raw)
        if tid in known and tid not in seen:
            cleaned.append(known[tid])
            seen.add(tid)
    for tid in known:
        if tid not in seen:
            cleaned.append(known[tid])
    _write(cleaned)
    return [p["id"] for p in cleaned]
