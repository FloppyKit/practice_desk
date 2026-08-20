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
        hours = weekly.get(key)
        if hours:
            open_t = _parse_hhmm(hours["start"])
            close_t = _parse_hhmm(hours["end"])
            cursor = datetime.combine(day, open_t, tzinfo=tz)
            day_end = datetime.combine(day, close_t, tzinfo=tz)
            # Need enough open day for the *calendar* block (may exceed client duration)
            while cursor + timedelta(minutes=cal_dur) <= day_end:
                start = cursor
                client_end = cursor + timedelta(minutes=client_dur)
                if start >= earliest:
                    # Busy check uses full calendar block (+ optional buffers; default 0)
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
                                # client-facing end (visit length); calendar write may be longer
                                "end": client_end.isoformat(),
                                "label": start.strftime("%a %b %-d · %-I:%M %p")
                                if hasattr(start, "strftime")
                                else start.isoformat(),
                            }
                        )
                cursor += timedelta(minutes=interval)
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
