"""ICS feeds from the local calendar. Public feed is busy-only (no titles)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
PAST_DAYS = int(os.environ.get("ICS_PAST_DAYS", "14"))
FUTURE_DAYS = int(os.environ.get("ICS_FUTURE_DAYS", "180"))


def _as_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def build_ics(*, busy_only: bool, calendar_name: str) -> bytes:
    from icalendar import Calendar, Event, vText

    from .cal import list_events
    from .config import DEFAULT_CALENDAR

    now = datetime.now(TZ)
    start = now - timedelta(days=PAST_DAYS)
    end = now + timedelta(days=FUTURE_DAYS)
    items = list_events(DEFAULT_CALENDAR, start, end)

    cal = Calendar()
    cal.add("prodid", "-//Psych Arts Booker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", vText(calendar_name))
    cal.add("x-wr-timezone", "America/Chicago")

    for ev in items:
        uid = str(ev.get("id") or "").strip()
        if not uid:
            continue
        s = _as_dt((ev.get("start") or {}).get("dateTime"))
        e = _as_dt((ev.get("end") or {}).get("dateTime"))
        if not s or not e:
            continue
        ve = Event()
        ve.add("uid", f"busy-{uid}" if busy_only else uid)
        ve.add("dtstart", s)
        ve.add("dtend", e)
        ve.add("dtstamp", now)
        ve.add("transp", "OPAQUE")
        ve.add("status", "CONFIRMED")
        if busy_only:
            ve.add("summary", "Busy")
        else:
            ve.add("summary", ev.get("summary") or "Visit")
            desc = (ev.get("description") or "").strip()
            if desc:
                ve.add("description", desc)
        cal.add_component(ve)
    return cal.to_ical()


def build_visit_ics(
    *,
    uid: str,
    summary: str,
    start: datetime,
    end: datetime,
    timezone: str = "America/Chicago",
    description: str = "",
    location: str = "https://meet.drmattbrown.com",
) -> bytes:
    """One-event invite for the patient (Proton / Apple / Google / Outlook)."""
    from icalendar import Calendar, Event, vText

    tz = ZoneInfo(timezone or "America/Chicago")
    now = datetime.now(tz)
    s = start if start.tzinfo else start.replace(tzinfo=tz)
    e = end if end.tzinfo else end.replace(tzinfo=tz)
    s, e = s.astimezone(tz), e.astimezone(tz)

    cal = Calendar()
    cal.add("prodid", "-//Psych Arts Booker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", vText(summary or "Psych Arts visit"))
    cal.add("x-wr-timezone", str(tz))
    ve = Event()
    ve.add("uid", uid or f"visit-{int(now.timestamp())}")
    ve.add("summary", summary or "Visit")
    ve.add("dtstart", s)
    ve.add("dtend", e)
    ve.add("dtstamp", now)
    ve.add("transp", "OPAQUE")
    ve.add("status", "CONFIRMED")
    if location:
        ve.add("location", location)
        ve.add("url", location)
    if description:
        ve.add("description", description)
    cal.add_component(ve)
    return cal.to_ical()
