from __future__ import annotations

import json
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request as UrlRequest
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field

from .config import DEFAULT_CALENDAR, get_event_type, load_config
from .cal import create_event, delete_event, get_event, list_booker_events, list_events, patch_event, using_caldav
from .notify import (
    ENABLE_SMS,
    parse_email_list,
    patient_confirm_email,
    patient_confirm_sms,
    send_late,
    send_named_email,
    staff_notify_email,
)
from .reminders import process_reminders
from .slots import generate_slots
from .patients import client_by_email, client_by_id, search_clients, supabase_ready, verify_patient
from .week_people import merge_client_hits, people_from_visits
from .staff_auth import COOKIE as STAFF_COOKIE, secret_from_request
from .tokens import mint_cancel_token, mint_verify_token, verify_cancel_token, verify_patient_token

def _seed_caldav_bg() -> None:
    try:
        from .cal import seed_from_google, using_caldav

        if using_caldav():
            result = seed_from_google()
            print("caldav_seed", result, flush=True)
    except Exception as e:
        print("caldav_seed_fail", e, flush=True)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    if os.environ.get("CALDAV_SEED_GOOGLE", "1").strip().lower() in ("1", "true", "yes"):
        import threading

        threading.Thread(target=_seed_caldav_bg, name="caldav-seed", daemon=True).start()
    yield


app = FastAPI(title="Psych Arts Booker", version="0.3.62", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://book.psycharts.org",
        "https://live.psycharts.org",
        "https://site.psycharts.org",
        "https://drmattbrown.com",
        "https://www.drmattbrown.com",
        "https://portal.drmattbrown.com",
        "http://127.0.0.1:8090",
        "http://localhost:8090",
        "http://127.0.0.1:7882",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC = Path(__file__).resolve().parent.parent / "static"
if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "https://book.psycharts.org").rstrip("/")
REMIND_SECRET = os.environ.get("REMIND_CRON_SECRET", "")
SITE_PUBLIC = os.environ.get("SITE_PUBLIC_URL", "https://site.psycharts.org").rstrip("/")
INTAKE_MINT_URL = os.environ.get(
    "INTAKE_MINT_URL", "http://127.0.0.1:3000/api/intake/mint"
)
INTAKE_MINT_SECRET = os.environ.get("INTAKE_MINT_SECRET", "").strip()
ICS_FEED_SECRET = os.environ.get("ICS_FEED_SECRET", "").strip()
_current_request: ContextVar[Request | None] = ContextVar("desk_request", default=None)


@app.middleware("http")
async def _bind_request(request: Request, call_next):
    token = _current_request.set(request)
    try:
        return await call_next(request)
    finally:
        _current_request.reset(token)

# Staff "send a link" catalog — forms on the site, booking on this host.
SEND_ITEMS: dict[str, dict[str, Any]] = {
    "intake": {
        "label": "Intake packet",
        "mail": "intake",
        "mint": True,
        "forms": ["personal", "consents", "card-auth"],
    },
    "personal": {
        "label": "Personal information",
        "mail": "form_share",
        "url": f"{SITE_PUBLIC}/forms/personal",
    },
    "consents": {
        "label": "Consents & policies",
        "mail": "form_share",
        "url": f"{SITE_PUBLIC}/forms/consents",
    },
    "card": {
        "label": "Payment authorization",
        "mail": "form_share",
        "url": f"{SITE_PUBLIC}/forms/card",
    },
    "roi": {
        "label": "Release of information",
        "mail": "form_share",
        "url": f"{SITE_PUBLIC}/forms/roi",
    },
    "pay": {
        "label": "Payment on file",
        "mail": "form_share",
        "url": f"{SITE_PUBLIC}/pay",
    },
}


def _prefilled_book_url(
    slug: str, *, name: str = "", email: str = "", phone: str = "", duration: int | None = None
) -> str:
    q: dict[str, str] = {}
    if name:
        q["name"] = name
    if email:
        q["email"] = email
    if phone:
        q["phone"] = phone
    if duration:
        q["duration"] = str(int(duration))
    return f"{PUBLIC_BASE}/{slug}" + (("?" + urlencode(q)) if q else "")


def _mint_intake_url(email: str, name: str = "", forms: list[str] | None = None) -> str:
    payload = {
        "secret": INTAKE_MINT_SECRET,
        "email": email,
        "name": name,
        "sendEmail": False,
        "ttlDays": 21,
        "forms": forms or ["personal", "consents", "card-auth"],
    }
    req = UrlRequest(
        INTAKE_MINT_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except HTTPError as e:
        detail = e.read().decode()[:200] if e.fp else str(e)
        raise HTTPException(502, f"intake mint failed: {detail}") from e
    except URLError as e:
        raise HTTPException(502, f"intake mint unreachable: {e.reason}") from e
    url = str((data or {}).get("url") or "")
    if not url:
        raise HTTPException(502, "intake mint returned no url")
    return url


class BookRequest(BaseModel):
    event_type: str
    start: str
    duration_minutes: int | None = None
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    phone: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=500)
    sms_consent: bool = False
    # Optional extra people to notify (comma-separated emails)
    notify_emails: str = Field(default="", max_length=400)
    source: str = Field(default="web", max_length=20)
    # Required for follow-up bookings from voice/chat
    verify_token: str = Field(default="", max_length=800)
    send_email: bool = True


class VerifyRequest(BaseModel):
    email: str = ""
    phone: str = ""
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    dob: str = ""


def _durations(et: dict[str, Any]) -> list[int]:
    d = et.get("duration_minutes", 30)
    if isinstance(d, list):
        return [int(x) for x in d]
    return [int(d)]


def _snap_half_hour(dt: datetime) -> datetime:
    """Force visit starts onto :00 or :30 in the event timezone."""
    total = dt.hour * 60 + dt.minute
    if dt.second >= 30:
        total += 1
    snapped = ((total + 15) // 30) * 30
    extra_days, snapped = divmod(snapped, 24 * 60)
    hour, minute = divmod(snapped, 60)
    out = dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if extra_days:
        out = out + timedelta(days=extra_days)
    return out


def _price_label(et: dict[str, Any], dur: int, cfg: dict[str, Any] | None = None) -> str:
    labels = et.get("price_labels") or {}
    if str(dur) in labels:
        return str(labels[str(dur)])
    if dur in labels:
        return str(labels[dur])
    by = et.get("price_by_duration") or {}
    if dur in by or str(dur) in by:
        amt = by.get(dur, by.get(str(dur)))
        if amt == 0 or amt == "0":
            return "No charge"
        return f"${amt}"
    if et.get("price_label"):
        return str(et["price_label"])
    if "price_usd" in et:
        amt = et["price_usd"]
        if amt == 0 or amt == "0":
            return "No charge"
        return f"${amt}"
    return ""


def _location_line(et: dict[str, Any], cfg: dict[str, Any]) -> str:
    loc = et.get("location") or cfg.get("location") or "Video visit (telehealth)"
    url = et.get("location_url") or cfg.get("location_url") or "https://meet.drmattbrown.com"
    return f"{loc} · {url}"


@app.get("/health")
def health():
    cfg = load_config()
    return {
        "status": "ok",
        "version": "0.3.61",
        "types": list(cfg.get("event_types", {}).keys()),
        "timezone": cfg.get("timezone"),
        "public_base": PUBLIC_BASE,
        "cancel": True,
        "reminders": True,
        "reminder_email": ["24h", "1h"],
        "reminder_sms_minutes": float(os.environ.get("REMINDER_SMS_MINUTES", "30")),
        "suite": True,
        "sms_enabled": ENABLE_SMS,
        "desk_call": bool(
            os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
            and os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
        ),
        "patient_verify": supabase_ready(),
        "clients": "sqlite",
        "calendar": os.environ.get("CALENDAR_BACKEND", "google"),
        "calendar_sot": "caldav" if using_caldav() else "google",
        "calendar_mirror": (
            os.environ.get("CALENDAR_MIRROR_GOOGLE", "").strip().lower() in ("1", "true", "yes")
            or (os.environ.get("CALENDAR_BACKEND") or "").strip().lower() == "both"
        ),
        "ics_busy": f"{PUBLIC_BASE}/cal/busy.ics",
    }


def _ics_response(body: bytes, filename: str, cache: str) -> Response:
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": cache,
        },
    )


@app.get("/cal")
def cal_subscribe_page():
    busy = f"{PUBLIC_BASE}/cal/busy.ics"
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Subscribe · Psych Arts</title>
<link rel="stylesheet" href="/static/css/booker.css"/>
</head><body class="page">
<main class="card" style="max-width:36rem;margin:3rem auto;padding:1.5rem">
<h1>Practice calendar</h1>
<p class="muted">Paste this URL into Proton Calendar → Other calendars → Add calendar from URL. Same link works in Apple or Google as “from URL”.</p>
<p><code id="u">{busy}</code></p>
<p><button type="button" id="c">Copy busy feed</button></p>
<p class="muted">This public feed is <strong>busy-only</strong> — times, no names, no notes. A titled staff feed exists behind a separate secret (not on this page).</p>
</main>
<script>
document.getElementById("c").onclick=()=>navigator.clipboard.writeText(document.getElementById("u").textContent);
</script>
</body></html>"""
    return HTMLResponse(html)


@app.get("/cal/busy.ics")
def ics_busy():
    from .ics import build_ics

    return _ics_response(
        build_ics(busy_only=True, calendar_name="Psych Arts (busy)"),
        "psycharts-busy.ics",
        "public, max-age=300",
    )


@app.get("/cal/practice.ics")
def ics_practice(token: str = Query("")):
    import hmac

    if not ICS_FEED_SECRET or not hmac.compare_digest(token, ICS_FEED_SECRET):
        raise HTTPException(status_code=404, detail="not found")
    from .ics import build_ics

    return _ics_response(
        build_ics(busy_only=False, calendar_name="Psych Arts practice"),
        "psycharts-practice.ics",
        "private, max-age=120",
    )


@app.get("/cal/visit.ics")
def ics_visit(token: str = Query("")):
    """One-event invite. Same token as cancel — patient already has it."""
    from datetime import timedelta

    from .email_templates import visit_heading
    from .ics import build_visit_ics, _as_dt
    from .tokens import verify_cancel_token

    try:
        payload = verify_cancel_token(token)
    except Exception:
        raise HTTPException(status_code=404, detail="not found") from None

    event_id = str(payload.get("e") or "")
    cal_id = str(payload.get("c") or "")
    start = _as_dt(payload.get("s"))
    end = None
    dur = payload.get("d")
    try:
        dur_i = int(dur) if dur else 0
    except (TypeError, ValueError):
        dur_i = 0
    if start and dur_i:
        end = start + timedelta(minutes=dur_i)

    ev = None
    if event_id:
        try:
            ev = get_event(cal_id, event_id)
        except Exception:
            ev = None
    if ev:
        start = _as_dt((ev.get("start") or {}).get("dateTime")) or start
        end = _as_dt((ev.get("end") or {}).get("dateTime")) or end
        # Prefer client-facing length from the token when the calendar
        # block is longer than the visit (e.g. 90 shown / 120 blocked).
        if start and dur_i:
            end = start + timedelta(minutes=dur_i)

    if not start or not end:
        raise HTTPException(status_code=404, detail="not found")

    title = str(payload.get("t") or (ev or {}).get("summary") or "Visit")
    summary = visit_heading(title, dur_i or None)
    join = "https://meet.drmattbrown.com"
    desc = f"Join: {join}"
    cancel = f"{PUBLIC_BASE}/cancel?token={token}"
    desc += f"\nCancel / reschedule: {cancel}"

    return _ics_response(
        build_visit_ics(
            uid=event_id or f"visit-{token[:16]}",
            summary=summary,
            start=start,
            end=end,
            timezone="America/Chicago",
            description=desc,
            location=join,
        ),
        "psycharts-visit.ics",
        "private, max-age=60",
    )


@app.post("/api/verify-patient")
def api_verify_patient(req: VerifyRequest):
    """Current-patient gate. Email or phone alone, or full name + DOB."""
    result = verify_patient(
        email=req.email,
        phone=req.phone,
        full_name=req.full_name,
        first_name=req.first_name,
        last_name=req.last_name,
        dob=req.dob,
    )
    if not result.get("ok"):
        return result
    try:
        result["verify_token"] = mint_verify_token(
            client_id=str(result.get("client_id") or ""),
            email=str(result.get("email") or ""),
            name=str(result.get("name") or ""),
            matched=str(result.get("matched") or ""),
        )
    except Exception:
        result["verify_token"] = ""
    return result


@app.get("/api/types")
def api_types():
    cfg = load_config()
    out = []
    for slug, et in cfg.get("event_types", {}).items():
        if et.get("enabled") is False:
            continue
        if et.get("public") is False:
            continue
        durs = _durations(et)
        prices = {str(d): _price_label(et, d, cfg) for d in durs}
        out.append(
            {
                "slug": slug,
                "title": et.get("title", slug),
                "durations": durs,
                "prices": prices,
                "location": _location_line(et, cfg),
                "path": f"/{slug}",
            }
        )
    return {"types": out}


@app.get("/api/slots")
def api_slots(
    type: str = Query(..., alias="type"),
    duration: int | None = None,
):
    cfg = load_config()
    et = get_event_type(cfg, type)
    if not et:
        raise HTTPException(404, "Unknown or disabled event type")
    durs = _durations(et)
    dur = int(duration or durs[0])
    if dur not in durs:
        raise HTTPException(400, f"duration must be one of {durs}")
    slots = generate_slots(cfg, et, dur)
    return {
        "event_type": type,
        "title": et.get("title"),
        "duration_minutes": dur,
        "timezone": cfg.get("timezone"),
        "slots": slots[:200],
        "count": len(slots),
    }


def _send_booking_notices(
    *,
    name: str,
    email: str,
    phone: str,
    title: str,
    when_label: str,
    event_type: str,
    cancel_url: str,
    notes: str,
    sms_consent: bool,
    price_label: str = "",
    location_line: str = "Video visit · https://meet.drmattbrown.com",
    extra_notify: list[str] | None = None,
    rebook_url: str = "",
    duration_minutes: int | None = None,
    start_iso: str | None = None,
    timezone: str = "America/Chicago",
    ics_url: str = "",
) -> None:
    """Run after response — Proton can take 10–60s; do not block the thanks screen."""
    patient_confirm_email(
        name=name,
        email=email,
        title=title,
        when_label=when_label,
        cancel_url=cancel_url,
        price_label=price_label,
        location_line=location_line,
        rebook_url=rebook_url,
        duration_minutes=duration_minutes,
        start_iso=start_iso,
        timezone=timezone,
        notes=notes,
        ics_url=ics_url,
    )
    patient_confirm_sms(
        phone=phone,
        title=title,
        when_label=when_label,
        sms_consent=sms_consent,
        duration_minutes=duration_minutes,
    )
    staff_notify_email(
        name=name,
        email=email,
        phone=phone,
        title=title,
        when_label=when_label,
        event_type=event_type,
        cancel_url=cancel_url,
        notes=notes,
        sms_consent=sms_consent,
        price_label=price_label,
        extra_notify=extra_notify,
    )


@app.post("/api/book")
def api_book(req: BookRequest, background_tasks: BackgroundTasks):
    cfg = load_config()
    et = get_event_type(cfg, req.event_type)
    if not et:
        raise HTTPException(404, "Unknown or disabled event type")
    durs = _durations(et)
    dur = int(req.duration_minutes or durs[0])
    if dur not in durs:
        raise HTTPException(400, f"duration must be one of {durs}")

    # SMS consent requires a phone number
    sms_consent = bool(req.sms_consent) and bool((req.phone or "").strip())
    extra_notify = parse_email_list(req.notify_emails or "", limit=4)

    # Voice/chat follow-ups must pass the current-patient gate.
    # The public /follow-up page stays open (source=web).
    src = (req.source or "web").strip().lower()
    if req.event_type == "follow-up" and src in ("voice", "chat", "agent"):
        token = (req.verify_token or "").strip()
        if not token:
            raise HTTPException(
                401,
                "Follow-up booking needs a verified current patient (email, phone, or name + date of birth).",
            )
        try:
            verify_patient_token(token)
        except ValueError as e:
            raise HTTPException(401, f"Patient verification expired or invalid: {e}") from e

    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    start = dateparser.isoparse(req.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=tz)
    else:
        start = start.astimezone(tz)
    start = _snap_half_hour(start)
    # Client-facing duration vs calendar busy length
    cal_dur = int(et.get("calendar_duration_minutes") or dur)
    if cal_dur < dur:
        cal_dur = dur
    end = start + timedelta(minutes=cal_dur)

    price_label = _price_label(et, dur, cfg)
    location_line = _location_line(et, cfg)

    staff_override = src == "desk"
    if not staff_override:
        slots = generate_slots(cfg, et, dur)
        ok = any(s["start"] == start.isoformat() for s in slots)
        if not ok:
            ok = any(
                abs((dateparser.isoparse(s["start"]) - start).total_seconds()) < 60
                for s in slots
            )
        if not ok:
            raise HTTPException(409, "That slot is no longer available. Please pick another.")

    cal_id = et.get("google_calendar_id") or DEFAULT_CALENDAR
    title = et.get("title", req.event_type)
    # Summary shows client visit length; calendar event span may be cal_dur (e.g. 120)
    if cal_dur != dur:
        summary = f"{req.name} – {title} ({dur} min visit · {cal_dur} min block)"
    else:
        summary = f"{req.name} – {title} ({dur} min)"
    when_label = start.strftime("%a %b %d · %I:%M %p").replace(" 0", " ")
    desc_lines = [
        "Booked via Psych Arts sovereign booker",
        f"Name: {req.name}",
        f"Email: {req.email}",
        f"Phone: {req.phone or '(none)'}",
        f"SMS consent: {'yes' if sms_consent else 'no'}",
        f"Type: {req.event_type}",
        f"Client duration: {dur} minutes",
        f"Calendar block: {cal_dur} minutes",
        f"Fee: {price_label or '(see practice site)'}",
        f"Location: {location_line}",
        f"When: {when_label} America/Chicago",
    ]
    if req.notes:
        desc_lines.append(f"Notes: {req.notes}")
    if extra_notify:
        desc_lines.append("Also notify: " + ", ".join(extra_notify))

    event_id = uuid.uuid4().hex
    rebook_url = f"{PUBLIC_BASE}/{req.event_type}"
    token = mint_cancel_token(
        event_id=event_id,
        calendar_id=cal_id,
        email=str(req.email),
        title=f"{title} ({dur} min)",
        when_label=when_label,
        name=req.name,
        event_type=req.event_type,
        start_iso=start.isoformat(),
        duration_minutes=dur,
    )
    cancel_url = f"{PUBLIC_BASE}/cancel?token={token}"
    ics_url = f"{PUBLIC_BASE}/cal/visit.ics?token={token}"
    desc_lines.append("")
    desc_lines.append("Cancel / reschedule:")
    desc_lines.append(cancel_url)
    desc_lines.append(f"Rebook: {rebook_url}")
    description = "\n".join(desc_lines)

    private_props = {
        "booker": "1",
        "patient_email": str(req.email),
        "patient_name": req.name,
        "patient_phone": req.phone or "",
        "sms_consent": "1" if sms_consent else "0",
        "event_type": req.event_type,
        "title": title,
        "when_label": when_label,
        "duration_minutes": str(dur),
        "price_label": price_label,
        "rem24": "0",
        "rem2": "0",
    }

    try:
        created = create_event(
            calendar_id=cal_id,
            summary=summary,
            description=description,
            start=start,
            end=end,
            timezone=str(cfg.get("timezone", "America/Chicago")),
            attendee_email=None,
            private_props=private_props,
            uid=event_id,
        )
    except Exception as e:
        raise HTTPException(502, f"Calendar write failed: {e}") from e

    if req.send_email:
        background_tasks.add_task(
            _send_booking_notices,
            name=req.name,
            email=str(req.email),
            phone=req.phone or "",
            title=title,
            when_label=when_label,
            event_type=req.event_type,
            cancel_url=cancel_url,
            notes=req.notes or "",
            sms_consent=sms_consent,
            price_label=price_label,
            location_line=location_line,
            extra_notify=extra_notify,
            rebook_url=rebook_url,
            duration_minutes=dur,
            start_iso=start.isoformat(),
            timezone=str(cfg.get("timezone", "America/Chicago")),
            ics_url=ics_url,
        )

    return {
        "ok": True,
        "event_id": event_id,
        "html_link": created.get("htmlLink"),
        "summary": summary,
        "start": start.isoformat(),
        "end": end.isoformat(),  # calendar end (may be longer than client duration)
        "client_duration_minutes": dur,
        "calendar_duration_minutes": cal_dur,
        "when_label": when_label,
        "cancel_url": cancel_url,
        "ics_url": ics_url,
        "sms_consent": sms_consent,
        "notify_emails": extra_notify,
        "price_label": price_label,
        "location": location_line,
        "email": {"queued": bool(req.send_email)},
        "message": "You're booked. A confirmation email with a cancel link is on the way.",
    }


@app.post("/api/reminders/run")
def api_reminders_run(
    secret: str = Query(default=""),
    dry_run: bool = Query(default=False),
):
    """Cron entrypoint. Protect with REMIND_CRON_SECRET when set."""
    if REMIND_SECRET and secret != REMIND_SECRET:
        raise HTTPException(401, "unauthorized")
    return process_reminders(dry_run=dry_run)


def _visit_details_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Prefer live Google event; fall back to token display fields."""
    title = (payload.get("t") or "").strip()
    when_label = (payload.get("w") or "").strip()
    name = (payload.get("n") or "").strip()
    email = (payload.get("m") or "").strip()
    try:
        ev = get_event(payload["c"], payload["e"])
        if ev.get("status") == "cancelled":
            return {
                "title": title or "Visit",
                "when_label": when_label,
                "name": name,
                "email": email,
                "gone": "1",
            }
        summary = (ev.get("summary") or title or "Visit").strip()
        start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get(
            "date"
        )
        if start and not when_label:
            try:
                dt = dateparser.isoparse(start.replace("Z", "+00:00"))
                tz = ZoneInfo("America/Chicago")
                when_label = (
                    dt.astimezone(tz)
                    .strftime("%a %b %d · %I:%M %p")
                    .replace(" 0", " ")
                )
            except Exception:
                when_label = start
        # Prefer human title from private props when present
        priv = (ev.get("extendedProperties") or {}).get("private") or {}
        if priv.get("title"):
            summary = priv.get("title") or summary
        if priv.get("when_label"):
            when_label = priv.get("when_label") or when_label
        if priv.get("patient_name"):
            name = priv.get("patient_name") or name
        return {
            "title": summary,
            "when_label": when_label,
            "name": name,
            "email": email,
            "gone": "0",
        }
    except Exception:
        return {
            "title": title or "Visit",
            "when_label": when_label,
            "name": name,
            "email": email,
            "gone": "0",
        }


@app.get("/cancel", response_class=HTMLResponse)
def cancel_page(token: str = ""):
    if not token:
        return HTMLResponse(_cancel_html(error="Missing cancel token."), status_code=400)
    try:
        payload = verify_cancel_token(token)
    except ValueError as e:
        return HTMLResponse(_cancel_html(error=str(e)), status_code=400)
    details = _visit_details_from_payload(payload)
    rebook = f"/{payload.get('et')}" if payload.get("et") else "/"
    if details.get("gone") == "1":
        return HTMLResponse(
            _cancel_html(
                done=True,
                note="This visit was already cancelled or removed.",
                title=details.get("title", ""),
                when_label=details.get("when_label", ""),
                name=details.get("name", ""),
                rebook_path=rebook,
            )
        )
    return HTMLResponse(
        _cancel_html(
            token=token,
            email=details.get("email") or payload.get("m", ""),
            ready=True,
            title=details.get("title", ""),
            when_label=details.get("when_label", ""),
            name=details.get("name", ""),
            rebook_path=rebook,
        )
    )


@app.post("/cancel", response_class=HTMLResponse)
def cancel_submit(token: str = Form(...)):
    try:
        payload = verify_cancel_token(token)
    except ValueError as e:
        return HTMLResponse(_cancel_html(error=str(e)), status_code=400)
    details = _visit_details_from_payload(payload)
    rebook = f"/{payload.get('et')}" if payload.get("et") else "/"
    try:
        delete_event(payload["c"], payload["e"])
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            return HTMLResponse(
                _cancel_html(
                    done=True,
                    note="This visit was already cancelled or removed.",
                    title=details.get("title", ""),
                    when_label=details.get("when_label", ""),
                    name=details.get("name", ""),
                    email=details.get("email", ""),
                    rebook_path=rebook,
                )
            )
        return HTMLResponse(
            _cancel_html(error=f"Could not cancel: {msg}"),
            status_code=502,
        )
    to = (details.get("email") or payload.get("m") or "").strip()
    if to:
        try:
            send_named_email(
                kind="cancel",
                to=to,
                fields={
                    "name": details.get("name") or "",
                    "email": to,
                    "title": details.get("title") or "Visit",
                    "when_label": details.get("when_label") or "",
                    "rebook_url": f"{PUBLIC_BASE}{rebook}" if rebook.startswith("/") else rebook,
                },
            )
        except Exception:
            pass
    return HTMLResponse(
        _cancel_html(
            done=True,
            note="Your visit has been cancelled. Pick a new time to reschedule.",
            title=details.get("title", ""),
            when_label=details.get("when_label", ""),
            name=details.get("name", ""),
            email=details.get("email", ""),
            rebook_path=rebook,
        )
    )


@app.post("/api/cancel")
def api_cancel(token: str = Form(...)):
    try:
        payload = verify_cancel_token(token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    try:
        delete_event(payload["c"], payload["e"])
    except Exception as e:
        msg = str(e)
        if "404" in msg or "Not Found" in msg:
            return {"ok": True, "already_cancelled": True}
        raise HTTPException(502, f"Cancel failed: {msg}") from e
    return {"ok": True, "cancelled": True}


def _cancel_html(
    *,
    token: str = "",
    email: str = "",
    ready: bool = False,
    done: bool = False,
    error: str = "",
    note: str = "",
    title: str = "",
    when_label: str = "",
    name: str = "",
    rebook_path: str = "/",
) -> str:
    def _esc(s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    visit_card = ""
    if title or when_label or name:
        rows = []
        if title:
            rows.append(
                f'<div class="cancel-row"><span class="k">Visit</span><span class="v">{_esc(title)}</span></div>'
            )
        if when_label:
            rows.append(
                f'<div class="cancel-row"><span class="k">When</span><span class="v">{_esc(when_label)} <span class="meta">America/Chicago</span></span></div>'
            )
        if name:
            rows.append(
                f'<div class="cancel-row"><span class="k">Name</span><span class="v">{_esc(name)}</span></div>'
            )
        if email:
            rows.append(
                f'<div class="cancel-row"><span class="k">Email</span><span class="v">{_esc(email)}</span></div>'
            )
        visit_card = f'<div class="cancel-visit" style="margin:12px 0 16px;padding:12px 14px;border:1px solid var(--border, #ddd);border-radius:10px">{"".join(rows)}</div>'

    rebook_href = rebook_path if rebook_path.startswith("/") else f"/{rebook_path}"
    if done:
        body = f"""
        <div class="msg ok">{_esc(note or "Cancelled.")}</div>
        {visit_card}
        <p style="margin-top:12px">To reschedule, pick a new time below.</p>
        <p style="margin-top:16px"><a class="btn" href="{_esc(rebook_href)}" style="display:inline-block;text-decoration:none">Book a new time</a></p>
        <p style="margin-top:10px"><a class="back" href="/">All booking options</a></p>
        """
    elif error:
        body = f"""
        <div class="msg err">{_esc(error)}</div>
        <p style="margin-top:16px"><a class="btn secondary" href="/" style="display:inline-block;text-decoration:none">Back to booking</a></p>
        """
    else:
        body = f"""
        <p>You are about to cancel the following visit. This removes it from the practice calendar.</p>
        <p class="meta" style="color:var(--muted);margin-top:8px">To reschedule: cancel here, then book a new time on the next screen.</p>
        {visit_card or '<p class="meta" style="color:var(--muted)">Linked email: ' + _esc(email or "(on file)") + "</p>"}
        <form method="post" action="/cancel" class="form" style="margin-top:16px">
          <input type="hidden" name="token" value="{_esc(token)}" />
          <button class="btn" type="submit">Yes, cancel this visit</button>
        </form>
        <p style="margin-top:14px"><a class="back" href="/">Keep my visit · back to booking</a></p>
        """
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Cancel visit · Psych Arts</title>
  <link rel="stylesheet" href="/static/css/booker.css" />
  <style>
    .cancel-row {{ display:flex; gap:12px; margin:6px 0; flex-wrap:wrap; }}
    .cancel-row .k {{ min-width:4.5rem; color:var(--muted,#666); font-size:0.85rem; }}
    .cancel-row .v {{ font-weight:600; }}
    .cancel-row .meta {{ font-weight:400; color:var(--muted,#666); font-size:0.85rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="brand">
      <div class="kicker">Psych Arts · Dr. Matt Brown</div>
      <h1>Cancel visit</h1>
    </div>
    <div class="card">
      {body}
    </div>
    <p class="foot">Questions: matt@drmattbrown.com · 773-312-3858</p>
  </div>
</body>
</html>"""


class MailSendRequest(BaseModel):
    kind: str
    to: EmailStr
    name: str = ""
    title: str = ""
    url: str = ""
    amount: str = ""
    form_name: str = ""
    notes: str = ""
    when_label: str = ""
    duration_minutes: int | None = None
    start_iso: str = ""
    rebook_url: str = ""
    doc_title: str = ""
    due_date: str = ""


class LateSendRequest(BaseModel):
    event_id: str
    calendar_id: str = ""
    kind: str = "late_soon"  # late_soon | late_25


def _host_secret() -> str:
    s = os.environ.get("LIVEKIT_HOST_URL_SECRET", "").strip()
    if s:
        return s
    office = os.environ.get("LIVEKIT_OFFICE_URL", "").strip()
    if "/r/" in office:
        return office.rstrip("/").split("/r/")[-1]
    return ""


def _staff_secret(secret: str = "", request: Request | None = None) -> str:
    return secret_from_request(request or _current_request.get(), secret)


def _require_staff(secret: str = "", request: Request | None = None) -> None:
    if not REMIND_SECRET or _staff_secret(secret, request) != REMIND_SECRET:
        raise HTTPException(401, "unauthorized")


def _require_staff_or_host(secret: str = "", request: Request | None = None) -> None:
    """Desk secret or LiveKit host-link secret (office page)."""
    got = _staff_secret(secret, request)
    if REMIND_SECRET and got == REMIND_SECRET:
        return
    host = _host_secret()
    if host and got == host:
        return
    raise HTTPException(401, "unauthorized")


def _set_staff_cookie(resp: Response, secret: str) -> None:
    if not secret:
        return
    resp.set_cookie(
        STAFF_COOKIE,
        secret,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
        path="/",
    )


def _event_mail_fields(ev: dict[str, Any], cid: str, tz: ZoneInfo) -> dict[str, Any]:
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    start_raw = (ev.get("start") or {}).get("dateTime") or ""
    end_raw = (ev.get("end") or {}).get("dateTime") or ""
    start = None
    end = None
    if start_raw:
        start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone(tz)
    if end_raw:
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00")).astimezone(tz)
    dur_raw = str(priv.get("duration_minutes") or "").strip()
    duration = int(dur_raw) if dur_raw.isdigit() else None
    if duration is None and start and end:
        duration = max(15, int((end - start).total_seconds() // 60))
    et = priv.get("event_type") or ""
    return {
        "event_id": ev.get("id") or "",
        "calendar_id": cid,
        "name": priv.get("patient_name") or "",
        "email": priv.get("patient_email") or "",
        "phone": priv.get("patient_phone") or "",
        "sms_consent": priv.get("sms_consent") == "1",
        "title": priv.get("title") or ev.get("summary") or "Visit",
        "event_type": et,
        "duration_minutes": duration,
        "price_label": priv.get("price_label") or "",
        "when_label": start.strftime("%a %b %d · %I:%M %p").replace(" 0", " ") if start else "",
        "start_iso": start.isoformat() if start else "",
        "end_iso": end.isoformat() if end else "",
        "rebook_url": f"{PUBLIC_BASE}/{et}" if et else PUBLIC_BASE,
        "minutes_from_start": round((datetime.now(tz) - start).total_seconds() / 60.0, 1)
        if start
        else None,
    }


@app.get("/api/desk/today")
def api_desk_today(secret: str = Query(default="")):
    """Today's booker visits for the staff desk (late buttons)."""
    _require_staff(secret)
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    now = datetime.now(tz)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day1 = day0 + timedelta(days=1)
    cal_ids = _desk_cal_ids()
    visits = []
    for cid in cal_ids:
        try:
            events = list_booker_events(cid, day0.astimezone(), day1.astimezone())
        except Exception as e:
            visits.append({"ok": False, "calendar": cid, "error": str(e)})
            continue
        for ev in events:
            if ev.get("status") == "cancelled":
                continue
            visits.append(_event_mail_fields(ev, cid, tz))
    visits = [v for v in visits if v.get("event_id")]
    visits.sort(key=lambda v: v.get("start_iso") or "")
    return {"ok": True, "now": now.isoformat(), "visits": visits}


class DeskBookRequest(BaseModel):
    event_type: str = "follow-up"
    start: str
    duration_minutes: int | None = None
    client_id: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    notes: str = ""
    send_email: bool = True


class DeskSendRequest(BaseModel):
    item: str = ""
    items: list[str] = []
    client_id: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    notes: str = ""
    duration_minutes: int | None = None
    send_email: bool = True


def _desk_week_payload(start: str = "", days: int = 7) -> dict[str, Any]:
    """Week of calendar events for the staff board (all events, not only booker)."""
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    now = datetime.now(tz)
    if start:
        try:
            day0 = datetime.strptime(start[:10], "%Y-%m-%d").replace(tzinfo=tz)
        except ValueError:
            raise HTTPException(400, "start must be YYYY-MM-DD") from None
    else:
        # week starts Monday
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day0 = day0 - timedelta(days=day0.weekday())
    days = max(1, min(int(days), 14))
    day1 = day0 + timedelta(days=days)
    cal_ids = _desk_cal_ids()
    visits = []
    t0, t1 = day0.astimezone(), day1.astimezone()
    for cid in cal_ids:
        try:
            events = list_events(cid, t0, t1)
        except Exception as e:
            visits.append({"ok": False, "calendar": cid, "error": str(e)})
            continue
        for ev in events:
            if ev.get("status") == "cancelled":
                continue
            if ev.get("start", {}).get("date") and not ev.get("start", {}).get("dateTime"):
                # all-day — still show
                fields = _event_mail_fields(ev, cid, tz)
                fields["all_day"] = True
                fields["title"] = ev.get("summary") or "Blocked"
                visits.append(fields)
                continue
            fields = _event_mail_fields(ev, cid, tz)
            if not fields.get("name"):
                fields["title"] = ev.get("summary") or fields.get("title") or "Busy"
            visits.append(fields)
    visits = [v for v in visits if v.get("event_id") or v.get("title")]
    visits.sort(key=lambda v: v.get("start_iso") or "")
    return {
        "ok": True,
        "start": day0.date().isoformat(),
        "end": (day1 - timedelta(days=1)).date().isoformat(),
        "timezone": str(tz),
        "visits": visits,
        "types": {
            k: {
                "title": v.get("title") or k,
                "durations": _durations(v) if isinstance(v, dict) else [30],
            }
            for k, v in (cfg.get("event_types") or {}).items()
            if v.get("enabled", True)
        },
    }


@app.get("/api/desk/week")
def api_desk_week(
    secret: str = Query(default=""),
    start: str = Query(default=""),
    days: int = Query(default=7),
):
    """Week of calendar events for the staff board (all events, not only booker)."""
    _require_staff(secret)
    return _desk_week_payload(start, days)


@app.get("/api/desk/clients")
def api_desk_clients(
    secret: str = Query(default=""),
    q: str = Query(default=""),
    start: str = Query(default=""),
    days: int = Query(default=7),
):
    """Find a client: phone book plus people already on this week."""
    _require_staff_or_host(secret)
    book = search_clients(q) if supabase_ready() else []
    week_people: list[dict[str, Any]] = []
    try:
        week = _desk_week_payload(start, days)
        week_people = people_from_visits(week.get("visits") or [], q)
    except HTTPException:
        raise
    except Exception:
        week_people = []
    clients = merge_client_hits(book, week_people)
    return {
        "ok": True,
        "clients": clients,
        "sources": {"book": len(book), "week": len(week_people)},
    }


class InternalCardRequest(BaseModel):
    email: str = ""
    customer_id: str = ""
    payment_method_id: str = ""
    brand: str = ""
    last4: str = ""
    exp_month: int | None = None
    exp_year: int | None = None


@app.post("/api/internal/client/card")
def api_internal_client_card(req: InternalCardRequest, secret: str = Query(default="")):
    """Site pay form writes Stripe pointers. Staff secret. No PAN. No new row."""
    from .patients import record_card_on_file

    _require_staff(secret)
    return record_card_on_file(
        email=req.email,
        customer_id=req.customer_id,
        payment_method_id=req.payment_method_id,
        brand=req.brand,
        last4=req.last4,
        exp_month=req.exp_month,
        exp_year=req.exp_year,
    )


def _desk_cal_ids() -> set[str]:
    cal_id = os.environ.get("GOOGLE_CALENDAR_ID") or DEFAULT_CALENDAR
    ids = {cal_id}
    for et in (load_config().get("event_types") or {}).values():
        if et.get("google_calendar_id"):
            ids.add(et["google_calendar_id"])
    # Local CalDAV is one practice calendar — do not re-query it once per Google id.
    if using_caldav() and ids:
        return {next(iter(ids))}
    return ids


def _visits_for_client(client: dict[str, Any], tz: ZoneInfo) -> list[dict[str, Any]]:
    now = datetime.now(tz)
    tmin = (now - timedelta(days=180)).astimezone()
    tmax = (now + timedelta(days=90)).astimezone()
    email = (client.get("email") or "").strip().lower()
    name = (client.get("name") or "").strip().lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cid in _desk_cal_ids():
        try:
            events = list_events(cid, tmin, tmax)
        except Exception:
            continue
        for ev in events:
            if ev.get("status") == "cancelled":
                continue
            fields = _event_mail_fields(ev, cid, tz)
            ev_email = (fields.get("email") or "").strip().lower()
            ev_name = (fields.get("name") or "").strip().lower()
            summary = str(ev.get("summary") or "").lower()
            hit = bool(email and ev_email == email)
            if not hit and name and ev_name == name:
                hit = True
            if not hit and name and len(name) > 4 and name in summary:
                hit = True
            eid = str(fields.get("event_id") or "")
            if not hit or not eid or eid in seen:
                continue
            seen.add(eid)
            start_iso = fields.get("start_iso") or ""
            fields["upcoming"] = bool(start_iso and start_iso >= now.isoformat())
            out.append(fields)
    out.sort(key=lambda v: v.get("start_iso") or "", reverse=True)
    return out


@app.get("/api/desk/client")
def api_desk_client(
    secret: str = Query(default=""),
    id: str = Query(default=""),
    email: str = Query(default=""),
    name: str = Query(default=""),
):
    """One client home: identity, card, visits, form catalog. Notes later."""
    _require_staff_or_host(secret)
    row = client_by_id(id) if id else None
    if not row and email:
        row = client_by_email(email)
    if not row and name.strip():
        hits = search_clients(name.strip(), limit=5)
        if len(hits) == 1:
            row = client_by_id(hits[0]["id"])
    if not row:
        # Calendar-only: still show visits for an email/name clicked from the grid
        who = (email or "").strip() or name.strip()
        if not who:
            raise HTTPException(404, "client not found")
        row = {
            "id": "",
            "name": name.strip(),
            "email": (email or "").strip().lower(),
            "phone": "",
            "dob": "",
            "status": "calendar-only",
            "card_on_file": False,
            "directory": False,
            "given_name": "",
            "family_name": "",
            "chart_url": "",
            "billing_url": "",
        }
    else:
        row = dict(row)
        row["directory"] = True
    tz = ZoneInfo(load_config().get("timezone", "America/Chicago"))
    visits = _visits_for_client(row, tz)
    if not row.get("name"):
        for v in visits:
            if v.get("name"):
                row["name"] = v["name"]
                break
    upcoming = [v for v in visits if v.get("upcoming")]
    upcoming.sort(key=lambda v: v.get("start_iso") or "")
    past = [v for v in visits if not v.get("upcoming")]
    pay_url = f"{SITE_PUBLIC}/pay"
    if row.get("email"):
        pay_url += "?" + urlencode({"email": row["email"], "name": row.get("name") or ""})
    forms = [
        {
            "id": sid,
            "label": spec.get("label") or sid,
            "status": "unknown",
            "url": spec.get("url") or f"{SITE_PUBLIC}/forms",
        }
        for sid, spec in SEND_ITEMS.items()
        if sid != "intake"
    ]
    from .drive_paths import VISIT_NOTES_FOLDER, paths_from_row

    stored = paths_from_row(row)
    vn_path = ""
    if stored.get("chart"):
        vn_path = stored["chart"].rstrip("/") + "/" + VISIT_NOTES_FOLDER
    last_note = _last_note_listing(vn_path)
    from .fees import client_books
    from .practice import load as load_practice

    last_dur = None
    for v in past[:1] + upcoming[:1]:
        if v.get("duration_minutes"):
            last_dur = v.get("duration_minutes")
            break
    role = str((load_practice() or {}).get("billing_role") or "psychiatrist")
    books = client_books(str(row.get("id") or ""), duration_minutes=last_dur, role=role)
    return {
        "ok": True,
        "client": row,
        "card": {
            "on_file": bool(row.get("card_on_file")),
            "brand": row.get("card_brand") or "",
            "last4": row.get("card_last4") or "",
            "exp_month": row.get("card_exp_month"),
            "exp_year": row.get("card_exp_year"),
        },
        "visits": {"upcoming": upcoming[:12], "past": past[:16]},
        "forms": forms,
        "last_note": last_note,
        "books": books,
        "plan": {"ok": False, "status": "none", "hint": "Light treatment plan comes after notes."},
        "files": {
            "chart": row.get("chart_url") or "",
            "billing": row.get("billing_url") or "",
            "folder_path": stored.get("folder") or "",
            "chart_path": stored.get("chart") or "",
            "billing_path": stored.get("billing") or "",
            "visit_notes_path": vn_path,
        },
        "links": {
            "pay": pay_url,
            "forms": f"{SITE_PUBLIC}/forms",
            "book": _prefilled_book_url(
                "follow-up",
                name=row.get("name") or "",
                email=row.get("email") or "",
                phone=row.get("phone") or "",
            ),
            "mailto": f"mailto:{row['email']}" if row.get("email") else "",
        },
    }


class DeskFeeRequest(BaseModel):
    client_id: str = ""
    fee_id: str = ""
    dos: str = ""
    amount: str = ""
    amount_cents: int | None = None
    kind: str = "visit"
    note: str = ""
    event_id: str = ""
    charge: bool = True
    duration_minutes: int | None = None
    notify: bool = True


def _fee_cents(req: DeskFeeRequest) -> int:
    from .fees import parse_amount

    if req.amount_cents is not None:
        return int(req.amount_cents)
    return parse_amount(req.amount)


@app.get("/api/desk/books")
def api_desk_books(secret: str = Query(default=""), month: str = Query(default="")):
    """Clinic month + year rollup. Not a fourth tile."""
    _require_staff(secret)
    from .fees import clinic_books

    return clinic_books(month=month)


@app.get("/api/desk/fees")
def api_desk_fees(secret: str = Query(default=""), client_id: str = Query(default="")):
    _require_staff_or_host(secret)
    from .fees import client_books

    if not client_id:
        raise HTTPException(400, "need a client")
    return {"ok": True, **client_books(client_id)}


@app.post("/api/desk/fees")
def api_desk_fees_create(req: DeskFeeRequest, secret: str = Query(default="")):
    """Add an owed line, optionally charge the card on file."""
    _require_staff(secret)
    from . import clients_db
    from .fees import create as fee_create, get as fee_get, set_status
    from .stripe_pay import charge_off_session, stripe_ready

    cid = (req.client_id or "").strip()
    if not cid:
        raise HTTPException(400, "need a client")
    row = clients_db.get_by_id(cid)
    if not row:
        raise HTTPException(404, "client not found")
    try:
        cents = _fee_cents(req)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    dos = (req.dos or "")[:10]
    if len(dos) != 10:
        raise HTTPException(400, "need a date of service")

    fee = None
    if req.fee_id:
        fee = fee_get(req.fee_id)
        if not fee or fee.get("client_id") != cid:
            raise HTTPException(404, "fee not found")
        if fee.get("status") == "paid":
            return {"ok": True, "fee": fee, "charged": False, "detail": "already paid"}
    else:
        fee = fee_create(
            client_id=cid,
            dos=dos,
            amount_cents=cents,
            kind=req.kind or "visit",
            note=req.note or "",
            event_id=req.event_id or "",
        )

    charged = False
    detail = ""
    if req.charge:
        if not stripe_ready():
            raise HTTPException(503, "Stripe is not on the booker")
        cust = str(row.get("stripe_customer_id") or "")
        pm = str(row.get("stripe_payment_method_id") or "")
        if not (cust and pm):
            return {
                "ok": True,
                "fee": fee,
                "charged": False,
                "detail": "On the books. No card on file — send the pay link.",
            }
        try:
            result = charge_off_session(
                customer_id=cust,
                payment_method_id=pm,
                amount_cents=int(fee["amount_cents"]),
                dos=str(fee.get("dos") or dos),
                fee_id=str(fee["id"]),
            )
        except RuntimeError as e:
            fee = set_status(str(fee["id"]), status="failed")
            raise HTTPException(402, str(e)[:200]) from e
        fee = set_status(
            str(fee["id"]),
            status="paid" if result.get("ok") else "failed",
            stripe_id=str(result.get("stripe_id") or ""),
        )
        charged = bool(result.get("ok"))
        detail = "" if charged else str(result.get("error") or "charge failed")
        if not charged:
            raise HTTPException(402, detail)

    return {"ok": True, "fee": fee, "charged": charged, "detail": detail}


@app.post("/api/desk/fees/waive")
def api_desk_fees_waive(req: DeskFeeRequest, secret: str = Query(default="")):
    _require_staff(secret)
    from .fees import get as fee_get, set_status

    fee = fee_get(req.fee_id)
    if not fee:
        raise HTTPException(404, "fee not found")
    if fee.get("status") == "paid":
        raise HTTPException(400, "already paid")
    return {"ok": True, "fee": set_status(str(fee["id"]), status="waived")}


@app.post("/api/desk/fees/no-show")
def api_desk_fees_no_show(req: DeskFeeRequest, secret: str = Query(default="")):
    """Mark a past visit as no-show: ledger row at the posted fee. Charge/invoice separately."""
    _require_staff(secret)
    from . import clients_db
    from .fees import create as fee_create, find_no_show, suggest_amount_cents
    from .notify import send_named_email
    from .practice import load as load_practice

    cid = (req.client_id or "").strip()
    if not cid:
        raise HTTPException(400, "need a client")
    row = clients_db.get_by_id(cid)
    if not row:
        raise HTTPException(404, "client not found")
    if req.event_id:
        existing = find_no_show(req.event_id)
        if existing:
            return {"ok": True, "fee": existing, "created": False, "detail": "already on the books"}
    dos = (req.dos or "")[:10]
    if len(dos) != 10:
        raise HTTPException(400, "need the visit date")
    role = str((load_practice() or {}).get("billing_role") or "psychiatrist")
    try:
        cents = _fee_cents(req) if (req.amount or req.amount_cents is not None) else suggest_amount_cents(
            req.duration_minutes, role
        )
    except ValueError:
        cents = suggest_amount_cents(req.duration_minutes, role)
    if cents <= 0:
        raise HTTPException(400, "no posted fee for this visit length")
    fee = fee_create(
        client_id=cid,
        dos=dos,
        amount_cents=cents,
        kind="no_show",
        note="no-show",
        event_id=req.event_id or "",
    )
    mailed = False
    if req.notify and row.get("email"):
        mail = send_named_email(
            kind="no_show",
            to=str(row.get("email") or ""),
            fields={
                "name": row.get("display_name") or row.get("email") or "",
                "email": row.get("email") or "",
                "title": "Visit",
                "start_iso": dos,
                "duration_minutes": req.duration_minutes or 30,
                "rebook_url": _prefilled_book_url(
                    "follow-up",
                    name=row.get("display_name") or "",
                    email=row.get("email") or "",
                    phone=row.get("phone_e164") or "",
                ),
            },
        )
        mailed = bool(mail.get("ok"))
    return {"ok": True, "fee": fee, "created": True, "mailed": mailed}


@app.post("/api/desk/fees/invoice")
def api_desk_fees_invoice(req: DeskFeeRequest, secret: str = Query(default="")):
    """One Stripe invoice for every owed line. Superbill stays per-DOS."""
    _require_staff(secret)
    from . import clients_db
    from .fees import attach_invoice, dollars, list_open
    from .notify import send_named_email
    from .stripe_pay import create_invoice, ensure_customer, stripe_ready, void_invoice

    cid = (req.client_id or "").strip()
    if not cid:
        raise HTTPException(400, "need a client")
    row = clients_db.get_by_id(cid)
    if not row:
        raise HTTPException(404, "client not found")
    if not stripe_ready():
        raise HTTPException(503, "Stripe is not on the booker")
    open_fees = list_open(cid)
    if not open_fees:
        raise HTTPException(400, "nothing owed")

    existing = {str(f.get("stripe_id") or "") for f in open_fees}
    existing.discard("")
    urls = {str(f.get("pay_url") or "") for f in open_fees}
    urls.discard("")
    if len(existing) == 1 and len(urls) == 1 and next(iter(existing)).startswith("in_"):
        url = next(iter(urls))
        return {
            "ok": True,
            "pay_url": url,
            "stripe_id": next(iter(existing)),
            "count": len(open_fees),
            "amount_pretty": dollars(sum(int(f["amount_cents"]) for f in open_fees)),
            "reused": True,
        }

    for sid in existing:
        if sid.startswith("in_"):
            void_invoice(sid)

    try:
        cust = ensure_customer(
            email=str(row.get("email") or ""),
            existing_id=str(row.get("stripe_customer_id") or ""),
            client_id=cid,
        )
    except RuntimeError as e:
        raise HTTPException(400, str(e)[:200]) from e
    if cust and cust != row.get("stripe_customer_id"):
        clients_db.upsert({"id": cid, "stripe_customer_id": cust})

    try:
        inv = create_invoice(customer_id=cust, lines=open_fees, client_id=cid)
    except RuntimeError as e:
        raise HTTPException(502, str(e)[:200]) from e
    attach_invoice(
        [str(f["id"]) for f in open_fees],
        stripe_id=str(inv["stripe_id"]),
        pay_url=str(inv.get("pay_url") or ""),
    )
    url = str(inv.get("pay_url") or "")
    if row.get("email") and url:
        send_named_email(
            kind="payment_due",
            to=str(row.get("email") or ""),
            fields={
                "name": row.get("display_name") or row.get("email") or "",
                "email": row.get("email") or "",
                "amount": dollars(int(inv.get("amount_cents") or 0)),
                "title": f"{len(open_fees)} visit" + ("s" if len(open_fees) != 1 else ""),
                "url": url,
            },
        )
    return {
        "ok": True,
        "pay_url": url,
        "stripe_id": inv.get("stripe_id"),
        "count": len(open_fees),
        "amount_pretty": dollars(int(inv.get("amount_cents") or 0)),
        "reused": False,
    }


@app.post("/api/stripe/webhook")
async def api_stripe_webhook(request: Request):
    """Mark ledger paid when a Stripe invoice is paid. No staff secret."""
    from .fees import mark_paid_by_stripe
    from .stripe_pay import verify_webhook

    raw = await request.body()
    sig = request.headers.get("Stripe-Signature") or ""
    try:
        event = verify_webhook(raw, sig)
    except RuntimeError as e:
        raise HTTPException(400, str(e)[:160]) from e
    et = str(event.get("type") or "")
    obj = (event.get("data") or {}).get("object") or {}
    if et in ("invoice.paid", "invoice.payment_succeeded"):
        iid = str(obj.get("id") or "")
        n = mark_paid_by_stripe(iid)
        return {"ok": True, "marked": n, "type": et}
    return {"ok": True, "ignored": et}


@app.get("/api/desk/client/drive")
def api_desk_client_drive(
    secret: str = Query(default=""),
    id: str = Query(default=""),
    email: str = Query(default=""),
):
    """Map a directory client onto an existing /Client files folder. No new names."""
    _require_staff_or_host(secret)
    row = client_by_id(id) if id else None
    if not row and email:
        row = client_by_email(email)
    if not row:
        return {
            "ok": True,
            "folder_path": "",
            "chart_path": "",
            "billing_path": "",
        }
    files = _client_files_payload(row, confirm=True)
    return {
        "ok": True,
        "folder_path": files.get("folder_path") or "",
        "chart_path": files.get("chart_path") or "",
        "billing_path": files.get("billing_path") or "",
    }


class DeskNoteSignRequest(BaseModel):
    client_id: str = ""
    email: str = ""
    event_id: str = ""
    dos: str = ""
    name: str = ""
    dob: str = ""
    physician: str = ""
    return_visit: str = ""
    medications: str = ""
    supplements: str = ""
    recommendations: str = ""
    long_run: str = ""
    template: str = "soap"
    data: str = ""
    subjective: str = ""
    objective: str = ""
    assessment: str = ""
    plan: str = ""
    addendum: str = ""
    duration: str = ""
    values: dict[str, str] = {}


@app.post("/api/desk/note/sign")
def api_desk_note_sign(req: DeskNoteSignRequest, secret: str = Query(default="")):
    """Sign an after-visit summary to Chart/Visit notes. Body never logged."""
    import base64

    from .drive_paths import LIVING_ROOT, VISIT_NOTES_FOLDER, person_folder_name, paths_from_row, resolve_client_paths
    from .note_pdf import after_visit_pdf, markdown_pdf
    from .note_templates import get_template, render_template

    _require_staff(secret)
    row = client_by_id(req.client_id) if req.client_id else None
    if not row and req.email:
        row = client_by_email(req.email)
    if not row:
        raise HTTPException(404, "client not found")
    stored = paths_from_row(row)
    chart = stored.get("chart") or ""
    if not chart:
        try:
            chart = resolve_client_paths(row, _drive_list).get("chart") or ""
        except Exception:
            chart = ""
    if not chart:
        dest = person_folder_name(row)
        if dest:
            chart = f"{LIVING_ROOT}/{dest}/Chart"
    if not chart:
        raise HTTPException(400, "no chart folder for this client")
    vn = chart.rstrip("/") + "/" + VISIT_NOTES_FOLDER
    _drive_mkdir(vn)
    dos = (req.dos or "").strip()[:10]
    if len(dos) != 10:
        dos = datetime.now().date().isoformat()
    given = (row.get("given_name") or "").strip()
    family = (row.get("family_name") or "").strip()
    ini = ((given[:1] + family[:1]).upper()) if given and family else ""
    tid = (req.template or "avs").strip().lower()
    if not get_template(tid):
        tid = "avs"
    slug = "avs" if tid == "avs" else tid
    fname = f"{dos} {ini} {slug}.pdf" if ini else f"{dos} {slug}.pdf"
    dest = vn + "/" + fname
    fields = {
        "name": req.name or row.get("name") or "",
        "dob": req.dob or row.get("dob") or "",
        "physician": req.physician or "",
        "dos": dos,
        "return_visit": req.return_visit,
        "medications": req.medications,
        "supplements": req.supplements,
        "recommendations": req.recommendations,
        "long_run": req.long_run,
        "data": req.data,
        "subjective": req.subjective,
        "objective": req.objective,
        "assessment": req.assessment,
        "plan": req.plan,
        "addendum": req.addendum,
        "addendum_date": datetime.now().date().isoformat(),
        "duration": req.duration,
    }
    spec = get_template(tid) or {}
    for k, v in (req.values or {}).items():
        if isinstance(k, str) and k.isidentifier():
            fields[k] = str(v or "")[:8000]
    from .practice import load as load_practice

    prac = load_practice()
    if not fields["physician"]:
        fields["physician"] = prac.get("clinician_name") or "Matthew Brown, D.O."
    letterhead = {
        "name": prac.get("display_name") or "",
        "address": prac.get("address") or "",
        "phone": prac.get("phone") or "",
        "fax": prac.get("fax") or "",
        "clinician": prac.get("clinician_name") or "",
        "npi": prac.get("npi") or "",
    }
    sections = spec.get("sections") or []
    if sections:
        pdf = after_visit_pdf(
            fields, sections, title=str(spec.get("title") or ""), letterhead=letterhead
        )
    elif tid == "avs":
        pdf = after_visit_pdf(fields, letterhead=letterhead)
    else:
        pdf = markdown_pdf(render_template(tid, fields))
    from .notify import NOTIFY_TOKEN

    payload = json.dumps({"path": dest, "b64": base64.b64encode(pdf).decode("ascii")}).encode()
    r = UrlRequest(
        _notify_base() + "/drive/save",
        data=payload,
        headers={"Content-Type": "application/json", "X-Notify-Token": NOTIFY_TOKEN},
        method="POST",
    )
    try:
        with urlopen(r, timeout=180) as resp:
            saved = json.loads(resp.read().decode() or "{}")
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e
    if not saved.get("ok"):
        raise HTTPException(502, "drive save failed")
    return {"ok": True, "path": dest, "name": fname, "bytes": len(pdf), "template": tid}


@app.get("/api/desk/note/templates")
def api_desk_note_templates(secret: str = Query(default=""), id: str = Query(default="")):
    _require_staff(secret)
    from .note_templates import get_template, list_templates

    if id:
        spec = get_template(id)
        if not spec:
            raise HTTPException(404, "unknown template")
        return {"ok": True, **spec}
    return {"ok": True, "templates": list_templates()}


class DeskNoteTemplateSave(BaseModel):
    id: str
    title: str = ""
    body: str = ""
    sections: list[dict] = []


@app.post("/api/desk/note/templates")
def api_desk_note_template_save(req: DeskNoteTemplateSave, secret: str = Query(default="")):
    _require_staff(secret)
    from .note_templates import save_template

    try:
        return {
            "ok": True,
            **save_template(req.id, req.body, title=req.title, sections=req.sections or None),
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class DeskNoteTemplateOrder(BaseModel):
    ids: list[str] = []


@app.post("/api/desk/note/templates/order")
def api_desk_note_template_order(req: DeskNoteTemplateOrder, secret: str = Query(default="")):
    _require_staff(secret)
    from .note_templates import save_order

    return {"ok": True, "ids": save_order(req.ids)}


@app.delete("/api/desk/note/templates")
def api_desk_note_template_delete(secret: str = Query(default=""), id: str = Query(default="")):
    _require_staff(secret)
    from .note_templates import delete_template

    try:
        delete_template(id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@app.get("/api/desk/phrases")
def api_desk_phrases(secret: str = Query(default=""), id: str = Query(default="")):
    _require_staff(secret)
    from .dot_phrases import get_phrase, list_phrases

    if id:
        spec = get_phrase(id)
        if not spec:
            raise HTTPException(404, "unknown phrase")
        return {"ok": True, **spec}
    return {"ok": True, "phrases": list_phrases()}


class DeskPhraseSave(BaseModel):
    trigger: str = ""
    id: str = ""
    title: str = ""
    body: str = ""


@app.post("/api/desk/phrases")
def api_desk_phrase_save(req: DeskPhraseSave, secret: str = Query(default="")):
    _require_staff(secret)
    from .dot_phrases import save_phrase

    try:
        return {
            "ok": True,
            **save_phrase(req.trigger or req.id, req.body, title=req.title),
        }
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class DeskPhraseOrder(BaseModel):
    ids: list[str] = []


@app.post("/api/desk/phrases/order")
def api_desk_phrase_order(req: DeskPhraseOrder, secret: str = Query(default="")):
    _require_staff(secret)
    from .dot_phrases import save_order

    return {"ok": True, "ids": save_order(req.ids)}


@app.delete("/api/desk/phrases")
def api_desk_phrase_delete(secret: str = Query(default=""), id: str = Query(default="")):
    _require_staff(secret)
    from .dot_phrases import delete_phrase

    try:
        delete_phrase(id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


class DeskNoteDraftRequest(BaseModel):
    transcript: str = ""
    name: str = ""
    dos: str = ""
    duration: str = ""


@app.post("/api/desk/note/draft")
def api_desk_note_draft(req: DeskNoteDraftRequest, secret: str = Query(default="")):
    """SOAP + AVS from a transcript. Body never logged."""
    _require_staff(secret)
    raw = (req.transcript or "").strip()
    if len(raw) < 20:
        raise HTTPException(400, "need a bit more transcript")
    from .desk_agent import XAI_KEY, XAI_MODEL, XAI_URL

    if not XAI_KEY:
        raise HTTPException(503, "desk agent key unset")
    prompt = (
        "From this visit transcript, fill a SOAP note and an after-visit summary. "
        "Do not invent facts. JSON only with keys: subjective, objective, assessment, "
        "plan, return_visit, medications, supplements, recommendations. "
        "AVS fields are the Plan in patient language.\n\n"
        + raw[:12000]
    )
    payload = json.dumps(
        {
            "model": XAI_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "You draft psychiatry notes. Reply with JSON only. No markdown fence.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
    ).encode()
    http_req = UrlRequest(
        XAI_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + XAI_KEY},
        method="POST",
    )
    try:
        with urlopen(http_req, timeout=90) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except HTTPError as e:
        raise HTTPException(502, f"draft failed {e.code}") from e
    text = ""
    try:
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception:
        text = ""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"plan": text[:2000]}
    if not isinstance(parsed, dict):
        parsed = {}
    keys = (
        "subjective",
        "objective",
        "assessment",
        "plan",
        "return_visit",
        "medications",
        "supplements",
        "recommendations",
    )
    out = {k: str(parsed.get(k) or "")[:8000] for k in keys}
    return {"ok": True, "fields": out}


@app.post("/api/desk/book")
def api_desk_book(req: DeskBookRequest, background_tasks: BackgroundTasks, secret: str = Query(default="")):
    """Staff book anyone, any time. Fills from Supabase when client_id is set."""
    _require_staff(secret)
    name, email, phone = req.name.strip(), req.email.strip(), req.phone.strip()
    if req.client_id:
        row = client_by_id(req.client_id)
        if not row:
            raise HTTPException(404, "client not found")
        name = name or row["name"]
        email = email or row["email"]
        phone = phone or row["phone"]
    if not name or not email:
        raise HTTPException(400, "need name and email (pick a client or type them)")
    book = BookRequest(
        event_type=req.event_type,
        start=req.start,
        duration_minutes=req.duration_minutes,
        name=name,
        email=email,
        phone=phone,
        notes=req.notes,
        source="desk",
        send_email=req.send_email,
    )
    return api_book(book, background_tasks)


class DeskAgentRequest(BaseModel):
    messages: list[dict[str, str]] = []


@app.get("/api/practice")
def api_practice_public():
    """Public letterhead. No PHI. Site, portal, and booker pages read this."""
    from .practice import public as public_practice

    return public_practice(PUBLIC_BASE)


@app.get("/api/practice/logo")
def api_practice_logo():
    from .practice import asset_bytes

    got = asset_bytes("logo")
    if not got:
        raise HTTPException(404, "no logo")
    data, ctype = got
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/practice/photo")
def api_practice_photo():
    from .practice import asset_bytes

    got = asset_bytes("photo")
    if not got:
        raise HTTPException(404, "no photo")
    data, ctype = got
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/practice/background")
def api_practice_background(page: str = Query(default="home")):
    from .practice import PAGE_SLUGS, asset_bytes

    slug = (page or "home").strip().lower()
    if slug not in PAGE_SLUGS:
        raise HTTPException(404, "unknown page")
    got = asset_bytes(f"bg-{slug}") or (asset_bytes("background") if slug == "home" else None)
    if not got:
        raise HTTPException(404, "no background")
    data, ctype = got
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/desk/practice")
def api_desk_practice_get(secret: str = Query(default="")):
    _require_staff(secret)
    from .desk_call import config as call_config
    from .practice import load as load_practice, pages_labeled, public as public_practice, service_set

    p = load_practice()
    pub = public_practice(PUBLIC_BASE)
    return {
        "ok": True,
        **p,
        **{k: pub[k] for k in ("logo_url", "photo_url", "backgrounds", "has_logo", "has_photo", "theme", "page_titles")},
        "services": service_set(str(p.get("billing_role") or "")),
        "pages": [{"id": a, "label": b} for a, b in pages_labeled(p.get("page_titles"))],
        "call": call_config(),
    }


class DeskPracticeSave(BaseModel):
    display_name: str = ""
    legal_name: str = ""
    clinician_name: str = ""
    clinician_title: str = ""
    address: str = ""
    phone: str = ""
    fax: str = ""
    email: str = ""
    website: str = ""
    meet: str = ""
    ein: str = ""
    npi: str = ""
    licenses: list[dict] = []
    billing_role: str = ""
    theme: dict = {}
    page_titles: dict = {}
    clinician_callback: str = ""
    call_handset: str = ""


@app.post("/api/desk/practice")
def api_desk_practice_save(req: DeskPracticeSave, secret: str = Query(default="")):
    _require_staff(secret)
    from .practice import public as public_practice, save as save_practice

    saved = save_practice(req.model_dump())
    from .practice import service_set

    pub = public_practice(PUBLIC_BASE)
    return {
        "ok": True,
        **saved,
        **{k: pub[k] for k in ("logo_url", "photo_url", "backgrounds", "has_logo", "has_photo", "theme", "page_titles")},
        "services": service_set(str(saved.get("billing_role") or "")),
    }


@app.get("/api/desk/services")
def api_desk_services_get(secret: str = Query(default="")):
    _require_staff(secret)
    from .event_store import list_types

    return {"ok": True, "services": list_types(load_config()), "days": [
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
    ]}


class DeskServiceSave(BaseModel):
    slug: str = ""
    title: str = ""
    enabled: bool = True
    public: bool = True
    duration_minutes: Any = None
    calendar_duration_minutes: int | None = None
    price_usd: float | None = None
    price_by_duration: dict | None = None
    weekly_hours: dict | None = None
    slot_interval_minutes: int = 30
    minimum_notice_minutes: int = 120
    booking_window_days: int = 30
    creating: bool = False


@app.post("/api/desk/services")
def api_desk_services_save(req: DeskServiceSave, secret: str = Query(default="")):
    _require_staff(secret)
    from .event_store import list_types, save_type

    try:
        saved = save_type(load_config(), req.slug, req.model_dump(), creating=req.creating)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "service": saved, "services": list_types(load_config())}


@app.delete("/api/desk/services")
def api_desk_services_delete(secret: str = Query(default=""), slug: str = Query(default="")):
    _require_staff(secret)
    from .event_store import delete_type, list_types

    if not slug:
        raise HTTPException(400, "need a slug")
    delete_type(load_config(), slug)
    return {"ok": True, "services": list_types(load_config())}


@app.post("/api/desk/practice/asset")
async def api_desk_practice_asset(
    secret: str = Query(default=""),
    kind: str = Query(default="logo"),
    file: UploadFile = File(...),
):
    _require_staff(secret)
    from .practice import public as public_practice, save_asset

    raw = await file.read()
    try:
        saved = save_asset(kind, raw, file.content_type or "")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    pub = public_practice(PUBLIC_BASE)
    return {
        "ok": True,
        **saved,
        **{k: pub[k] for k in ("logo_url", "photo_url", "backgrounds", "has_logo", "has_photo")},
    }


class DeskCallRequest(BaseModel):
    to: str = ""
    client_id: str = ""
    name: str = ""
    sid: str = ""
    handset: str = ""


@app.get("/api/desk/call")
def api_desk_call_get(secret: str = Query(default=""), sid: str = Query(default="")):
    _require_staff_or_host(secret)
    from .desk_call import call_status, config as call_config

    if sid:
        try:
            return call_status(sid)
        except RuntimeError as e:
            raise HTTPException(502, str(e)[:200]) from e
    return call_config()


@app.post("/api/desk/call")
def api_desk_call_place(req: DeskCallRequest, secret: str = Query(default="")):
    _require_staff_or_host(secret)
    from .desk_call import place_call

    try:
        out = place_call(to=req.to, client_id=req.client_id, name=req.name, handset=req.handset)
    except RuntimeError as e:
        raise HTTPException(502, str(e)[:200]) from e
    if not out.get("ok"):
        raise HTTPException(400, str(out.get("error") or "could not place call"))
    return out


@app.post("/api/desk/call/hangup")
def api_desk_call_hangup(req: DeskCallRequest, secret: str = Query(default="")):
    _require_staff_or_host(secret)
    from .desk_call import hangup_call

    try:
        out = hangup_call(req.sid)
    except RuntimeError as e:
        raise HTTPException(502, str(e)[:200]) from e
    if not out.get("ok"):
        raise HTTPException(400, str(out.get("error") or "could not hang up"))
    return out


@app.post("/api/twilio/desk-voice")
async def api_twilio_desk_voice(request: Request):
    """TwiML for desktop handset. Destination comes from a one-time nonce, not the browser."""
    from .desk_call import browser_twiml, reject_twiml, take_pending, valid_twilio_signature

    form = {str(k): str(v) for k, v in (await request.form()).items()}
    url = PUBLIC_BASE + "/api/twilio/desk-voice"
    sig = request.headers.get("X-Twilio-Signature") or ""
    if not valid_twilio_signature(url, form, sig):
        raise HTTPException(403, "bad signature")
    nonce = (form.get("nonce") or form.get("CallNonce") or "").strip()
    rec = take_pending(nonce)
    if not rec:
        return Response(content=reject_twiml("That call expired."), media_type="text/xml")
    dest = str(rec.get("to") or "")
    did = str(rec.get("from") or "")
    if not dest or not did:
        return Response(content=reject_twiml("Call cannot be completed."), media_type="text/xml")
    return Response(content=browser_twiml(dest, did), media_type="text/xml")


@app.get("/api/desk/meta")
def api_desk_meta(secret: str = Query(default="")):
    """Staff links — office + guest waiting room."""
    _require_staff(secret)
    resp = {
        "ok": True,
        "office_url": os.environ.get("LIVEKIT_OFFICE_URL", "").strip(),
        "guest_url": os.environ.get(
            "LIVEKIT_GUEST_URL", "https://live.psycharts.org/guest"
        ).strip(),
        "agent": True,
        "drive": True,
        "practice": True,
        "superbill": True,
    }
    out = Response(
        content=json.dumps(resp),
        media_type="application/json",
    )
    got = _staff_secret(secret)
    if REMIND_SECRET and got == REMIND_SECRET:
        _set_staff_cookie(out, got)
    return out


class DeskSuperbillRequest(BaseModel):
    client_id: str = ""
    email: str = ""
    dos: str = ""
    pos: str = "10"
    address: str = ""
    diagnoses: list[dict] = []
    lines: list[dict] = []


@app.post("/api/desk/superbill")
def api_desk_superbill(req: DeskSuperbillRequest, secret: str = Query(default="")):
    """Write a Superbill PDF to Billing. Body never logged."""
    import base64

    from .drive_paths import BILLING_CHILD, LIVING_ROOT, person_folder_name, paths_from_row, resolve_client_paths
    from .practice import format_licenses, load as load_practice, next_statement
    from .superbill_pdf import superbill_filename, superbill_pdf

    _require_staff(secret)
    row = client_by_id(req.client_id) if req.client_id else None
    if not row and req.email:
        row = client_by_email(req.email)
    if not row:
        raise HTTPException(404, "client not found")
    stored = paths_from_row(row)
    billing = stored.get("billing") or ""
    if not billing:
        try:
            billing = resolve_client_paths(row, _drive_list).get("billing") or ""
        except Exception:
            billing = ""
    if not billing:
        dest = person_folder_name(row)
        if dest:
            billing = f"{LIVING_ROOT}/{dest}/{BILLING_CHILD}"
    if not billing:
        raise HTTPException(400, "no billing folder for this client")
    _drive_mkdir(billing)
    dos = (req.dos or "").strip()[:10]
    if len(dos) != 10:
        dos = datetime.now().date().isoformat()
    prac = load_practice()
    statement = next_statement()
    diagnoses = []
    for item in (req.diagnoses or [])[:12]:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()[:16]
        label = str(item.get("label") or "").strip()[:160]
        if code or label:
            diagnoses.append({"code": code, "label": label})
    lines = []
    for item in (req.lines or [])[:20]:
        if not isinstance(item, dict):
            continue
        lines.append(
            {
                "dos": str(item.get("dos") or dos)[:10],
                "pos": str(item.get("pos") or req.pos or "10")[:8],
                "service": str(item.get("service") or "")[:80],
                "code": str(item.get("code") or "")[:16],
                "modifier": str(item.get("modifier") or "95")[:8],
                "dx": str(item.get("dx") or "")[:20],
                "units": str(item.get("units") or "1")[:6],
                "cost": str(item.get("cost") or "")[:16],
                "paid": str(item.get("paid") or "")[:16],
            }
        )
    if not lines:
        raise HTTPException(400, "add at least one service line")
    fname = superbill_filename(dos)
    dest = billing.rstrip("/") + "/" + fname
    pdf = superbill_pdf(
        {
            "statement": statement,
            "issued": datetime.now().date().isoformat(),
            "dos": dos,
            "practice": {
                "name": prac.get("display_name") or "",
                "address": prac.get("address") or "",
                "ein": prac.get("ein") or "",
                "npi": prac.get("npi") or "",
            },
            "provider": {
                "name": prac.get("clinician_name") or "",
                "email": prac.get("email") or "",
                "phone": prac.get("phone") or "",
                "npi": prac.get("npi") or "",
            },
            "client": {
                "name": row.get("name") or "",
                "email": row.get("email") or "",
                "phone": row.get("phone") or "",
                "address": (req.address or "").strip()[:200],
                "dob": row.get("dob") or "",
            },
            "licenses": format_licenses(prac.get("licenses") or []),
            "diagnoses": diagnoses,
            "lines": lines,
        }
    )
    from .notify import NOTIFY_TOKEN

    payload = json.dumps({"path": dest, "b64": base64.b64encode(pdf).decode("ascii")}).encode()
    r = UrlRequest(
        _notify_base() + "/drive/save",
        data=payload,
        headers={"Content-Type": "application/json", "X-Notify-Token": NOTIFY_TOKEN},
        method="POST",
    )
    try:
        with urlopen(r, timeout=180) as resp:
            saved = json.loads(resp.read().decode() or "{}")
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e
    if not saved.get("ok"):
        raise HTTPException(502, "drive save failed")
    return {"ok": True, "path": dest, "name": fname, "statement": statement, "bytes": len(pdf)}


def _last_note_listing(vn_path: str) -> dict[str, Any]:
    if not vn_path:
        return {
            "ok": False,
            "status": "none",
            "hint": "After-visit summary will land in Chart / Visit notes.",
        }
    try:
        data = _drive_list(vn_path)
    except Exception:
        data = {}
    items = [
        it
        for it in (data.get("items") or [])
        if (it.get("type") or "") == "file" and (it.get("name") or "")
    ]
    if not items:
        return {
            "ok": False,
            "status": "empty",
            "path": vn_path,
            "hint": "No after-visit summary in Visit notes yet.",
        }
    items.sort(key=lambda it: int(it.get("modify_time") or 0), reverse=True)
    latest = items[0]
    name = latest.get("name") or ""
    return {
        "ok": True,
        "status": "file",
        "name": name,
        "path": vn_path + "/" + name,
        "hint": name,
        "count": len(items),
    }


def _drive_mkdir(path: str) -> None:
    from .notify import NOTIFY_TOKEN

    payload = json.dumps({"path": path}).encode()
    req = UrlRequest(
        _notify_base() + "/drive/mkdir",
        data=payload,
        headers={"Content-Type": "application/json", "X-Notify-Token": NOTIFY_TOKEN},
        method="POST",
    )
    try:
        with urlopen(req, timeout=90) as resp:
            json.loads(resp.read().decode() or "{}")
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e


def _drive_list(path: str) -> dict[str, Any]:
    code, body, _ = _notify_get("/drive/list?path=" + quote(path or "/", safe="/"))
    if code != 200:
        return {}
    try:
        return json.loads(body.decode() or "{}")
    except Exception:
        return {}


def _client_files_payload(row: dict[str, Any], *, confirm: bool = False) -> dict[str, str]:
    from .drive_paths import existing_paths, paths_from_row

    paths = {"folder": "", "chart": "", "billing": ""}
    try:
        if confirm:
            paths = existing_paths(row, _drive_list)
        else:
            paths = paths_from_row(row)
    except Exception:
        paths = {"folder": "", "chart": "", "billing": ""}
    return {
        "chart": row.get("chart_url") or "",
        "billing": row.get("billing_url") or "",
        "folder_path": paths.get("folder") or "",
        "chart_path": paths.get("chart") or "",
        "billing_path": paths.get("billing") or "",
    }


def _notify_base() -> str:
    raw = os.environ.get("NOTIFY_WEBHOOK_URL") or "http://127.0.0.1:8791/notify"
    return raw[:-7] if raw.endswith("/notify") else raw.rstrip("/")


def _notify_get(path: str) -> tuple[int, bytes, str]:
    from .notify import NOTIFY_TOKEN

    url = _notify_base() + path
    req = UrlRequest(
        url,
        headers={"X-Notify-Token": NOTIFY_TOKEN},
        method="GET",
    )
    try:
        with urlopen(req, timeout=90) as resp:
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            return resp.status, resp.read(), ctype
    except HTTPError as e:
        return e.code, e.read(), "application/json"


def _notify_post(path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    from .notify import NOTIFY_TOKEN

    data = json.dumps(payload).encode()
    req = UrlRequest(
        _notify_base() + path,
        data=data,
        headers={
            "Content-Type": "application/json",
            "X-Notify-Token": NOTIFY_TOKEN,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except Exception:
            body = {"ok": False, "error": "notify failed"}
        return e.code if e.code >= 400 else 502, body


@app.get("/api/desk/mail/setup")
def api_desk_mail_setup(secret: str = Query(default="")):
    _require_staff(secret)
    code, body, _ = _notify_get("/mail/setup")
    if code != 200:
        raise HTTPException(code if code >= 400 else 502, body.decode()[:200])
    return json.loads(body.decode() or "{}")


@app.get("/api/desk/mail/list")
def api_desk_mail_list(
    secret: str = Query(default=""),
    folder: str = Query(default="Desk"),
    unread: bool = Query(default=False),
):
    _require_staff(secret)
    q = "/mail/list?folder=" + quote(folder or "Desk", safe="")
    if unread:
        q += "&unread=1"
    code, body, _ = _notify_get(q)
    if code != 200:
        raise HTTPException(code if code >= 400 else 502, body.decode()[:200])
    return json.loads(body.decode() or "{}")


@app.get("/api/desk/mail/thread")
def api_desk_mail_thread(secret: str = Query(default=""), id: str = Query(default="")):
    _require_staff(secret)
    if not id.strip():
        raise HTTPException(400, "pick a thread")
    code, body, _ = _notify_get("/mail/thread?id=" + quote(id.strip(), safe=""))
    if code != 200:
        raise HTTPException(code if code >= 400 else 502, body.decode()[:200])
    data = json.loads(body.decode() or "{}")
    email = (data.get("from") or "").strip()
    client = None
    if email:
        row = client_by_email(email)
        if row:
            client = {"id": row.get("id") or "", "name": row.get("name") or "", "email": row.get("email") or ""}
    data["client"] = client
    return data


class MailReplyFile(BaseModel):
    name: str = "file"
    b64: str = ""


class MailReplyRequest(BaseModel):
    id: str
    body: str = ""
    files: list[MailReplyFile] = []


@app.get("/api/desk/mail/attachment")
def api_desk_mail_attachment(
    secret: str = Query(default=""),
    message: str = Query(default=""),
    id: str = Query(default=""),
    name: str = Query(default="file"),
):
    from fastapi.responses import Response

    _require_staff(secret)
    if not message.strip() or not id.strip():
        raise HTTPException(400, "pick a file")
    q = (
        "/mail/attachment?message="
        + quote(message.strip(), safe="")
        + "&id="
        + quote(id.strip(), safe="")
        + "&name="
        + quote((name or "file")[:120], safe="")
    )
    code, body, ctype = _notify_get(q)
    if code != 200:
        raise HTTPException(code if code >= 400 else 502, body.decode()[:200])
    safe = (name or "file").replace("/", "_").replace("\\", "_")[:120]
    return Response(
        content=body,
        media_type=ctype or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
    )


@app.post("/api/desk/mail/reply")
def api_desk_mail_reply(req: MailReplyRequest, secret: str = Query(default="")):
    _require_staff(secret)
    files = [{"name": f.name, "b64": f.b64} for f in (req.files or [])[:4] if f.b64]
    code, payload = _notify_post("/mail/reply", {"id": req.id, "body": req.body, "files": files})
    if code != 200 or not payload.get("ok"):
        raise HTTPException(code if code >= 400 else 502, payload.get("error") or payload.get("detail") or "reply failed")
    return payload


@app.get("/api/desk/drive/list")
def api_desk_drive_list(secret: str = Query(default=""), path: str = Query(default="/")):
    _require_staff_or_host(secret)
    code, body, _ = _notify_get("/drive/list?path=" + quote(path or "/", safe="/"))
    if code != 200:
        raise HTTPException(code if code >= 400 else 502, body.decode()[:200])
    return json.loads(body.decode() or "{}")


@app.get("/api/desk/drive/file")
def api_desk_drive_file(secret: str = Query(default=""), path: str = Query(default="")):
    from fastapi.responses import Response

    _require_staff_or_host(secret)
    if not path or path == "/":
        raise HTTPException(400, "pick a file")
    code, body, ctype = _notify_get("/drive/file?path=" + quote(path, safe="/"))
    if code != 200:
        raise HTTPException(code if code >= 400 else 502, body.decode()[:200])
    name = path.rsplit("/", 1)[-1] or "file"
    return Response(
        content=body,
        media_type=ctype or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{name}"'},
    )


class DriveSaveRequest(BaseModel):
    path: str
    text: str = ""
    b64: str = ""


@app.post("/api/desk/drive/save")
def api_desk_drive_save(req: DriveSaveRequest, secret: str = Query(default="")):
    """Overwrite a text file on Proton Drive (RAM upload)."""
    _require_staff_or_host(secret)
    from .notify import NOTIFY_TOKEN

    payload = json.dumps({"path": req.path, "text": req.text, "b64": req.b64}).encode()
    r = UrlRequest(
        _notify_base() + "/drive/save",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Notify-Token": NOTIFY_TOKEN,
        },
        method="POST",
    )
    try:
        with urlopen(r, timeout=180) as resp:
            return json.loads(resp.read().decode() or "{}")
    except HTTPError as e:
        raise HTTPException(e.code if e.code >= 400 else 502, e.read().decode()[:200]) from e


class DriveShareRequest(BaseModel):
    path: str
    client_id: str = ""
    name: str = ""
    email: str = ""
    phone: str = ""
    send_email: bool = True
    send_sms: bool = False


@app.post("/api/desk/drive/share")
def api_desk_drive_share(req: DriveShareRequest, secret: str = Query(default="")):
    """Create a Proton file share (cloak via Shlink when set) and send email and/or SMS."""
    _require_staff(secret)
    from .notify import send_named_email, send_sms
    from .patients import normalize_phone

    path = (req.path or "").strip()
    if not path or path == "/":
        raise HTTPException(400, "pick a file")
    name, email, phone = req.name.strip(), req.email.strip(), req.phone.strip()
    if req.client_id:
        row = client_by_id(req.client_id)
        if not row:
            raise HTTPException(404, "client not found")
        name = name or row.get("name") or ""
        email = email or row.get("email") or ""
        phone = phone or row.get("phone") or ""
    want_mail = bool(req.send_email)
    want_sms = bool(req.send_sms)
    if not want_mail and not want_sms:
        raise HTTPException(400, "check email, SMS, or both")
    if want_mail and not email:
        raise HTTPException(400, "need an email for that send")
    if want_sms and not (phone or normalize_phone(phone)):
        raise HTTPException(400, "need a phone for SMS")

    title = path.rsplit("/", 1)[-1] or "file"
    code, body = _notify_post("/drive/share", {"path": path, "title": title})
    if code >= 400 or not body.get("ok"):
        raise HTTPException(code if code >= 400 else 502, body.get("error") or "share failed")
    url = str(body.get("url") or "").strip()
    if not url.startswith("https://"):
        raise HTTPException(502, "share failed")
    cloaked = bool(body.get("cloaked"))

    mail: dict[str, Any] = {"ok": False, "skipped": True}
    sms: dict[str, Any] = {"ok": False, "skipped": True}
    if want_mail:
        mail = send_named_email(
            kind="document",
            to=email,
            fields={"name": name or "there", "email": email, "title": title, "doc_title": title, "url": url},
        )
    if want_sms:
        first = (name or "there").split()[0]
        sms = send_sms(phone, f"Hi {first}, a file is ready from the practice: {url} — Dr. B")

    ok = bool(mail.get("ok") or sms.get("ok"))
    return {
        "ok": ok,
        "cloaked": cloaked,
        "kind": "cloak" if cloaked else "proton",
        "email": {"ok": bool(mail.get("ok")), "skipped": bool(mail.get("skipped")), "detail": mail.get("detail") or ""},
        "sms": {"ok": bool(sms.get("ok")), "skipped": bool(sms.get("skipped")), "detail": sms.get("detail") or ""},
    }


@app.post("/api/desk/agent")
def api_desk_agent(
    req: DeskAgentRequest,
    background_tasks: BackgroundTasks,
    secret: str = Query(default=""),
):
    """Staff Grok: look up, create, send forms/mail, book."""
    _require_staff(secret)
    from .desk_agent import run_desk_agent

    def book_fn(args: dict) -> dict:
        book = BookRequest(
            event_type="follow-up",
            start=str(args.get("start") or ""),
            duration_minutes=int(args.get("duration_minutes") or 30),
            name=str(args.get("name") or ""),
            email=str(args.get("email") or ""),
            phone=str(args.get("phone") or ""),
            notes=str(args.get("notes") or ""),
            source="desk",
        )
        return api_book(book, background_tasks)

    def send_fn(args: dict) -> dict:
        items = args.get("items") or []
        if isinstance(items, str):
            items = [items]
        fake = DeskSendRequest(
            items=[str(x) for x in items],
            name=str(args.get("name") or ""),
            email=str(args.get("email") or ""),
            send_email=bool(args.get("send_email", True)),
        )
        return api_desk_send(fake, secret)

    def mail_fn(args: dict) -> dict:
        from .notify import send_named_email

        kind = str(args.get("kind") or "form_share")
        to = str(args.get("to") or "")
        fields = {
            "name": args.get("name") or "",
            "email": to,
            "title": args.get("title") or "",
            "form_name": args.get("title") or kind,
            "url": args.get("url") or "",
            "notes": args.get("notes") or "",
        }
        return send_named_email(kind=kind, to=to, fields=fields)

    try:
        return run_desk_agent(req.messages, book_fn=book_fn, send_fn=send_fn, mail_fn=mail_fn)
    except Exception as e:
        raise HTTPException(502, str(e)[:200]) from e


@app.get("/api/desk/send-items")
def api_desk_send_items(secret: str = Query(default="")):
    _require_staff(secret)
    return {
        "ok": True,
        "items": [
            {"id": k, "label": v["label"], "group": "form"}
            for k, v in SEND_ITEMS.items()
        ],
    }


def _resolve_send_ids(req: DeskSendRequest) -> list[str]:
    raw = list(req.items or [])
    if req.item:
        raw.append(req.item)
    ids: list[str] = []
    for x in raw:
        if x in SEND_ITEMS and x not in ids:
            ids.append(x)
    # Packet already includes personal / consents / card.
    if "intake" in ids:
        ids = [x for x in ids if x not in ("personal", "consents", "card")]
    return ids


def _build_send_links(ids: list[str], *, name: str, email: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for item_id in ids:
        spec = SEND_ITEMS[item_id]
        if spec.get("mint"):
            try:
                url = _mint_intake_url(email, name, spec.get("forms"))
            except HTTPException:
                url = f"{SITE_PUBLIC}/forms"
        else:
            url = str(spec.get("url") or f"{SITE_PUBLIC}/forms")
        links.append({"id": item_id, "label": spec["label"], "url": url})
    return links


@app.post("/api/desk/send")
def api_desk_send(req: DeskSendRequest, secret: str = Query(default="")):
    """Staff: email or copy one or more form links."""
    _require_staff(secret)
    ids = _resolve_send_ids(req)
    if not ids:
        raise HTTPException(400, f"pick at least one form; use {sorted(SEND_ITEMS)}")
    name, email = req.name.strip(), req.email.strip()
    if req.client_id:
        row = client_by_id(req.client_id)
        if not row:
            raise HTTPException(404, "client not found")
        name = name or row["name"]
        email = email or row["email"]
    if not email:
        raise HTTPException(400, "need an email (pick a client or type one)")

    links = _build_send_links(ids, name=name, email=email)
    labels = [x["label"] for x in links]
    label = labels[0] if len(labels) == 1 else (", ".join(labels[:-1]) + " and " + labels[-1])
    first_url = links[0]["url"]

    emailed = False
    mail: dict[str, Any] = {}
    if req.send_email:
        if len(links) == 1 and links[0]["id"] == "intake":
            kind = "intake"
        elif len(links) == 1:
            kind = "form_share"
        else:
            kind = "forms_bundle"
        fields = {
            "name": name,
            "email": email,
            "title": label,
            "form_name": label,
            "url": first_url,
            "notes": req.notes,
            "links": links,
        }
        mail = send_named_email(kind=kind, to=email, fields=fields)
        emailed = bool(mail.get("ok"))
    return {
        "ok": True,
        "item": ids[0],
        "items": ids,
        "label": label,
        "url": first_url,
        "links": links,
        "emailed": emailed,
        "email": email,
        "name": name,
        "emailDetail": None if emailed else (mail.get("detail") if mail else None),
    }


@app.post("/api/late")
def api_late(req: LateSendRequest, secret: str = Query(default="")):
    """Staff-fired running-late email + SMS. kind=late_soon | late_25."""
    _require_staff(secret)
    kind = req.kind if req.kind in ("late_soon", "late_25", "no_show") else "late_soon"
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "America/Chicago"))
    cid = req.calendar_id or os.environ.get("GOOGLE_CALENDAR_ID") or DEFAULT_CALENDAR
    try:
        ev = get_event(cid, req.event_id)
    except Exception as e:
        raise HTTPException(404, f"event not found: {e}") from e
    fields = _event_mail_fields(ev, cid, tz)
    if not fields.get("start_iso"):
        raise HTTPException(400, "event has no start time")
    if kind == "no_show":
        to = fields.get("email") or ""
        if not to:
            raise HTTPException(400, "no patient email on this visit")
        result = send_named_email(kind="no_show", to=to, fields=fields)
        return {"ok": result.get("ok"), "kind": kind, **result, "visit": fields}
    result = send_late(
        kind=kind,
        name=fields["name"],
        email=fields["email"],
        phone=fields["phone"],
        title=fields["title"],
        duration_minutes=fields.get("duration_minutes"),
        start_iso=fields["start_iso"],
        sms_consent=bool(fields.get("sms_consent")),
        rebook_url=fields["rebook_url"],
        now_iso=datetime.now(tz).isoformat(),
    )
    return {"ok": result.get("ok"), "kind": kind, **result, "visit": fields}


@app.post("/api/mail/send")
def api_mail_send(req: MailSendRequest, secret: str = Query(default="")):
    """Send any branded suite email (intake, form, pay, document, superbill…)."""
    _require_staff(secret)
    from .email_templates import SUITE

    if req.kind not in SUITE:
        raise HTTPException(400, f"kind must be one of {sorted(SUITE)}")
    fields = req.model_dump()
    try:
        result = send_named_email(kind=req.kind, to=str(req.to), fields=fields)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": result.get("ok"), "kind": req.kind, "to": str(req.to), **result}


@app.get("/api/mail/kinds")
def api_mail_kinds(secret: str = Query(default="")):
    _require_staff(secret)
    from .email_templates import SUITE

    return {"kinds": SUITE}


@app.get("/desk")
def desk_page():
    return _page("desk.html", cache=False)


@app.get("/desk-sw.js")
def desk_service_worker():
    path = STATIC / "js" / "desk-sw.js"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache",
            "Service-Worker-Allowed": "/",
        },
    )


def _page(name: str, cache: bool = True) -> FileResponse:
    path = STATIC / name
    if not path.exists():
        raise HTTPException(404)
    headers = {} if cache else {"Cache-Control": "no-store"}
    return FileResponse(path, headers=headers)


@app.get("/")
def hub():
    return _page("index.html")


@app.get("/new-client")
def page_new():
    return _page("book.html")


@app.get("/follow-up")
def page_follow():
    return _page("book.html")


@app.get("/intro-15")
def page_intro():
    return _page("book.html")


@app.get("/thanks")
def page_thanks():
    return _page("thanks.html")


@app.get("/{slug}")
def page_book_slug(slug: str):
    """Public book page for any enabled service."""
    from .event_store import RESERVED

    if "." in slug or slug in RESERVED:
        raise HTTPException(404)
    et = (load_config().get("event_types") or {}).get(slug)
    if not isinstance(et, dict) or et.get("enabled") is False or et.get("public") is False:
        raise HTTPException(404)
    return _page("book.html")
