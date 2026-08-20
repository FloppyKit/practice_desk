"""Local CalDAV (Radicale) — busy + events. No Google required to read."""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

log = logging.getLogger("booker.caldav")

CALDAV_URL = os.environ.get("CALDAV_URL", "http://127.0.0.1:5232/").rstrip("/") + "/"
CALDAV_USER = os.environ.get("CALDAV_USER", "booker")
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", "")
CALDAV_CALENDAR = os.environ.get("CALDAV_CALENDAR", "practice")


def ready() -> bool:
    return bool(CALDAV_URL and CALDAV_USER and CALDAV_PASSWORD)


def _client():
    import caldav

    return caldav.DAVClient(
        url=CALDAV_URL,
        username=CALDAV_USER,
        password=CALDAV_PASSWORD,
    )


_cal = None
_list_cache: dict[tuple[str, str], tuple[float, list]] = {}
_LIST_TTL = 12.0


def _calendar(force_principal: bool = False):
    """Reuse one Calendar handle. Skip principal()+calendars() PROPFIND on the hot path."""
    global _cal
    if _cal is not None and not force_principal:
        return _cal
    import caldav

    client = _client()
    name = CALDAV_CALENDAR
    if not force_principal:
        direct = (
            f"{CALDAV_URL.rstrip('/')}/"
            f"{quote(CALDAV_USER, safe='')}/"
            f"{quote(name, safe='')}/"
        )
        _cal = caldav.Calendar(client=client, url=direct)
        return _cal
    principal = client.principal()
    for cal in principal.calendars():
        try:
            if (cal.name or "").lower() == name.lower():
                _cal = cal
                return cal
        except Exception:
            continue
        href = str(getattr(cal, "url", "") or "")
        if href.rstrip("/").endswith("/" + name) or f"/{name}/" in href:
            _cal = cal
            return cal
    _cal = principal.make_calendar(name=name, cal_id=name)
    return _cal


def reset() -> None:
    global _cal
    _cal = None
    _list_cache.clear()


def _bump_lists() -> None:
    _list_cache.clear()


def _prop_key(k: str) -> str:
    return "X-PSYCHARTS-" + str(k).upper().replace("_", "-")


def _ics(
    *,
    uid: str,
    summary: str,
    description: str,
    start: datetime,
    end: datetime,
    timezone: str,
    private_props: dict[str, str] | None,
) -> bytes:
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//Psych Arts Booker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    if description:
        ev.add("description", description)
    tz = ZoneInfo(timezone)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    if end.tzinfo is None:
        end = end.replace(tzinfo=tz)
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("dtstamp", datetime.now(tz))
    props = dict(private_props or {})
    props.setdefault("booker", "1")
    for k, v in props.items():
        ev[_prop_key(k)] = str(v)
    cal.add_component(ev)
    return cal.to_ical()


def _vevent_to_google(comp) -> dict[str, Any]:
    from icalendar import vDDDTypes

    def as_dt(val) -> datetime | None:
        if val is None:
            return None
        dt = val.dt if hasattr(val, "dt") else val
        if isinstance(dt, datetime):
            return dt
        return None

    uid = str(comp.get("uid") or "")
    summary = str(comp.get("summary") or "")
    description = str(comp.get("description") or "")
    start = as_dt(comp.get("dtstart"))
    end = as_dt(comp.get("dtend"))
    priv: dict[str, str] = {}
    for key, val in comp.items():
        k = str(key)
        if k.upper().startswith("X-PSYCHARTS-"):
            short = k[12:].lower().replace("-", "_")
            priv[short] = str(val)
    return {
        "id": uid,
        "summary": summary,
        "description": description,
        "status": "confirmed",
        "start": {"dateTime": start.isoformat()} if start else {},
        "end": {"dateTime": end.isoformat()} if end else {},
        "extendedProperties": {"private": priv},
    }


def _search(cal, time_min: datetime, time_max: datetime):
    try:
        return cal.search(
            start=time_min,
            end=time_max,
            event=True,
            expand=True,
        )
    except TypeError:
        return cal.date_search(start=time_min, end=time_max, expand=True)


def _iter_events(time_min: datetime, time_max: datetime):
    cal = _calendar()
    try:
        results = _search(cal, time_min, time_max)
    except Exception as e:
        log.warning("caldav search retry via principal: %s", e)
        reset()
        cal = _calendar(force_principal=True)
        results = _search(cal, time_min, time_max)
    for item in results or []:
        try:
            ical = item.icalendar_instance
        except Exception:
            try:
                from icalendar import Calendar

                ical = Calendar.from_ical(item.data)
            except Exception:
                continue
        for comp in ical.walk("VEVENT"):
            yield item, comp


def freebusy(
    _calendar_ids: list[str], time_min: datetime, time_max: datetime
) -> list[dict[str, datetime]]:
    busy: list[tuple[datetime, datetime]] = []
    for _item, comp in _iter_events(time_min, time_max):
        start = comp.get("dtstart")
        end = comp.get("dtend")
        if not start or not end:
            continue
        s, e = start.dt, end.dt
        if not isinstance(s, datetime) or not isinstance(e, datetime):
            continue
        busy.append((s, e))
    busy.sort(key=lambda x: x[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in busy:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
    return [{"start": s, "end": e} for s, e in merged]


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
    del calendar_id, attendee_email
    uid = uid or uuid.uuid4().hex
    ics = _ics(
        uid=uid,
        summary=summary,
        description=description,
        start=start,
        end=end,
        timezone=timezone,
        private_props=private_props,
    )
    cal = _calendar()
    cal.save_event(ics.decode() if isinstance(ics, bytes) else ics)
    _bump_lists()
    return {
        "id": uid,
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "extendedProperties": {"private": dict(private_props or {})},
        "htmlLink": "",
        "status": "confirmed",
    }


def get_event(calendar_id: str, event_id: str) -> dict[str, Any]:
    del calendar_id
    cal = _calendar()
    ev = cal.event_by_uid(event_id)
    ical = ev.icalendar_instance
    for comp in ical.walk("VEVENT"):
        return _vevent_to_google(comp)
    raise KeyError(event_id)


def list_events(
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    del calendar_id
    key = (time_min.isoformat(), time_max.isoformat())
    now = time.monotonic()
    hit = _list_cache.get(key)
    if hit and now - hit[0] < _LIST_TTL:
        return list(hit[1])
    out = []
    for _item, comp in _iter_events(time_min, time_max):
        out.append(_vevent_to_google(comp))
    out.sort(key=lambda e: (e.get("start") or {}).get("dateTime") or "")
    if len(_list_cache) > 40:
        _list_cache.clear()
    _list_cache[key] = (now, out)
    return list(out)


def list_booker_events(
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    return [
        e
        for e in list_events(calendar_id, time_min, time_max)
        if ((e.get("extendedProperties") or {}).get("private") or {}).get("booker") == "1"
    ]


def patch_event(
    calendar_id: str,
    event_id: str,
    *,
    description: str | None = None,
    private_props: dict[str, str] | None = None,
) -> dict[str, Any]:
    del calendar_id
    cal = _calendar()
    ev = cal.event_by_uid(event_id)
    ical = ev.icalendar_instance
    for comp in ical.walk("VEVENT"):
        if description is not None:
            comp["description"] = description
        if private_props:
            for k, v in private_props.items():
                comp[_prop_key(k)] = str(v)
        break
    ev.data = ical.to_ical()
    ev.save()
    _bump_lists()
    return get_event("", event_id)


def delete_event(calendar_id: str, event_id: str) -> None:
    del calendar_id
    cal = _calendar()
    ev = cal.event_by_uid(event_id)
    ev.delete()
    _bump_lists()
