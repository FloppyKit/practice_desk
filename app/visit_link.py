"""Tie scribe transcripts to calendar visits by recording time (all math in aware UTC datetimes).

match(rec_start, rec_end, client_id, visits) -> (event_id, status)   pure
  visits: [{event_id, start, end (aware datetimes), client_id ('' if unknown)}]
  status: linked | ambiguous | none
Tolerance: recording may start up to EARLY before a visit and run up to LATE after it.
Several candidates -> keep those whose true overlap covers >= half the recording (back-to-back);
still more than one -> ambiguous (left unlinked, flagged).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

EARLY = timedelta(minutes=15)
LATE = timedelta(minutes=30)


def parse_ts(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def recording_window(row: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    end = parse_ts(row.get("ended_at") or "") or parse_ts(row.get("created") or "")
    if not end:
        return None, None
    mins = int(row.get("duration_minutes") or 0)
    return end - timedelta(minutes=max(mins, 0)), end


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def match(rec_start: datetime, rec_end: datetime, client_id: str, visits: list[dict[str, Any]]) -> tuple[str, str]:
    pool = [v for v in visits if v.get("start") and v.get("end") and v.get("event_id")]
    if client_id:
        pool = [v for v in pool if v.get("client_id") == client_id]
    cands = [v for v in pool if rec_start <= v["end"] + LATE and rec_end >= v["start"] - EARLY]
    if len(cands) > 1:
        span = max((rec_end - rec_start).total_seconds(), 60.0)
        strong = [v for v in cands if _overlap(rec_start, rec_end, v["start"], v["end"]) >= span / 2]
        cands = strong if strong else cands
    if len(cands) == 1:
        return cands[0]["event_id"], "linked"
    return "", ("ambiguous" if cands else "none")
