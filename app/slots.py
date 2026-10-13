from __future__ import annotations

from datetime import datetime, timedelta, time
from typing import Any
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

from .cal import freebusy

WEEKDAY_KEYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def day_windows(raw: Any, duration_minutes: int = 0) -> list[dict[str, str]]:
    """One day may be a single block or several windows (a gap between them)."""
    if isinstance(raw, list):
        return [w for w in raw if isinstance(w, dict) and w.get("start") and w.get("end")]
    if not isinstance(raw, dict):
        return []
    by = raw.get("by_duration") or raw.get("hours_by_duration")
    if isinstance(by, dict) and duration_minutes:
        specific = by.get(duration_minutes) or by.get(str(duration_minutes))
        if specific is not None:
            return day_windows(specific, 0)
    windows = raw.get("windows")
    if isinstance(windows, list):
        return day_windows(windows, 0)
    if raw.get("start") and raw.get("end"):
        return [raw]
    return []


def windows_for(event_type: dict[str, Any], day_key: str, duration_minutes: int) -> list[dict[str, str]]:
    by = event_type.get("hours_by_duration") or {}
    if isinstance(by, dict):
        specific = by.get(duration_minutes) or by.get(str(duration_minutes))
        if isinstance(specific, dict) and day_key in specific:
            return day_windows(specific.get(day_key), duration_minutes)
    weekly = event_type.get("weekly_hours") or {}
    return day_windows(weekly.get(day_key), duration_minutes)


def window_starts(
    day,
    window: dict[str, str],
    *,
    interval: int,
    cal_dur: int,
    tz,
) -> list[datetime]:
    """Starts inside one window. latest_start caps the start; otherwise the block must finish by end."""
    open_t = _parse_hhmm(window["start"])
    cursor = datetime.combine(day, open_t, tzinfo=tz)
    latest = str(window.get("latest_start") or "").strip()
    if latest:
        limit = datetime.combine(day, _parse_hhmm(latest), tzinfo=tz)
        out = []
        while cursor <= limit:
            out.append(cursor)
            cursor += timedelta(minutes=interval)
        return out
    close_t = _parse_hhmm(window["end"])
    day_end = datetime.combine(day, close_t, tzinfo=tz)
    out = []
    while cursor + timedelta(minutes=cal_dur) <= day_end:
        out.append(cursor)
        cursor += timedelta(minutes=interval)
    return out


def _overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


def generate_slots(
    config: dict[str, Any],
    event_type: dict[str, Any],
    duration_minutes: int,
    range_start: datetime | None = None,
    days: int | None = None,
) -> list[dict[str, str]]:
    tz = ZoneInfo(config.get("timezone", "America/Chicago"))
    now = datetime.now(tz)
    min_notice = int(event_type.get("minimum_notice_minutes", 120))
    window_days = int(days or event_type.get("booking_window_days", 30))
    interval = int(event_type.get("slot_interval_minutes", 15))
    buf_before = int(event_type.get("buffer_before_minutes", 0))
    buf_after = int(event_type.get("buffer_after_minutes", 0))
    # Client-facing length vs calendar busy length (e.g. 90 shown, 120 blocked)
    client_dur = int(duration_minutes)
    cal_dur = int(event_type.get("calendar_duration_minutes") or client_dur)
    if cal_dur < client_dur:
        cal_dur = client_dur
    weekly = event_type.get("weekly_hours") or {}

    if range_start is None:
        range_start = now
    else:
        if range_start.tzinfo is None:
            range_start = range_start.replace(tzinfo=tz)
        else:
            range_start = range_start.astimezone(tz)

    earliest = now + timedelta(minutes=min_notice)
    range_end = now + timedelta(days=window_days)

    busy_ids = list(config.get("busy_calendars") or [])
    cal_id = event_type.get("google_calendar_id")
    if cal_id and cal_id not in busy_ids:
        busy_ids.append(cal_id)

    # query freebusy in UTC
    fb_start = earliest.astimezone(ZoneInfo("UTC"))
    fb_end = range_end.astimezone(ZoneInfo("UTC"))
    busy = freebusy(busy_ids, fb_start, fb_end)
    busy_local = [
        {
            "start": b["start"].astimezone(tz),
            "end": b["end"].astimezone(tz),
        }
        for b in busy
    ]

    slots: list[dict[str, str]] = []
    day = max(range_start, earliest).date()
    end_day = range_end.date()

    while day <= end_day:
        key = WEEKDAY_KEYS[day.weekday()]
        for window in windows_for(event_type, key, client_dur):
            for start in window_starts(
                day, window, interval=interval, cal_dur=cal_dur, tz=tz
            ):
                client_end = start + timedelta(minutes=client_dur)
                if start < earliest:
                    continue
                block_start = start - timedelta(minutes=buf_before)
                block_end = start + timedelta(minutes=cal_dur + buf_after)
                conflict = False
                for b in busy_local:
                    if _overlaps(block_start, block_end, b["start"], b["end"]):
                        conflict = True
                        break
                if not conflict:
                    slots.append(
                        {
                            "start": start.isoformat(),
                            "end": client_end.isoformat(),
                            "label": start.isoformat(),
                        }
                    )
        day += timedelta(days=1)

    # normalize labels (Linux strftime %-d may fail on some; fix)
    fixed = []
    for s in slots:
        st = dateparser.isoparse(s["start"])
        fixed.append(
            {
                "start": s["start"],
                "end": s["end"],
                "label": st.strftime("%a %b %d · %I:%M %p").replace(" 0", " "),
            }
        )
    return fixed
