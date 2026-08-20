"""Demo backend — fixture week, fake people only, no network."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

os.environ["CALENDAR_BACKEND"] = "demo"

from app import cal, demo_store  # noqa: E402
from app.week_people import SKIP_TITLES, people_from_visits  # noqa: E402

TZ = ZoneInfo("America/Chicago")


def _this_week() -> tuple[datetime, datetime]:
    now = datetime.now(TZ)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return monday, monday + timedelta(days=7)


def _visits(events: list[dict]) -> list[dict]:
    out = []
    for ev in events:
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        out.append(
            {
                "event_id": ev.get("id") or "",
                "name": priv.get("patient_name") or "",
                "email": priv.get("patient_email") or "",
                "phone": priv.get("patient_phone") or "",
                "title": priv.get("title") or ev.get("summary") or "Visit",
            }
        )
    return out


def test_demo_backend_ready():
    assert demo_store.ready()
    assert cal.using_demo()
    assert not cal.using_caldav()
    assert not cal._write_google()


def test_fixture_week_has_jane_and_blocks():
    t0, t1 = _this_week()
    events = demo_store.list_events("", t0, t1)
    summaries = {e.get("summary") for e in events}
    assert "Lunch" in summaries
    assert "Hold" in summaries
    names = {
        ((e.get("extendedProperties") or {}).get("private") or {}).get("patient_name")
        for e in events
    }
    assert "Jane Demo" in names


def test_people_from_fixture_returns_jane_skips_blocks():
    t0, t1 = _this_week()
    visits = _visits(demo_store.list_events("", t0, t1))
    hits = people_from_visits(visits, "jane")
    assert len(hits) == 1
    assert hits[0]["name"] == "Jane Demo"
    assert hits[0]["email"] == "jane@example.invalid"
    everyone = people_from_visits(visits, "")
    names = [p["name"] for p in everyone]
    assert "Jane Demo" in names
    assert "Lunch" not in names
    assert "Hold" not in names


def test_skip_titles_cover_fixture_blocks():
    assert SKIP_TITLES.match("Lunch")
    assert SKIP_TITLES.match("Hold")


def test_cal_facade_reads_fixture_without_network():
    t0, t1 = _this_week()
    events = cal.list_events("anything", t0, t1)
    assert events
    busy = cal.freebusy(["anything"], t0, t1)
    assert busy
    booker = cal.list_booker_events("anything", t0, t1)
    assert booker
    assert all(
        ((e.get("extendedProperties") or {}).get("private") or {}).get("booker") == "1"
        for e in booker
    )


def test_fixture_is_fake_people_only():
    raw = demo_store.FIXTURE.read_text(encoding="utf-8")
    assert "example.invalid" in raw
    for frag in ("drmattbrown", "psycharts", "matt@", "1881910081"):
        assert frag not in raw
