"""Calendar facade: local CalDAV is SoT. Google is an optional write-mirror."""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any

from . import caldav_store
from . import demo_store

try:
    from . import gcal
except ImportError:  # demo/minimal installs may lack google client libs
    gcal = None  # type: ignore[assignment]

log = logging.getLogger("booker.cal")

# google | caldav | both | demo  (both = read CalDAV, write CalDAV+Google)
BACKEND = (os.environ.get("CALENDAR_BACKEND") or "google").strip().lower()
MIRROR_GOOGLE = (os.environ.get("CALENDAR_MIRROR_GOOGLE") or "").strip().lower() in (
    "1",
    "true",
    "yes",
) or BACKEND == "both"


def using_demo() -> bool:
    return BACKEND == "demo" or demo_store.ready()


def using_caldav() -> bool:
    if using_demo():
        return False
    return BACKEND in ("caldav", "both") and caldav_store.ready()


def _read_caldav() -> bool:
    return using_caldav()


def _write_google() -> bool:
    if using_demo():
        return False
    if BACKEND == "google":
        return True
    return MIRROR_GOOGLE


def _new_uid() -> str:
    # Google event ids must be base32hex-ish; hex UUID is safe on both backends.
    return uuid.uuid4().hex


def _mirror(name: str, fn, *args, **kwargs) -> None:
    def run() -> None:
        try:
            fn(*args, **kwargs)
        except Exception as e:
            log.warning("google mirror %s: %s", name, e)

    threading.Thread(target=run, name=f"gcal-{name}", daemon=True).start()


def freebusy(
    calendar_ids: list[str], time_min: datetime, time_max: datetime
) -> list[dict[str, datetime]]:
    if using_demo():
        return demo_store.freebusy(calendar_ids, time_min, time_max)
    if _read_caldav():
        try:
            return caldav_store.freebusy(calendar_ids, time_min, time_max)
        except Exception as e:
            log.warning("caldav freebusy failed, google fallback: %s", e)
    return gcal.freebusy(calendar_ids, time_min, time_max)


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
    uid = uid or _new_uid()
    if using_demo():
        return demo_store.create_event(
            calendar_id=calendar_id,
            summary=summary,
            description=description,
            start=start,
            end=end,
            timezone=timezone,
            attendee_email=attendee_email,
            private_props=private_props,
            uid=uid,
        )
    local: dict[str, Any] | None = None
    if using_caldav():
        local = caldav_store.create_event(
            calendar_id=calendar_id,
            summary=summary,
            description=description,
            start=start,
            end=end,
            timezone=timezone,
            attendee_email=attendee_email,
            private_props=private_props,
            uid=uid,
        )
    if _write_google():
        kwargs = dict(
            calendar_id=calendar_id,
            summary=summary,
            description=description,
            start=start,
            end=end,
            timezone=timezone,
            attendee_email=attendee_email,
            private_props=private_props,
            uid=uid,
        )
        if local is not None:
            _mirror("create", gcal.create_event, **kwargs)
            return local
        return gcal.create_event(**kwargs)
    if not local:
        raise RuntimeError("no calendar backend wrote the event")
    return local


def get_event(calendar_id: str, event_id: str) -> dict[str, Any]:
    if using_demo():
        return demo_store.get_event(calendar_id, event_id)
    if using_caldav():
        try:
            return caldav_store.get_event(calendar_id, event_id)
        except Exception as e:
            log.info("caldav get miss %s: %s", event_id, e)
    return gcal.get_event(calendar_id, event_id)


def list_events(
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    if using_demo():
        return demo_store.list_events(calendar_id, time_min, time_max)
    if _read_caldav():
        try:
            return caldav_store.list_events(calendar_id, time_min, time_max)
        except Exception as e:
            log.warning("caldav list failed: %s", e)
    return gcal.list_events(calendar_id, time_min, time_max)


def list_booker_events(
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
) -> list[dict[str, Any]]:
    if using_demo():
        return demo_store.list_booker_events(calendar_id, time_min, time_max)
    if _read_caldav():
        try:
            return caldav_store.list_booker_events(calendar_id, time_min, time_max)
        except Exception as e:
            log.warning("caldav booker list failed: %s", e)
    return gcal.list_booker_events(calendar_id, time_min, time_max)


def patch_event(
    calendar_id: str,
    event_id: str,
    *,
    description: str | None = None,
    private_props: dict[str, str] | None = None,
) -> dict[str, Any]:
    if using_demo():
        return demo_store.patch_event(
            calendar_id,
            event_id,
            description=description,
            private_props=private_props,
        )
    out: dict[str, Any] = {}
    if using_caldav():
        try:
            out = caldav_store.patch_event(
                calendar_id,
                event_id,
                description=description,
                private_props=private_props,
            )
        except Exception as e:
            log.warning("caldav patch: %s", e)
    if _write_google():
        if out:
            _mirror(
                "patch",
                gcal.patch_event,
                calendar_id,
                event_id,
                description=description,
                private_props=private_props,
            )
            return out
        out = gcal.patch_event(
            calendar_id,
            event_id,
            description=description,
            private_props=private_props,
        )
    return out


def delete_event(calendar_id: str, event_id: str) -> None:
    if using_demo():
        demo_store.delete_event(calendar_id, event_id)
        return
    local_ok = False
    if using_caldav():
        try:
            caldav_store.delete_event(calendar_id, event_id)
            local_ok = True
        except Exception as e:
            log.warning("caldav delete: %s", e)
    if _write_google():
        if local_ok:
            _mirror("delete", gcal.delete_event, calendar_id, event_id)
            return
        gcal.delete_event(calendar_id, event_id)


def seed_from_google(days: int = 60) -> dict[str, Any]:
    """Copy upcoming Google events into CalDAV once so reads stay local."""
    if using_demo():
        return {"ok": False, "reason": "demo backend"}
    if not using_caldav():
        return {"ok": False, "reason": "caldav off"}
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from .config import DEFAULT_CALENDAR

    tz = ZoneInfo("America/Chicago")
    now = datetime.now(tz)
    end = now + timedelta(days=days)
    items = gcal.list_events(DEFAULT_CALENDAR, now.astimezone(), end.astimezone())
    try:
        have = {
            e.get("id")
            for e in caldav_store.list_events("", now.astimezone(), end.astimezone())
            if e.get("id")
        }
    except Exception as e:
        log.warning("seed list existing failed: %s", e)
        have = set()
    n = 0
    skipped = 0
    for ev in items:
        uid = ev.get("id") or ""
        if not uid:
            continue
        if uid in have:
            skipped += 1
            continue
        start_raw = (ev.get("start") or {}).get("dateTime")
        end_raw = (ev.get("end") or {}).get("dateTime")
        if not start_raw or not end_raw:
            continue
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        finish = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        priv = ((ev.get("extendedProperties") or {}).get("private")) or {}
        try:
            caldav_store.create_event(
                calendar_id="",
                summary=ev.get("summary") or "Busy",
                description=ev.get("description") or "",
                start=start,
                end=finish,
                timezone="America/Chicago",
                private_props={str(k): str(v) for k, v in priv.items()} or {"booker": "0"},
                uid=uid,
            )
            n += 1
            if n % 25 == 0:
                log.info("caldav seed imported %s", n)
        except Exception as e:
            log.warning("seed skip %s: %s", uid, e)
    return {"ok": True, "imported": n, "skipped": skipped, "seen": len(items)}
