"""Demo calendar backend — fake week from config/demo-week.json. No network.

Fixture entries are weekday-relative so a stranger always sees *this* week.
Writes go to a per-process in-memory overlay only; nothing is persisted.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

FIXTURE = Path(
    os.environ.get("DEMO_WEEK_PATH")
    or (Path(__file__).resolve().parent.parent / "config" / "demo-week.json")
)

_MEMO: dict[str, dict[str, Any]] = {}


def ready() -> bool:
    if (os.environ.get("CALENDAR_BACKEND") or "").strip().lower() == "demo":
        return True
    return (os.environ.get("DEMO") or "").strip().lower() in ("1", "true", "yes")


def _fixture() -> dict[str, Any]:
    try:
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _week_mondays(t0: datetime, t1: datetime, tz: ZoneInfo) -> list[datetime]:
    start = t0.astimezone(tz)
    monday = (start - timedelta(days=start.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    out = [monday]
    while monday + timedelta(days=7) < t1.astimezone(tz):
        monday += timedelta(days=7)
        out.append(monday)
    return out


def _materialize(entry: dict[str, Any], monday: datetime, tz: ZoneInfo) -> dict[str, Any] | None:
    try:
        weekday = int(entry.get("weekday"))
        sh, sm = (int(x) for x in str(entry.get("start")).split(":"))
        eh, em = (int(x) for x in str(entry.get("end")).split(":"))
    except (TypeError, ValueError):
        return None
    day = monday + timedelta(days=weekday % 7)
    start = day.replace(hour=sh, minute=sm)
    end = day.replace(hour=eh, minute=em)
    patient = entry.get("patient") or {}
    priv = {"booker": "1" if patient else "0"}
    if patient:
        priv.update(
            {
                "patient_name": str(patient.get("name") or ""),
                "patient_email": str(patient.get("email") or ""),
                "patient_phone": str(patient.get("phone") or ""),
            }
        )
    if entry.get("event_type"):
        priv["event_type"] = str(entry["event_type"])
    if entry.get("price_label"):
        priv["price_label"] = str(entry["price_label"])
    uid = f"{entry.get('id') or 'demo'}-{monday.date().isoformat()}"
    return {
        "id": uid,
        "status": "confirmed",
        "summary": entry.get("summary") or "Busy",
        "description": "Demo fixture event. Not a real person.",
        "start": {"dateTime": start.isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(tz)},
        "extendedProperties": {"private": priv},
    }


def _overlap(ev: dict[str, Any], t0: datetime, t1: datetime) -> bool:
    try:
        start = datetime.fromisoformat(str(ev["start"]["dateTime"]))
        end = datetime.fromisoformat(str(ev["end"]["dateTime"]))
    except (KeyError, TypeError, ValueError):
        return False
    return start < t1 and end > t0


def _fixture_events(t0: datetime, t1: datetime) -> list[dict[str, Any]]:
    data = _fixture()
    tz = ZoneInfo(str(data.get("timezone") or "America/Chicago"))
    out: list[dict[str, Any]] = []
    for monday in _week_mondays(t0, t1, tz):
        for entry in data.get("events") or []:
            if not isinstance(entry, dict):
                continue
            ev = _materialize(entry, monday, tz)
            if ev is not None and _overlap(ev, t0, t1):
                out.append(ev)
    return out


def _all_events(t0: datetime, t1: datetime) -> list[dict[str, Any]]:
    out = _fixture_events(t0, t1)
    out.extend(ev for ev in _MEMO.values() if _overlap(ev, t0, t1))
    out.sort(key=lambda e: str((e.get("start") or {}).get("dateTime") or ""))
    return out


def list_events(
    calendar_id: str, time_min: datetime, time_max: datetime
) -> list[dict[str, Any]]:
    return _all_events(time_min, time_max)


def list_booker_events(
    calendar_id: str, time_min: datetime, time_max: datetime
) -> list[dict[str, Any]]:
    out = []
    for ev in _all_events(time_min, time_max):
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        if priv.get("booker") == "1":
            out.append(ev)
    return out


def freebusy(
    calendar_ids: list[str], time_min: datetime, time_max: datetime
) -> list[dict[str, datetime]]:
    busy: list[tuple[datetime, datetime]] = []
    for ev in _all_events(time_min, time_max):
        try:
            start = datetime.fromisoformat(str(ev["start"]["dateTime"]))
            end = datetime.fromisoformat(str(ev["end"]["dateTime"]))
        except (KeyError, TypeError, ValueError):
            continue
        busy.append((start, end))
    busy.sort(key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    return [{"start": s, "end": e} for s, e in merged]


def get_event(calendar_id: str, event_id: str) -> dict[str, Any]:
    if event_id in _MEMO:
        return _MEMO[event_id]
    now = datetime.now(ZoneInfo("UTC"))
    for ev in _fixture_events(now - timedelta(days=14), now + timedelta(days=14)):
        if ev.get("id") == event_id:
            return ev
    raise KeyError(f"demo event not found: {event_id}")


def create_event(
    calendar_id: str,
    summary: str,
    description: str,
    start: datetime,
    end: datetime,
    timezone: str,
    attendee_email: str | None = None,
    private_props: dict[str, str] | None = None,
    uid: str | None = None,
) -> dict[str, Any]:
    uid = uid or uuid.uuid4().hex
    tz = ZoneInfo(timezone or "America/Chicago")
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    priv = {str(k): str(v) for k, v in (private_props or {}).items()}
    priv.setdefault("booker", "1")
    ev: dict[str, Any] = {
        "id": uid,
        "status": "confirmed",
        "summary": summary or "Visit",
        "description": description or "",
        "start": {"dateTime": start.isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": end.isoformat(), "timeZone": str(tz)},
        "extendedProperties": {"private": priv},
    }
    if attendee_email:
        ev["attendees"] = [{"email": attendee_email}]
    _MEMO[uid] = ev
    return ev


def patch_event(
    calendar_id: str,
    event_id: str,
    *,
    description: str | None = None,
    private_props: dict[str, str] | None = None,
) -> dict[str, Any]:
    ev = _MEMO.get(event_id)
    if ev is None:
        return {}
    if description is not None:
        ev["description"] = description
    if private_props:
        priv = ev.setdefault("extendedProperties", {}).setdefault("private", {})
        priv.update({str(k): str(v) for k, v in private_props.items()})
    return ev


def delete_event(calendar_id: str, event_id: str) -> None:
    _MEMO.pop(event_id, None)
