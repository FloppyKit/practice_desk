"""Keep local CalDAV busy copy in sync with Google (psychartsservices) every few minutes.

Only touches CalDAV events imported from Google (X-PSYCHARTS-BOOKER:0). Booker-created
bookings (BOOKER:1) and anything without that marker are never modified or deleted.
Logs counts only (no names, emails, or titles).
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

INTERVAL = int(os.environ.get("GCAL_SYNC_INTERVAL_SECONDS", "300"))
FETCH_BACK_DAYS, FETCH_AHEAD_DAYS = 2, 100     # window fetched from both sides
ACT_BACK_DAYS, ACT_AHEAD_DAYS = 1, 95          # deletes only for events starting inside this
MAX_DELETE_FRACTION, MAX_DELETE_FLOOR = 0.4, 25


def _log(**kw) -> None:
    print("gcal_sync", kw, flush=True)


def _dt(val):
    dt = getattr(val, "dt", val)
    return dt if isinstance(dt, datetime) else None


def sync_once() -> dict:
    from . import caldav_store, gcal
    from .cal import using_caldav
    from .config import DEFAULT_CALENDAR

    if not using_caldav() or gcal is None or not DEFAULT_CALENDAR:
        return {"ok": False, "reason": "caldav or google off"}
    tz = ZoneInfo("America/Chicago")
    now = datetime.now(tz)
    f0, f1 = now - timedelta(days=FETCH_BACK_DAYS), now + timedelta(days=FETCH_AHEAD_DAYS)
    a0, a1 = now - timedelta(days=ACT_BACK_DAYS), now + timedelta(days=ACT_AHEAD_DAYS)
    try:
        g_items = gcal.list_events(DEFAULT_CALENDAR, f0, f1)
    except Exception as e:
        _log(ok=False, stage="google_fetch", error=type(e).__name__)
        return {"ok": False, "reason": "google fetch failed"}
    google = {}
    for ev in g_items:
        if ev.get("status") == "cancelled":
            continue
        priv = ((ev.get("extendedProperties") or {}).get("private")) or {}
        if priv.get("booker") == "1":
            continue  # booker-created booking mirrored to Google: CalDAV already owns it
        s = (ev.get("start") or {}).get("dateTime")
        e = (ev.get("end") or {}).get("dateTime")
        if not ev.get("id") or not s or not e:
            continue
        google[ev["id"]] = (
            datetime.fromisoformat(s.replace("Z", "+00:00")),
            datetime.fromisoformat(e.replace("Z", "+00:00")),
            ev,
        )
    try:
        local = list(caldav_store._iter_events(f0, f1))
    except Exception as e:
        _log(ok=False, stage="caldav_read", error=type(e).__name__)
        return {"ok": False, "reason": "caldav read failed"}

    managed: dict[str, list] = {}
    all_uids = set()
    for item, comp in local:
        uid = str(comp.get("uid") or "")
        all_uids.add(uid)
        if str(comp.get("X-PSYCHARTS-BOOKER") or "") == "0" and "RRULE" not in comp:
            managed.setdefault(uid, []).append((item, comp))

    added = updated = deleted = failed = 0
    # add new Google events
    for uid, (s, e, ev) in google.items():
        if uid in all_uids:
            continue
        try:
            caldav_store.create_event(
                calendar_id="", summary=ev.get("summary") or "Busy", description="",
                start=s, end=e, timezone="America/Chicago",
                private_props={"booker": "0", "gsync": "1"}, uid=uid,
            )
            added += 1
        except Exception:
            failed += 1
    # update moved events
    for uid, rows in managed.items():
        if uid not in google:
            continue
        s, e, _ev = google[uid]
        for item, comp in rows:
            if _dt(comp.get("dtstart")) == s and _dt(comp.get("dtend")) == e:
                continue
            try:
                ical = item.icalendar_instance
                for c in ical.walk("VEVENT"):
                    if str(c.get("uid") or "") == uid:
                        del c["dtstart"]; c.add("dtstart", s)
                        if "dtend" in c: del c["dtend"]
                        c.add("dtend", e)
                item.data = ical.to_ical()
                item.save()
                updated += 1
            except Exception:
                failed += 1
    # deletions, guarded
    gone = []
    for uid, rows in managed.items():
        if uid in google:
            continue
        for item, comp in rows:
            st = _dt(comp.get("dtstart"))
            if st and a0 <= st <= a1:
                gone.append(item)
    in_window = sum(1 for rows in managed.values() for _i, c in rows
                    if (_dt(c.get("dtstart")) or now - timedelta(days=999)) >= a0
                    and (_dt(c.get("dtstart")) or now) <= a1)
    limit = max(MAX_DELETE_FLOOR, int(in_window * MAX_DELETE_FRACTION))
    skipped = ""
    if not google and in_window:
        skipped = "google_empty"
    elif len(gone) > limit:
        skipped = "too_many_deletes"
    if not skipped:
        for item in gone:
            try:
                item.delete()
                deleted += 1
            except Exception:
                failed += 1
    caldav_store._bump_lists()
    out = {"ok": True, "google": len(google), "managed_in_window": in_window, "added": added,
           "updated": updated, "deleted": deleted, "delete_candidates": len(gone),
           "deletes_skipped": skipped or None, "failed": failed}
    _log(**out)
    return out


def _loop(wait_for: threading.Thread | None) -> None:
    if wait_for is not None:
        wait_for.join(timeout=600)
    while True:
        try:
            sync_once()
        except Exception as e:
            _log(ok=False, stage="unexpected", error=type(e).__name__)
        time.sleep(max(60, INTERVAL))


def start(wait_for: threading.Thread | None = None) -> None:
    if (os.environ.get("GCAL_SYNC", "1").strip().lower()) not in ("1", "true", "yes"):
        return
    threading.Thread(target=_loop, args=(wait_for,), name="gcal-sync", daemon=True).start()
