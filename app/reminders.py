"""Scan Google Calendar for booker events and send timed reminders.

Email: T-24h and T-1h.
SMS: T-30m (default) or T-15m — join ping only, not a second email.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import DEFAULT_CALENDAR, load_config
from .cal import list_booker_events, patch_event, using_caldav
from .notify import (
    append_log,
    patient_reminder_email,
    patient_reminder_sms,
)
from .tokens import mint_cancel_token

log = logging.getLogger("booker.reminders")

PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "https://book.psycharts.org").rstrip("/")
# Window: look ahead this many hours for candidates
LOOKAHEAD_HOURS = float(os.environ.get("REMINDER_LOOKAHEAD_HOURS", "26"))
REM_24H_HOURS = float(os.environ.get("REMINDER_24H_HOURS", "24"))
REM_1H_HOURS = float(os.environ.get("REMINDER_1H_HOURS", "1"))
# SMS join ping: 30 or 15 minutes before start
REM_SMS_MINUTES = float(os.environ.get("REMINDER_SMS_MINUTES", "30"))


def _props(ev: dict[str, Any]) -> dict[str, str]:
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    return {str(k): str(v) for k, v in priv.items()}


def _event_start(ev: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    start = ev.get("start") or {}
    if start.get("dateTime"):
        dt = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        return dt.astimezone(tz)
    if start.get("date"):
        # all-day — skip reminders
        return None
    return None


def process_reminders(*, dry_run: bool = False) -> dict[str, Any]:
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    now = datetime.now(tz)
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID") or DEFAULT_CALENDAR
    # Also scan each type's calendar if different
    cal_ids = {cal_id}
    for et in (cfg.get("event_types") or {}).values():
        cid = et.get("google_calendar_id")
        if cid:
            cal_ids.add(cid)
    if using_caldav() and cal_ids:
        cal_ids = {next(iter(cal_ids))}

    time_min = now.astimezone(timezone.utc)
    time_max = (now + timedelta(hours=LOOKAHEAD_HOURS)).astimezone(timezone.utc)

    results: list[dict[str, Any]] = []
    for cid in cal_ids:
        try:
            events = list_booker_events(cid, time_min, time_max)
        except Exception as e:
            row = {"ok": False, "calendar": cid, "error": str(e)}
            results.append(row)
            append_log("reminders.jsonl", {**row, "at": now.isoformat()})
            continue

        for ev in events:
            event_id = ev.get("id") or ""
            props = _props(ev)
            start = _event_start(ev, tz)
            if not start or not event_id:
                continue
            hours_until = (start - now).total_seconds() / 3600.0
            if hours_until <= 0:
                continue

            email = props.get("patient_email") or ""
            name = props.get("patient_name") or "there"
            phone = props.get("patient_phone") or ""
            sms_consent = props.get("sms_consent") == "1"
            title = props.get("title") or ev.get("summary") or "Visit"
            when_label = start.strftime("%a %b %d · %I:%M %p").replace(" 0", " ")
            dur_raw = (props.get("duration_minutes") or "").strip()
            duration_minutes = int(dur_raw) if dur_raw.isdigit() else None
            if duration_minutes is None:
                ev_end = (ev.get("end") or {}).get("dateTime")
                if ev_end:
                    try:
                        end_dt = datetime.fromisoformat(ev_end.replace("Z", "+00:00")).astimezone(tz)
                        mins = int(round((end_dt - start).total_seconds() / 60.0))
                        if 5 <= mins <= 240:
                            duration_minutes = mins
                    except Exception:
                        duration_minutes = None
            price_label = props.get("price_label") or ""

            cancel_url = ""
            ics_url = ""
            rebook_url = PUBLIC_BASE
            try:
                et_slug = props.get("event_type") or ""
                token = mint_cancel_token(
                    event_id=event_id,
                    calendar_id=cid,
                    email=email or "unknown@invalid",
                    title=title,
                    when_label=when_label,
                    name=name if name != "there" else "",
                    event_type=et_slug,
                    start_iso=start.isoformat(),
                    duration_minutes=duration_minutes,
                )
                cancel_url = f"{PUBLIC_BASE}/cancel?token={token}"
                rebook_url = f"{PUBLIC_BASE}/{et_slug}" if et_slug else PUBLIC_BASE
                ics_url = f"{PUBLIC_BASE}/cal/visit.ics?token={token}"
            except Exception:
                pass

            # Email windows (no SMS). SMS window is a join ping only.
            sms_hours = max(REM_SMS_MINUTES, 1.0) / 60.0
            jobs: list[tuple[str, str, str]] = []  # kind, flag, channel
            if hours_until <= REM_24H_HOURS and hours_until > REM_1H_HOURS + 0.05:
                if props.get("rem24") != "1":
                    jobs.append(("24h", "rem24", "email"))
            if hours_until <= REM_1H_HOURS + 0.05 and hours_until > sms_hours:
                if props.get("rem1") != "1":
                    jobs.append(("1h", "rem1", "email"))
            if hours_until <= sms_hours:
                if props.get("remsms") != "1":
                    jobs.append(
                        ("30m" if REM_SMS_MINUTES >= 22 else "15m", "remsms", "sms")
                    )

            for kind, flag, channel in jobs:
                entry: dict[str, Any] = {
                    "at": now.isoformat(),
                    "kind": kind,
                    "channel": channel,
                    "event_id": event_id,
                    "calendar": cid,
                    "email": email,
                    "hours_until": round(hours_until, 2),
                    "dry_run": dry_run,
                }
                if dry_run:
                    entry["ok"] = True
                    entry["would_send"] = True
                    results.append(entry)
                    append_log("reminders.jsonl", entry)
                    continue

                mail: dict[str, Any] = {"ok": False, "skipped": True, "detail": "not this channel"}
                sms: dict[str, Any] = {"ok": False, "skipped": True, "detail": "not this channel"}
                if channel == "email" and email:
                    mail = patient_reminder_email(
                        name=name,
                        email=email,
                        title=title,
                        when_label=when_label,
                        kind=kind,
                        cancel_url=cancel_url,
                        rebook_url=rebook_url,
                        duration_minutes=duration_minutes,
                        start_iso=start.isoformat(),
                        timezone=str(tz),
                        price_label=price_label,
                        location_line="Video visit (telehealth) · https://meet.drmattbrown.com",
                        ics_url=ics_url,
                    )
                if channel == "sms":
                    sms = patient_reminder_sms(
                        phone=phone,
                        title=title,
                        when_label=when_label,
                        kind=kind,
                        sms_consent=sms_consent,
                        duration_minutes=duration_minutes,
                    )
                entry["email_result"] = mail
                entry["sms_result"] = sms
                entry["ok"] = bool(mail.get("ok") or sms.get("ok"))

                # Mark flag only if at least email attempted successfully
                # (or SMS sent). If both fail, retry next run.
                if mail.get("ok") or sms.get("ok"):
                    new_props = dict(props)
                    new_props[flag] = "1"
                    try:
                        patch_event(cid, event_id, private_props=new_props)
                        entry["flag_set"] = flag
                    except Exception as e:
                        entry["flag_error"] = str(e)
                        append_log(
                            "notify-failures.jsonl",
                            {
                                "ok": False,
                                "channel": "gcal_flag",
                                "event_id": event_id,
                                "detail": str(e),
                            },
                        )
                else:
                    append_log(
                        "notify-failures.jsonl",
                        {
                            "ok": False,
                            "channel": "reminder",
                            "kind": kind,
                            "event_id": event_id,
                            "email": mail,
                            "sms": sms,
                        },
                    )

                results.append(entry)
                append_log("reminders.jsonl", entry)

    summary = {
        "ok": True,
        "processed": len(results),
        "results": results,
        "now": now.isoformat(),
        "calendars": list(cal_ids),
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    out = process_reminders(dry_run=dry)
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
