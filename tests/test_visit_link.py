"""Transcript -> visit matching. Synthetic; pure; UTC/CT aware."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.visit_link import match, recording_window

CT = ZoneInfo("America/Chicago")


def V(eid, h, m, mins, cid="", day=30):
    s = datetime(2026, 10, day, h, m, tzinfo=CT)
    return {"event_id": eid, "client_id": cid, "start": s.astimezone(timezone.utc),
            "end": (s + timedelta(minutes=mins)).astimezone(timezone.utc)}


def R(h, m, mins, day=30):
    s = datetime(2026, 10, day, h, m, tzinfo=CT).astimezone(timezone.utc)
    return s, s + timedelta(minutes=mins)


def test_overlap() -> None:
    assert match(*R(10, 2, 45), "", [V("a", 10, 0, 50)]) == ("a", "linked")


def test_early_start_within_15() -> None:
    assert match(*R(9, 46, 10), "", [V("a", 10, 0, 50)]) == ("a", "linked")
    assert match(*R(9, 30, 10), "", [V("a", 10, 0, 50)]) == ("", "none")


def test_late_run_within_30() -> None:
    assert match(*R(11, 15, 5), "", [V("a", 10, 0, 50)]) == ("a", "linked")
    assert match(*R(11, 30, 5), "", [V("a", 10, 0, 50)]) == ("", "none")


def test_back_to_back_picks_true_overlap() -> None:
    visits = [V("a", 10, 0, 50), V("b", 10, 50, 50)]
    assert match(*R(10, 0, 48), "", visits) == ("a", "linked")
    assert match(*R(10, 52, 45), "", visits) == ("b", "linked")


def test_two_clients_same_time() -> None:
    visits = [V("a", 10, 0, 50, "c1"), V("b", 10, 0, 50, "c2")]
    assert match(*R(10, 0, 50), "", visits) == ("", "ambiguous")
    assert match(*R(10, 0, 50), "c2", visits) == ("b", "linked")
    assert match(*R(10, 0, 50), "c3", visits) == ("", "none")  # known client, no visit of theirs


def test_no_match() -> None:
    assert match(*R(15, 0, 30), "", [V("a", 10, 0, 50)]) == ("", "none")
    assert match(*R(15, 0, 30), "", []) == ("", "none")


def test_ct_utc_day_boundary() -> None:
    # 6:30 PM CT Oct 30 = 23:30 UTC; recording runs past UTC midnight (Oct 31 UTC).
    v = V("eve", 18, 30, 50)
    assert v["start"].day == 30 and v["end"].astimezone(timezone.utc).day == 31
    row = {"ended_at": "2026-10-31T00:22:00Z", "duration_minutes": 50, "created": ""}
    rs, re_ = recording_window(row)
    assert match(rs, re_, "", [v, V("next-day", 18, 30, 50, day=31)]) == ("eve", "linked")
