from __future__ import annotations

import json
import os
import threading
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
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
from .sms_bridge import (
    ack as sms_bridge_ack,
    bridge_enabled,
    claim as sms_bridge_claim,
    due_summary,
    enqueue as sms_bridge_enqueue,
    render_agent_template,
    secret_ok,
)
from .spruce_api import spruce_api_enabled
from .slots import generate_slots
from .patients import (
    client_by_email,
    client_by_id,
    create_client as patient_create_client,
    search_clients,
    supabase_ready,
)
from .week_people import merge_client_hits, people_from_visits
from .staff_auth import COOKIE as STAFF_COOKIE, secret_from_request
from .tokens import mint_cancel_token, verify_cancel_token, verify_patient_token
from . import cipher_mode, cipher_queue, cipher_seal

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
    seed = None
    if os.environ.get("CALDAV_SEED_GOOGLE", "1").strip().lower() in ("1", "true", "yes"):
        import threading

        seed = threading.Thread(target=_seed_caldav_bg, name="caldav-seed", daemon=True)
        seed.start()
    from . import gsync

    gsync.start(wait_for=seed)  # Google -> CalDAV re-sync at startup, then every 5 min
    from . import onboarding

    onboarding.start_sweeper(_onboarding_fx)  # new-client holds: reminder / confirm / release
    yield


VERSION = "0.4.0"
GIT_SHA = (os.environ.get("GIT_SHA") or "").strip() or "unknown"

app = FastAPI(title="Psych Arts Booker", version=VERSION, lifespan=_lifespan)
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
    # CIPHER_P1: id of the sealed envelope posted to /api/cipher/intake first
    cipher_envelope_id: str = Field(default="", max_length=80)


class VerifyRequest(BaseModel):
    email: str = ""
    phone: str = ""
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    dob: str = ""
    portal: bool = False


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
        "version": VERSION,
        "git_sha": GIT_SHA,
        "types": list(cfg.get("event_types", {}).keys()),
        "timezone": cfg.get("timezone"),
        "public_base": PUBLIC_BASE,
        "cancel": True,
        "reminders": True,
        "reminder_email": ["24h", "1h"],
        "reminder_sms_minutes": float(os.environ.get("REMINDER_SMS_MINUTES", "30")),
        "suite": True,
        "sms_enabled": ENABLE_SMS,
        "sms_bridge": (
            "on" if spruce_api_enabled() or bridge_enabled() else "off"
        ),
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


class VerifyCodeRequest(BaseModel):
    email: str = ""
    phone: str = ""
    code: str = ""


@app.post("/api/verify-patient")
def api_verify_patient(req: VerifyRequest):
    """Phone or email on file can name a message. No token. No chart."""
    from .verify_codes import recognize_for_message

    if not (req.email or "").strip() and not (req.phone or "").strip():
        return {"ok": False, "level": "none", "reason": "need_email_or_phone"}
    return recognize_for_message(email=req.email, phone=req.phone)


@app.post("/api/verify-patient/code-send")
def api_verify_code_send(req: VerifyRequest):
    """Send a code before any visit, note, or billing detail."""
    from .verify_codes import start_verify

    return start_verify(email=req.email, phone=req.phone)


@app.post("/api/verify-patient/code")
def api_verify_patient_code(req: VerifyCodeRequest):
    """Mint a token only after the code. No chart fields."""
    from .verify_codes import confirm_code

    result = confirm_code(email=req.email, phone=req.phone, code=req.code)
    if result.get("ok"):
        result["level"] = "sensitive"
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


# Phone line (Amazon Connect via Caddy /desk-bridge, which sets X-Desk-Channel).
# Web requests never carry it (Caddy strips it on public routes), so web is unchanged.
PHONE_WINDOW_DAYS = 92
PHONE_NOTICE_MINUTES = 14 * 60


def _phone_channel(request: Request) -> bool:
    return (request.headers.get("x-desk-channel") or "").strip().lower() == "voice"


def _phone_slots(cfg, et, dur: int, range_start: datetime | None = None):
    return generate_slots(
        cfg, et, dur,
        range_start=range_start,
        days=PHONE_WINDOW_DAYS,
        min_notice_minutes=PHONE_NOTICE_MINUTES,
    )


@app.get("/api/slots")
def api_slots(
    request: Request,
    type: str = Query(..., alias="type"),
    duration: int | None = None,
    date: str | None = None,
):
    cfg = load_config()
    et = get_event_type(cfg, type)
    if not et:
        raise HTTPException(404, "Unknown or disabled event type")
    durs = _durations(et)
    dur = int(duration or durs[0])
    if dur not in durs:
        raise HTTPException(400, f"duration must be one of {durs}")
    if _phone_channel(request):
        rs = None
        if date:
            try:
                rs = datetime.strptime(date, "%Y-%m-%d").replace(
                    tzinfo=ZoneInfo(cfg.get("timezone", "America/Chicago"))
                )
            except ValueError:
                raise HTTPException(400, "date must be YYYY-MM-DD")
        slots = _phone_slots(cfg, et, dur, rs)
    else:
        slots = generate_slots(cfg, et, dur)
    return {
        "event_type": type,
        "title": et.get("title"),
        "duration_minutes": dur,
        "timezone": cfg.get("timezone"),
        "slots": slots[:1000],  # 92-day window needs >200
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
    location_line: str = "Video visit · https://live.psycharts.org/guest",
    extra_notify: list[str] | None = None,
    rebook_url: str = "",
    duration_minutes: int | None = None,
    start_iso: str | None = None,
    timezone: str = "America/Chicago",
    ics_url: str = "",
    event_id: str = "",
    hold_until_label: str = "",
    intake_url: str = "",
) -> None:
    """Run after response — Proton can take 10–60s; do not block the thanks screen."""
    patient_confirm_email(
        name=name,
        email=email,
        phone=phone,
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
        hold_until_label=hold_until_label,
        intake_url=intake_url,
    )
    patient_confirm_sms(
        phone=phone,
        title=title,
        when_label=when_label,
        sms_consent=sms_consent,
        duration_minutes=duration_minutes,
        event_id=event_id,
        hold_until_label=hold_until_label,
        intake_url=intake_url,
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


HOLD_EVENT_TYPE = "new-client"
from .onboarding import HOLD_PREFIX  # noqa: E402


def _plan_hold(req: "BookRequest", start: datetime, *, phone: bool) -> dict[str, Any] | None:
    """None for existing clients (unchanged) or when the hold can't be set up (fail open)."""
    from . import onboarding

    try:
        needed, _row = onboarding.needs_hold(str(req.email))
        if not needed:
            return None
        row = onboarding.ensure_new_client(name=req.name, email=str(req.email), phone=req.phone or "")
    except Exception as e:
        print("hold_plan_fail", type(e).__name__, flush=True)
        return None
    try:
        intake_url = _mint_intake_url(str(req.email), req.name)
    except Exception as e:
        print("hold_mint_fail", type(e).__name__, flush=True)
        intake_url = f"{SITE_PUBLIC}/forms"
    booked = datetime.now(start.tzinfo)
    deadline = onboarding.deadline_for(booked, start)
    return {
        "client_id": row["id"],
        "intake_url": intake_url,
        "booked_at": booked,
        "deadline": deadline,
        "deadline_iso": deadline.isoformat(),
        "deadline_label": onboarding.hold_phrase(booked, deadline),
        "phone": phone,
    }


def _desk_intake_link(email: str) -> str:
    """Desk-booked new client (no hold): still send the intake cascade link unless intake is done."""
    from . import onboarding

    try:
        if onboarding.status(email=email)["complete"]:
            return ""
        return _mint_intake_url(email)
    except Exception:
        return ""


def _onboarding_fx():
    from . import onboarding
    from .notify import default_staff_emails, send_email, send_sms

    def staff(subject: str, body: str) -> None:
        for to in default_staff_emails():
            send_email(to, subject, body)

    return onboarding.Effects(
        get_event=get_event,
        patch_event=patch_event,
        delete_event=delete_event,
        send_email=send_email,
        send_sms=send_sms,
        staff_email=staff,
        rebook_url=f"{PUBLIC_BASE}/{HOLD_EVENT_TYPE}",
    )


@app.post("/api/book")
def api_book(req: BookRequest, background_tasks: BackgroundTasks, request: Request):
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
        if token:
            try:
                verify_patient_token(token)
            except ValueError as e:
                raise HTTPException(401, f"Patient verification expired or invalid: {e}") from e
        else:
            from .verify_codes import recognize_for_message

            found = recognize_for_message(email=req.email, phone=req.phone)
            if not found.get("ok"):
                raise HTTPException(
                    401,
                    "Follow-up booking needs the phone or email on file. A code is not required.",
                )

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
        if _phone_channel(request):
            slots = _phone_slots(cfg, et, dur, start)
        else:
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
    event_id = uuid.uuid4().hex

    cipher_on = cipher_mode.enabled()
    occ = cipher_mode.occupancy()
    dual = cipher_mode.dual_write()
    envelope_id = (req.cipher_envelope_id or "").strip()
    if cipher_on and envelope_id and not cipher_queue.get_envelope(envelope_id):
        raise HTTPException(400, "cipher mode: unknown cipher_envelope_id")
    if occ and not dual and not envelope_id:
        # Occupancy with no cleartext shadow: detail must exist as a sealed blob.
        raise HTTPException(400, "cipher mode: sealed intake required (cipher_envelope_id)")

    # New-client hold (web + phone): 'Held: paperwork due' until intake + consents + card on file.
    hold: dict[str, Any] | None = None
    desk_intake_url = ""
    if req.event_type == HOLD_EVENT_TYPE and src != "desk" and not occ:
        hold = _plan_hold(req, start, phone=_phone_channel(request) or src in ("voice", "chat", "agent"))
    elif req.event_type == HOLD_EVENT_TYPE and src == "desk" and not occ:
        desk_intake_url = _desk_intake_link(str(req.email))

    # Summary shows client visit length; calendar event span may be cal_dur (e.g. 120)
    if occ:
        summary = f"Visit · {event_id[:8]}"
    elif cal_dur != dur:
        summary = f"{req.name} – {title} ({dur} min visit · {cal_dur} min block)"
    else:
        summary = f"{req.name} – {title} ({dur} min)"
    when_label = start.strftime("%a %b %d · %I:%M %p").replace(" 0", " ")
    if occ:
        # Occupancy-only: no Name/Email/Phone/Notes lines on the calendar event.
        desc_lines = [
            "Booked via Psych Arts sovereign booker",
            "Occupancy hold — visit detail is sealed (cipher P1).",
            f"Type: {req.event_type}",
            f"Client duration: {dur} minutes",
            f"Calendar block: {cal_dur} minutes",
            f"When: {when_label} America/Chicago",
        ]
    else:
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
        if hold:
            desc_lines.insert(1, f"HELD {hold['deadline_label']}: paperwork due (intake + consents + card on file). "
                                 "Released automatically if not finished.")

    rebook_url = f"{PUBLIC_BASE}/{req.event_type}"
    token = mint_cancel_token(
        event_id=event_id,
        calendar_id=cal_id,
        email=str(req.email),
        title=f"{title} ({dur} min)",
        when_label=when_label,
        name="" if occ else req.name,
        event_type=req.event_type,
        start_iso=start.isoformat(),
        duration_minutes=dur,
        purpose="cancel",
    )
    cancel_url = f"{PUBLIC_BASE}/cancel?token={token}"
    ics_url = f"{PUBLIC_BASE}/cal/visit.ics?token={token}"
    desc_lines.append("")
    desc_lines.append("Cancel / reschedule:")
    desc_lines.append(cancel_url)
    desc_lines.append(f"Rebook: {rebook_url}")
    description = "\n".join(desc_lines)

    if occ:
        # Opaque occupancy props. No patient_email/name/phone — no silent put-back.
        # Reminder flags pre-set: the cron has no cleartext address on the event.
        private_props = {
            "booker": "1",
            "cipher": "1",
            "event_type": req.event_type,
            "duration_minutes": str(dur),
            "rem24": "1",
            "rem1": "1",
            "rem2": "1",
            "remsms": "1",
        }
        if envelope_id:
            private_props["cipher_envelope_id"] = envelope_id
    else:
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
        if hold:
            private_props["hold"] = "held"
            private_props["hold_deadline"] = hold["deadline_iso"]
    if hold:
        summary = HOLD_PREFIX + summary

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
            **({"status": "tentative"} if hold else {}),
        )
    except Exception as e:
        raise HTTPException(502, f"Calendar write failed: {e}") from e

    if hold:
        try:
            from . import onboarding

            onboarding.create_hold(
                event_id=event_id, calendar_id=cal_id, client_id=hold["client_id"], email=str(req.email),
                name=req.name, phone=req.phone or "", sms_consent=sms_consent, title=title,
                when_label=when_label, event_type=req.event_type, start=start, booked_at=hold["booked_at"],
                deadline=hold["deadline"], intake_url=hold["intake_url"], summary=summary, source=src,
            )
        except Exception as e:  # fail open: the visit stays booked, staff sees no hold
            print("hold_record_fail", type(e).__name__, flush=True)
            hold = None

    if cipher_on and dual:
        # Dual-write: cleartext client record in the on-box sqlite, as today.
        try:
            patient_create_client(name=req.name, email=str(req.email), phone=req.phone or "")
        except Exception:
            pass

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
            event_id=event_id,
            hold_until_label=(hold or {}).get("deadline_label", ""),
            intake_url=(hold or {}).get("intake_url", "") or desk_intake_url,
        )

    out = {
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
    if hold:
        out["held"] = True
        out["hold_until"] = hold["deadline_iso"]
        out["hold_until_label"] = hold["deadline_label"]
        out["message"] = (
            f"Your spot is held {hold['deadline_label']}. Please complete your intake paperwork "
            "and add a card on file before your visit; the link is also in your email"
            + (" and text." if sms_consent else ".")
        )
        if not hold["phone"]:
            # Web booker goes straight into the intake screens with this personal link.
            out["intake_url"] = hold["intake_url"]
    return out


@app.post("/api/reminders/run")
def api_reminders_run(
    secret: str = Query(default=""),
    dry_run: bool = Query(default=False),
):
    """Cron entrypoint. Protect with REMIND_CRON_SECRET when set."""
    if REMIND_SECRET and secret != REMIND_SECRET:
        raise HTTPException(401, "unauthorized")
    return process_reminders(dry_run=dry_run)


class ChatThreadPost(BaseModel):
    body: str
    token: str = ""
    label: str = ""
    email: str = ""
    phone: str = ""
    hear_back: str = ""
    sms_consent: bool = False
    name: str = ""


@app.post("/api/public/chat-threads")
def api_chat_thread_post(req: ChatThreadPost):
    from .chat_threads import post_message

    try:
        return post_message(
            body=req.body,
            token=req.token,
            label=req.label,
            email=req.email,
            phone=req.phone,
            hear_back=req.hear_back,
            sms_consent=req.sms_consent,
            name=req.name,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/public/chat-threads")
def api_chat_thread_get(token: str = Query(default="")):
    from .chat_threads import get_thread

    if not token:
        raise HTTPException(400, "token required")
    try:
        row = get_thread(token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not row:
        raise HTTPException(404, "thread not found")
    return row


@app.post("/api/spruce/webhook")
async def api_spruce_webhook(request: Request):
    """Spruce conversationItem.created. Signature required. No message bodies in the response."""
    from .chat_threads import handle_spruce_webhook

    raw = await request.body()
    sig = request.headers.get("x-spruce-signature") or ""
    result = handle_spruce_webhook(raw, sig)
    if result.get("unauthorized"):
        raise HTTPException(401, "unauthorized")
    return {"ok": True, "accepted": int(result.get("accepted") or 0)}


class SmsBridgeEnqueue(BaseModel):
    template: str
    phone: str
    source: str = "voice"
    idempotency_key: str = ""


class SmsBridgeAck(BaseModel):
    id: str
    status: str


def _require_bridge(request: Request) -> None:
    if not secret_ok(request.headers.get("x-bridge-secret", "")):
        raise HTTPException(401, "unauthorized")


@app.get("/api/sms-bridge/due")
def api_sms_bridge_due(request: Request):
    """Counts only. No phone numbers. Worker uses this while send is off."""
    _require_bridge(request)
    return {"ok": True, **due_summary()}


@app.post("/api/sms-bridge/claim")
def api_sms_bridge_claim(request: Request):
    """Lease one due text for the desk Mac. Secret header. Not a public pull."""
    _require_bridge(request)
    return {"ok": True, "job": sms_bridge_claim()}


@app.post("/api/sms-bridge/ack")
def api_sms_bridge_ack(req: SmsBridgeAck, request: Request):
    _require_bridge(request)
    try:
        result = sms_bridge_ack(req.id, req.status)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not result.get("ok"):
        raise HTTPException(400, result.get("detail") or "ack failed")
    return result


@app.post("/api/sms-bridge/enqueue")
def api_sms_bridge_enqueue(req: SmsBridgeEnqueue, request: Request):
    """Voice or website chat may queue a logistics template. No free text."""
    _require_bridge(request)
    if not bridge_enabled():
        raise HTTPException(409, "sms bridge off")
    source = (req.source or "").strip().lower()
    if source not in ("voice", "chat"):
        raise HTTPException(400, "source must be voice or chat")
    template = (req.template or "").strip()
    if template not in ("book_link", "join"):
        raise HTTPException(400, "template not allowed")
    from .notify import normalize_e164

    phone = normalize_e164(req.phone)
    if not phone:
        raise HTTPException(400, "invalid phone")
    body = render_agent_template(template)
    return sms_bridge_enqueue(
        to=phone,
        body=body,
        kind=template,
        source=source,
        idempotency_key=(req.idempotency_key or "").strip(),
        template=template,
    )


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


class PublicTokenRequest(BaseModel):
    verify_token: str


class PublicCancelVisitRequest(BaseModel):
    verify_token: str
    event_id: str


def _verified_client(token: str) -> dict:
    from .patients import client_by_id

    try:
        payload = verify_patient_token(token)
    except ValueError as e:
        raise HTTPException(401, str(e)) from e
    row = client_by_id(str(payload.get("id") or ""))
    if not row:
        raise HTTPException(404, "not found")
    return row


@app.post("/api/public/upcoming")
def api_public_upcoming(req: PublicTokenRequest):
    """Next visits for a verified current patient. No chart."""
    row = _verified_client(req.verify_token)
    tz = ZoneInfo(load_config().get("timezone", "America/Chicago"))
    visits = _visits_for_client(row, tz)
    upcoming = [v for v in visits if v.get("upcoming")]
    upcoming.sort(key=lambda v: v.get("start_iso") or "")
    return {
        "ok": True,
        "name": row.get("name") or "",
        "visits": [
            {
                "event_id": v.get("event_id") or "",
                "title": v.get("title") or "Visit",
                "when_label": v.get("when_label") or "",
                "duration_minutes": v.get("duration_minutes"),
                "start_iso": v.get("start_iso") or "",
            }
            for v in upcoming[:6]
        ],
    }


@app.post("/api/public/cancel-visit")
def api_public_cancel_visit(req: PublicCancelVisitRequest):
    """Cancel one of this verified patient's visits. Used to reschedule."""
    row = _verified_client(req.verify_token)
    tz = ZoneInfo(load_config().get("timezone", "America/Chicago"))
    match = None
    for v in _visits_for_client(row, tz):
        if str(v.get("event_id") or "") == req.event_id:
            match = v
            break
    if not match:
        raise HTTPException(404, "visit not found")
    delete_event(match["calendar_id"], match["event_id"])
    return {"ok": True, "cancelled": match.get("when_label") or ""}


@app.post("/api/public/folders")
def api_public_folders(req: PublicTokenRequest):
    """Cloaked visit-notes and superbill folder links. No note bodies."""
    row = _verified_client(req.verify_token)
    return {
        "ok": True,
        "notes_url": row.get("chart_url") or "",
        "billing_url": row.get("billing_url") or "",
        "notes_label": "Visit notes",
        "billing_label": "Superbills",
    }


@app.post("/api/public/balance")
def api_public_balance(req: PublicTokenRequest):
    """Owed amount for a verified patient. No line-item detail."""
    row = _verified_client(req.verify_token)
    from .fees import dollars, owed_cents

    cents = owed_cents(str(row.get("id") or ""))
    return {"ok": True, "owed_cents": cents, "owed_pretty": dollars(cents)}


@app.post("/api/public/pay")
def api_public_pay(req: PublicTokenRequest):
    """Stripe Checkout for the open balance. Saves the card for later."""
    row = _verified_client(req.verify_token)
    from . import clients_db
    from .fees import attach_invoice, dollars, list_open, owed_cents
    from .stripe_pay import (
        create_balance_checkout,
        ensure_customer,
        open_checkout_url,
        stripe_ready,
        void_invoice,
    )

    if not stripe_ready():
        raise HTTPException(503, "card payments are not on")
    cid = str(row.get("id") or "")
    email = str(row.get("email") or "").strip()
    if not email:
        raise HTTPException(400, "no email on file for a card")
    open_fees = list_open(cid)
    cents = owed_cents(cid)
    if cents <= 0 or not open_fees:
        return {"ok": True, "url": "", "owed_cents": 0, "owed_pretty": dollars(0)}
    for fee in open_fees:
        sid = str(fee.get("stripe_id") or "")
        url = str(fee.get("pay_url") or "")
        if sid.startswith("cs_") and url.startswith("https://"):
            live = open_checkout_url(sid)
            if live:
                return {
                    "ok": True,
                    "url": live,
                    "owed_cents": cents,
                    "owed_pretty": dollars(cents),
                    "reused": True,
                }
    for fee in open_fees:
        sid = str(fee.get("stripe_id") or "")
        if sid.startswith("in_"):
            void_invoice(sid)
    cust = ensure_customer(
        email=email,
        existing_id=str(row.get("stripe_customer_id") or ""),
        client_id=cid,
    )
    if cust and cust != row.get("stripe_customer_id"):
        clients_db.upsert({"id": cid, "stripe_customer_id": cust})
    fee_ids = [str(fee.get("id") or "") for fee in open_fees if fee.get("id")]
    sess = create_balance_checkout(
        customer_id=cust,
        amount_cents=cents,
        client_id=cid,
        fee_ids=fee_ids,
        success_url=PUBLIC_BASE + "/pay/thanks?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=SITE_PUBLIC + "/portal",
    )
    attach_invoice(fee_ids, stripe_id=str(sess["id"]), pay_url=str(sess["url"]))
    return {
        "ok": True,
        "url": sess["url"],
        "owed_cents": cents,
        "owed_pretty": dollars(cents),
    }


@app.get("/pay/thanks", response_class=HTMLResponse)
def pay_thanks(session_id: str = ""):
    from .stripe_pay import finalize_checkout

    title = "Payment not completed"
    note = "If you closed the payment page, nothing was charged."
    if session_id.startswith("cs_"):
        try:
            done = finalize_checkout(session_id)
        except Exception:
            done = {"ok": False}
        if done.get("ok") and not done.get("pending"):
            title = "Payment received"
            note = "This card is saved for later visits."
        elif done.get("pending"):
            title = "Payment still processing"
            note = "If the bank finishes it, the balance will clear on its own."
    page = f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<body style="margin:0;font-family:Georgia,serif;background:#f6f3ee;color:#1a1a1a;display:flex;min-height:100vh;align-items:center;justify-content:center">
<main style="max-width:28rem;padding:2rem;text-align:center">
<h1 style="font-size:1.5rem;font-weight:500">{title}</h1>
<p style="line-height:1.5">{note}</p>
<p><a href="{SITE_PUBLIC}" style="color:#1a1a1a">Back to the practice site</a></p>
</main>
"""
    return HTMLResponse(page)


@app.get("/cancel", response_class=HTMLResponse)
def cancel_page(token: str = "", done: str = ""):
    if not token:
        return HTMLResponse(_cancel_html(error="Missing cancel token."), status_code=400)
    try:
        payload = verify_cancel_token(token)
    except ValueError as e:
        return HTMLResponse(_cancel_html(error=str(e)), status_code=400)
    details = _visit_details_from_payload(payload)
    rebook = f"/{payload.get('et')}" if payload.get("et") else "/"
    if details.get("gone") == "1" or done == "1":
        return HTMLResponse(
            _cancel_html(
                done=True,
                note="This visit is cancelled.",
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
    from urllib.parse import quote
    return RedirectResponse(
        url=f"/cancel?token={quote(token)}&done=1",
        status_code=303,
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
        <p style="font-size:1.15rem;font-weight:650;margin:0 0 8px">This visit is cancelled.</p>
        <p>It is off the calendar.</p>
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
  <title>{'Visit cancelled' if done else 'Cancel visit'} · Psych Arts</title>
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
      <h1>{'Visit cancelled' if done else 'Cancel visit'}</h1>
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
    _attach_intake(visits)
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


def _attach_intake(visits: list[dict[str, Any]]) -> None:
    """Calendar card: hold state + intake flags for new-client visits. Never raises."""
    try:
        from . import onboarding

        holds = onboarding.holds_by_event([str(v.get("event_id") or "") for v in visits
                                           if v.get("event_type") == HOLD_EVENT_TYPE])
        for v in visits:
            h = holds.get(str(v.get("event_id") or ""))
            if not h:
                continue
            st = onboarding.status(client_id=h.get("client_id") or "", email=h.get("email") or "")
            v["hold"] = {"status": h["status"], "deadline_at": h["deadline_at"]}
            v["intake"] = {"flags": st["flags"], "complete": st["complete"], "missing": st["missing"]}
    except Exception:
        return


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


class InternalIntakeRequest(BaseModel):
    form: str
    fields: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)


INTAKE_FORMS = ("personal", "consents", "card-auth", "roi")


def _chart_for(row: dict[str, Any]) -> str:
    from .drive_paths import LIVING_ROOT, person_folder_name, paths_from_row, resolve_client_paths

    chart = paths_from_row(row).get("chart") or ""
    if not chart:
        try:
            chart = resolve_client_paths(row, _drive_list).get("chart") or ""
        except Exception:
            chart = ""
    if not chart:
        dest = person_folder_name(row)
        if dest:
            chart = f"{LIVING_ROOT}/{dest}/Chart"
    return chart


_COPIES_LOCK = threading.Lock()


def _maybe_send_copies(client_id: str) -> dict[str, Any] | None:
    """Once intake is complete, email + text the client a 30-day no-login link to their signed copies
    (and the Notice of Privacy Practices). Re-sends with a fresh link when a newer document is signed.
    Never raises; logs no PHI."""
    from . import billing, intake_copies, onboarding

    try:
        with _COPIES_LOCK:
            st = onboarding.status(client_id=client_id)
            if not intake_copies.due(client_id, bool(st["complete"])):
                return None
            view = dict(client_by_id(client_id) or {})
            if not view:
                return None
            ids = [d["id"] for d in intake_copies.docs_for(client_id)]
            n = len(ids)
            billing.revoke_shares(client_id, "docs")
            link = billing.mint_share(billing_id=client_id, client_id=client_id, source="docs", path=",".join(ids),
                                      name="Signed intake documents", kind="docs")
        url = f"{PUBLIC_BASE}/docs/{link['token']}"
        first = (str(view.get("name") or "there").split() or ["there"])[0]
        sent = _bill_send(
            kind="intake_copies",
            view=view,
            fields={"name": view.get("name") or "", "url": url, "count": n, "npp_url": intake_copies.NPP_URL},
            sms_body=f"Hi {first}, copies of the forms you signed for Dr. Brown, plus our privacy notice "
                     f"(private link, good for 30 days): {url}",
            send_email=True,
            send_sms=True,
            dry=False,
            key=f"docs-{link['id']}",
        )
        print("intake_copies_sent", n, "email", (sent.get("email") or {}).get("ok"),
              "sms", (sent.get("sms") or {}).get("ok"), flush=True)
        return sent
    except Exception as e:
        print("intake_copies_fail", type(e).__name__, flush=True)
        return None


def _file_intake_pdf(sub_id: str, client_id: str, form: str, fields: dict[str, Any], meta: dict[str, Any],
                     received_at: str) -> dict[str, Any]:
    """Render the signed submission and save it to the client's Chart/Consents (created if missing)."""
    import base64

    from . import onboarding
    from .intake_pdf import intake_pdf

    row = client_by_id(client_id) if client_id else None
    if not row:
        onboarding.set_pdf(sub_id, status="no client")
        return {"ok": False, "detail": "no client"}
    try:
        tz = ZoneInfo(load_config().get("timezone", "America/Chicago"))
        when = datetime.fromisoformat(received_at).astimezone(tz)
        link = meta.get("linkAuth") or {}
        pdf = intake_pdf(
            form=form, fields=fields, received_at=when.strftime("%Y-%m-%d %I:%M:%S %p CT"),
            ip=str(meta.get("ip") or ""), user_agent=str(meta.get("userAgent") or ""),
            link_verified=bool(link.get("verified")), link_email=str(link.get("email") or ""),
            client_name=row.get("name") or "",
        )
        chart = _chart_for(row)
        if not chart:
            raise HTTPException(400, "no chart folder for this client")
        folder = _ensure_visit_notes(chart, "Consents")
        given = (row.get("given_name") or "").strip()
        family = (row.get("family_name") or "").strip()
        ini = ((given[:1] + family[:1]).upper()) if given and family else ""
        stem = f"{when.date().isoformat()} {ini} {form}" if ini else f"{when.date().isoformat()} {form}"
        dest = folder + "/" + _unique_pdf_name(folder, stem)
        code, saved = _notify_post("/drive/save", {"path": dest, "b64": base64.b64encode(pdf).decode("ascii")})
        if code >= 400 or not saved.get("ok"):
            raise HTTPException(502, "drive save failed")
        onboarding.set_pdf(sub_id, path=dest, status="saved")
        _maybe_send_copies(client_id)
        return {"ok": True, "path": dest}
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else type(e).__name__
        onboarding.set_pdf(sub_id, status=f"failed: {str(detail)[:80]}")
        print("intake_pdf_fail", form, str(detail)[:80], flush=True)
        return {"ok": False, "detail": str(detail)[:80]}


@app.post("/api/internal/intake/submitted")
def api_internal_intake_submitted(req: InternalIntakeRequest, request: Request, background_tasks: BackgroundTasks,
                                  secret: str = Query(default="")):
    """Site intake form landed (already emailed to the practice). Record the completion flag, file the
    signed PDF under Chart/Consents, and confirm any held new-client visit once intake is complete."""
    from . import onboarding

    _require_staff(secret, request)
    form = (req.form or "").strip()
    if form not in INTAKE_FORMS:
        raise HTTPException(400, "unknown form")
    link = req.meta.get("linkAuth") or {}
    email = str((link.get("email") if link.get("verified") else "") or req.fields.get("email") or "").strip().lower()
    rec = onboarding.record_submission(form=form, email=email, link_verified=bool(link.get("verified")))
    background_tasks.add_task(_file_intake_pdf, rec["id"], rec["client_id"], form, dict(req.fields),
                              dict(req.meta), rec["received_at"])
    # Confirming a hold patches two calendars and sends notices (slow); do it after responding so the
    # site's call never times out. The PDF task above runs first, then this.
    if rec["client_id"]:
        background_tasks.add_task(_check_client_bg, rec["client_id"])
    st = onboarding.status(client_id=rec["client_id"]) if rec["client_id"] else None
    return {"ok": True, "linked": bool(rec["client_id"]), "flags": (st or {}).get("flags"),
            "complete": bool(st and st["complete"])}


def _check_client_bg(client_id: str) -> list[str]:
    from . import onboarding

    try:
        return onboarding.check_client(_onboarding_fx(), client_id=client_id)
    except Exception as e:
        print("hold_check_fail", type(e).__name__, flush=True)
        return []


@app.get("/api/internal/intake/progress")
def api_internal_intake_progress(request: Request, email: str = "", secret: str = Query(default="")):
    """Site cascade: which steps are done for the emailed-link session (resume at first incomplete),
    plus name/phone to prefill. Staff secret (site server only)."""
    from . import onboarding

    _require_staff(secret, request)
    row = onboarding.find_client(email)
    st = onboarding.status(email=email)
    holds = onboarding.holds_for_client(st["client_id"]) if st["client_id"] else []
    h = holds[0] if holds else None
    return {
        "ok": True,
        "flags": st["flags"],
        "complete": st["complete"],
        "missing": st["missing"],
        "client": {
            "name": (row or {}).get("display_name") or "",
            "given_name": (row or {}).get("given_name") or "",
            "family_name": (row or {}).get("family_name") or "",
            "phone": (row or {}).get("phone_e164") or "",
            "dob": (row or {}).get("dob") or "",
        } if row else None,
        "hold": {"status": h["status"], "when_label": h["when_label"],
                 "deadline_label": onboarding.label(datetime.fromisoformat(h["deadline_at"]))} if h else None,
    }


class HoldTimesRequest(BaseModel):
    deadline_minutes: float
    remind_minutes: float | None = None


@app.post("/api/desk/onboarding/hold/{event_id}/times")
def api_desk_hold_times(event_id: str, req: HoldTimesRequest, request: Request, secret: str = Query(default="")):
    """Staff/test only: move ONE hold's deadline (minutes from now). The 48h policy is unchanged."""
    from datetime import timezone as _tz

    from . import onboarding

    _require_staff(secret, request)
    now = datetime.now(_tz.utc)
    dl = now + timedelta(minutes=req.deadline_minutes)
    rm = now + timedelta(minutes=req.remind_minutes if req.remind_minutes is not None else req.deadline_minutes / 2)
    h = onboarding.set_times(event_id, deadline=dl, remind_at=rm)
    if not h:
        raise HTTPException(404, "no hold")
    return {"ok": True, "hold": {k: h[k] for k in ("event_id", "status", "deadline_at", "remind_at")}}


@app.post("/api/desk/onboarding/sweep")
def api_desk_hold_sweep(request: Request, secret: str = Query(default="")):
    from . import onboarding

    _require_staff(secret, request)
    return {"ok": True, **onboarding.sweep(_onboarding_fx())}


@app.get("/api/desk/onboarding/status")
def api_desk_onboarding_status(request: Request, secret: str = Query(default=""), client_id: str = "",
                               email: str = ""):
    from . import onboarding

    _require_staff(secret, request)
    st = onboarding.status(client_id=client_id, email=email)
    holds = onboarding.holds_for_client(st["client_id"]) if st["client_id"] else []
    return {"ok": True, **st, "holds": [{k: h[k] for k in ("event_id", "status", "when_label", "deadline_at",
                                                          "reminded_at", "resolved_at")} for h in holds]}


class InternalCardRequest(BaseModel):
    email: str = ""
    customer_id: str = ""
    payment_method_id: str = ""
    brand: str = ""
    last4: str = ""
    exp_month: int | None = None
    exp_year: int | None = None


@app.post("/api/internal/client/card")
def api_internal_client_card(req: InternalCardRequest, request: Request, secret: str = Query(default="")):
    """Site pay form writes Stripe pointers. Staff secret. No PAN. No new row."""
    from .patients import record_card_on_file

    _require_staff(secret, request)
    out = record_card_on_file(
        email=req.email,
        customer_id=req.customer_id,
        payment_method_id=req.payment_method_id,
        brand=req.brand,
        last4=req.last4,
        exp_month=req.exp_month,
        exp_year=req.exp_year,
    )
    try:
        from . import onboarding

        out["confirmed_holds"] = onboarding.check_client(_onboarding_fx(), email=req.email)
        row = onboarding.find_client(req.email)
        if row and row.get("id"):
            _maybe_send_copies(str(row["id"]))
    except Exception as e:
        print("hold_check_fail", type(e).__name__, flush=True)
    return out


def _intake_summary(row: dict[str, Any]) -> dict[str, Any]:
    """Per-client intake flags for the desk card. Never raises."""
    try:
        from . import intake_copies, onboarding

        if not (row.get("id") or row.get("email")):
            return {}
        st = onboarding.status(client_id=str(row.get("id") or ""), email=str(row.get("email") or ""))
        holds = onboarding.holds_for_client(st["client_id"]) if st["client_id"] else []
        open_hold = next((h for h in holds if h["status"] == "held"), None)
        return {
            "flags": st["flags"],
            "complete": st["complete"],
            "missing": st["missing"],
            "forms": st["forms"],
            "copies": intake_copies.summary(st["client_id"]) if st["client_id"] else None,
            "hold": {k: open_hold[k] for k in ("event_id", "when_label", "deadline_at", "reminded_at")}
            if open_hold
            else None,
        }
    except Exception:
        return {}


def _form_status(intake: dict[str, Any], form: str) -> str:
    f = ((intake or {}).get("forms") or {}).get(form) if form else None
    if not intake or not form:
        return "unknown"
    return "done" if f and f.get("done") else "not yet"


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
            ev_phone = "".join(ch for ch in str(fields.get("phone") or "") if ch.isdigit())
            client_phone = "".join(ch for ch in str(client.get("phone") or "") if ch.isdigit())
            if not hit and len(client_phone) >= 10 and ev_phone[-10:] == client_phone[-10:]:
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
    intake = _intake_summary(row)
    done_by_item = {"personal": "personal", "consents": "consents", "card": "card-auth", "roi": "roi"}
    forms = [
        {
            "id": sid,
            "label": spec.get("label") or sid,
            "status": _form_status(intake, done_by_item.get(sid, "")),
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
        "intake": intake,
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
    charge: bool = False  # charges only when the desk click says so explicitly
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
    if et == "checkout.session.completed":
        from .stripe_pay import finalize_checkout

        sid = str(obj.get("id") or "")
        try:
            done = finalize_checkout(sid)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)[:160]}
        return {"ok": True, "type": et, **done}
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
def api_desk_note_sign(
    req: DeskNoteSignRequest,
    background_tasks: BackgroundTasks,
    secret: str = Query(default=""),
):
    """Sign an after-visit summary to Chart/Visit notes. Body never logged."""
    import base64

    from .drive_paths import LIVING_ROOT, VISIT_NOTES_FOLDER, person_folder_name, paths_from_row, resolve_client_paths
    from .note_pdf import after_visit_pdf, markdown_pdf
    from .note_templates import get_template, render_template

    _require_staff(secret)
    from .billing import letterhead_missing, refusal_message
    from .practice import load as load_practice  # was imported below its first use: every sign 500'd

    missing = letterhead_missing(load_practice())
    if missing:
        raise HTTPException(409, refusal_message(missing))
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
    vn = _ensure_visit_notes(chart)
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
    fname = _unique_pdf_name(vn, f"{dos} {ini} {slug}" if ini else f"{dos} {slug}")
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
    from .note_pdf import CHART_ONLY
    from .practice import asset_bytes, format_licenses

    letterhead = {
        "name": prac.get("display_name") or "",
        "address": prac.get("address") or "",
        "phone": prac.get("phone") or "",
        "fax": prac.get("fax") or "",
        "email": prac.get("email") or "",
        "clinician": prac.get("clinician_name") or "",
        "licenses": format_licenses(prac.get("licenses") or []),
    }
    # Logo slot: only a logo uploaded in desk -> Practice (no shipped placeholder on patient documents).
    logo = asset_bytes("logo") if prac.get("logo") else None
    sections = spec.get("sections") or []
    is_avs = tid in ("avs", "avs-followup")
    chart_pdf = b""
    if is_avs:
        # Patient-facing AVS (January 2025 letterhead layout, Meet again first). S/O/A never go on it;
        # when present they are kept in a separate chart copy in the same Visit notes folder.
        pdf = after_visit_pdf(fields, title="After Visit Summary", letterhead=letterhead, patient=True, logo=logo)
        if any(str(fields.get(k) or "").strip() for k in CHART_ONLY):
            chart_pdf = after_visit_pdf(
                fields, sections, title=str(spec.get("title") or "After-Visit Summary") + " (chart copy)",
                letterhead=letterhead, logo=logo,
            )
    elif sections:
        pdf = after_visit_pdf(
            fields, sections, title=str(spec.get("title") or ""), letterhead=letterhead, logo=logo
        )
    else:
        pdf = markdown_pdf(render_template(tid, fields))
    from .notify import NOTIFY_TOKEN

    def _save(path: str, blob: bytes) -> None:
        payload = json.dumps({"path": path, "b64": base64.b64encode(blob).decode("ascii")}).encode()
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

    _save(dest, pdf)
    chart_dest = ""
    if chart_pdf:
        chart_dest = dest[: -len(".pdf")] + " chart.pdf"
        _save(chart_dest, chart_pdf)
    from . import signed_notes

    note_id = signed_notes.record(
        client_id=str(row.get("id") or ""), dos=dos, template=tid, fields=fields,
        avs_path=dest if is_avs else "", avs_name=fname if is_avs else "", chart_path=chart_dest,
    )
    # Local style-pack learn (scrub Name/DOB first; never log body; no cloud train).
    try:
        from .style_pack_learn import learn_safe

        background_tasks.add_task(learn_safe, dict(fields), tid)
    except Exception:
        pass
    return {"ok": True, "path": dest, "name": fname, "bytes": len(pdf), "template": tid,
            "note_id": note_id, "chart_path": chart_dest, "shareable": is_avs}


# ---- signed AVS: prefill next note + 30-day patient link (no login) ----------

@app.get("/api/desk/note/last-avs")
def api_desk_note_last_avs(client_id: str = Query(default=""), secret: str = Query(default="")):
    """Last signed AVS for a client: prefill fields for the next note + its share status."""
    _require_staff(secret)
    from . import billing, signed_notes

    note = signed_notes.last_for_client(client_id.strip())
    if not note:
        return {"ok": True, "note": None, "prefill": {}}
    share = billing.active_share(note["id"], "avs") if note.get("avs_path") else None
    return {
        "ok": True,
        "note": {"id": note["id"], "dos": note["dos"], "template": note["template"],
                 "name": note.get("avs_name") or "", "signed_at": note["signed_at"],
                 "shareable": bool(note.get("avs_path"))},
        "prefill": signed_notes.prefill(note),
        "share": share,
    }


class AvsShareRequest(BaseModel):
    note_id: str
    send_email: bool = True
    send_sms: bool = True
    to_email: str = ""
    to_phone: str = ""


@app.post("/api/desk/note/avs/share")
def api_desk_note_avs_share(req: AvsShareRequest, secret: str = Query(default="")):
    """Send (or re-send) the signed AVS: turn off any old link, mint a fresh 30-day one, email + Spruce text."""
    _require_staff(secret)
    from . import billing, signed_notes
    from .superbill_pdf import pretty_date

    note = signed_notes.get(req.note_id.strip())
    if not note or not note.get("avs_path"):
        raise HTTPException(404, "signed AVS not found")
    view = dict(client_by_id(note["client_id"]) or {})
    if not view:
        raise HTTPException(404, "client not found")
    if req.to_email.strip():
        view["email"] = req.to_email.strip()
    if req.to_phone.strip():
        view["phone"] = req.to_phone.strip()
    revoked = billing.revoke_shares(note["id"], "avs")
    link = billing.mint_share(
        billing_id=note["id"], client_id=note["client_id"], source="avs", path=note["avs_path"],
        name=note.get("avs_name") or "After Visit Summary.pdf", kind="avs",
    )
    url = f"{PUBLIC_BASE}/avs/{link['token']}"
    first = (str(view.get("name") or "there").split() or ["there"])[0]
    sent = _bill_send(
        kind="avs",
        view=view,
        fields={"name": view.get("name") or "", "url": url, "dos_label": pretty_date(note["dos"])},
        sms_body=f"Hi {first}, your after-visit summary from Dr. Brown (link good for 30 days): {url}",
        send_email=req.send_email,
        send_sms=req.send_sms,
        dry=False,
        key=f"avs-{link['id']}",
    )
    return {"ok": True, "url": url, "share_id": link["id"], "expires_iso": link["expires_iso"],
            "replaced": revoked, **sent}


class AvsRevokeRequest(BaseModel):
    note_id: str


@app.post("/api/desk/note/avs/share/revoke")
def api_desk_note_avs_revoke(req: AvsRevokeRequest, secret: str = Query(default="")):
    _require_staff(secret)
    from . import billing, signed_notes

    if not signed_notes.get(req.note_id.strip()):
        raise HTTPException(404, "signed AVS not found")
    return {"ok": True, "revoked": billing.revoke_shares(req.note_id.strip(), "avs")}


@app.get("/avs/{token}")
def public_avs_share(token: str):
    """Branded, signed, expiring AVS link (30 days, no login; re-send or turn off from the desk)."""
    from . import billing

    try:
        row = billing.verify_share(token, kind="avs")
    except billing.ShareError as e:
        return _share_page(e.message, e.status, title="After-visit summary")
    code, data, _ctype = _notify_get("/drive/file?path=" + quote(str(row.get("path") or ""), safe="/"))
    if code != 200 or not data.startswith(b"%PDF"):
        return _share_page("The file could not be loaded right now. Try again shortly.", 502, title="After-visit summary")
    billing.note_hit(row["id"])
    name = (row.get("name") or "After Visit Summary.pdf").replace('"', "")
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.get("/docs/{token}")
def public_intake_copies(token: str):
    """Client's copies of their signed intake documents (30-day, no-login, revocable) + privacy notice."""
    from html import escape

    from . import billing, intake_copies
    from .practice import load as load_practice

    try:
        row = billing.verify_share(token, kind="docs")
    except billing.ShareError as e:
        return _share_page(e.message, e.status, title="Your signed documents")
    docs = intake_copies.docs_for(str(row.get("client_id") or ""))
    billing.note_hit(row["id"])
    tz = ZoneInfo(load_config().get("timezone", "America/Chicago"))
    who = str(load_practice().get("display_name") or "")
    exp = datetime.fromtimestamp(int(row.get("expires_at") or 0), timezone.utc).astimezone(tz)
    items = "".join(
        "<li style='margin:.6rem 0;padding:.8rem 1rem;border:1px solid #e5e7eb;border-radius:12px'>"
        f"<a style='color:#111;font-weight:600' href='/docs/{escape(token)}/{escape(d['id'])}'>{escape(d['label'])}</a>"
        "<div style='color:#6b7280;font-size:.85rem'>Signed "
        f"{escape(datetime.fromisoformat(d['received_at']).astimezone(tz).strftime('%b %-d, %Y · %-I:%M %p CT'))} · PDF</div></li>"
        for d in docs
    ) or "<li>No signed documents yet.</li>"
    body = (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='robots' content='noindex'><title>Your signed documents</title>"
        "<body style='font-family:Inter,system-ui,sans-serif;max-width:34rem;margin:8vh auto;padding:0 1rem;color:#111'>"
        f"<img src='/api/practice/logo' alt='' style='height:56px'><p style='color:#2f57a5;margin:.4rem 0'>{escape(who)}</p>"
        "<h1 style='font-size:1.3rem;margin:.6rem 0'>Your signed documents</h1>"
        "<p style='color:#374151'>Here are copies of the forms you signed, for your records. Tap one to open or save it.</p>"
        f"<ul style='list-style:none;padding:0'>{items}</ul>"
        "<h2 style='font-size:1rem;margin-top:1.6rem'>Notice of Privacy Practices</h2>"
        "<p style='color:#374151'>How we use and protect your health information, and your rights. "
        f"<a href='{escape(intake_copies.NPP_URL)}' style='color:#2f57a5'>Open the notice (PDF)</a>. "
        "You can ask us for a paper copy anytime.</p>"
        f"<p style='color:#6b7280;font-size:.85rem;margin-top:1.6rem'>This private link works until "
        f"{escape(exp.strftime('%b %-d, %Y'))}. Questions? Reply to the email or call the practice.</p></body>"
    )
    return HTMLResponse(body, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex",
                                       "Referrer-Policy": "no-referrer"})


@app.get("/docs/{token}/{sub_id}")
def public_intake_copy_file(token: str, sub_id: str):
    from . import billing, intake_copies

    try:
        row = billing.verify_share(token, kind="docs")
    except billing.ShareError as e:
        return _share_page(e.message, e.status, title="Your signed documents")
    d = intake_copies.doc(str(row.get("client_id") or ""), sub_id)
    if not d:
        return _share_page("That document was not found.", 404, title="Your signed documents")
    code, data, _ctype = _notify_get("/drive/file?path=" + quote(str(d["path"]), safe="/"))
    if code != 200 or not data.startswith(b"%PDF"):
        return _share_page("The file could not be loaded right now. Try again shortly.", 502, title="Your signed documents")
    name = (d["path"].rsplit("/", 1)[-1] or "document.pdf").replace('"', "")
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"})


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
    """SOAP + AVS fields from a scribe note or a transcript. Body never logged. No xAI.

    A scribe-aws note (fixed SOAP + After Visit Summary format) is parsed directly, no model call.
    A raw transcript goes to the scribe-aws note writer (Bedrock Nova Pro, under the AWS BAA)."""
    _require_staff(secret)
    raw = (req.transcript or "").strip()
    if len(raw) < 20:
        raise HTTPException(400, "need a bit more transcript")
    from .note_parse import is_structured, parse_structured

    if is_structured(raw):
        return {"ok": True, "fields": parse_structured(raw), "engine": "scribe-structured"}
    minutes = 0
    try:
        minutes = int(str(req.duration or "0").strip() or 0)
    except ValueError:
        minutes = 0
    body = json.dumps({"transcript": raw[:400000], "duration_minutes": minutes}).encode()
    http_req = UrlRequest(SCRIBE_NOTE_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(http_req, timeout=150) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except (HTTPError, URLError, TimeoutError, ValueError) as e:
        raise HTTPException(502, "note writer unavailable") from e
    note = str(data.get("note") or "")
    if data.get("note_engine") == "raw-transcript" or not is_structured(note):
        raise HTTPException(502, "note writer (Bedrock) unavailable; nothing was sent elsewhere")
    return {"ok": True, "fields": parse_structured(note), "engine": str(data.get("note_engine") or "")}


# scribe-aws sidecar (127.0.0.1 only): Bedrock note writer under the AWS BAA.
SCRIBE_NOTE_URL = os.environ.get("SCRIBE_NOTE_URL", "http://127.0.0.1:8795/note")


class DeskScribeIngestRequest(BaseModel):
    transcript: str = ""
    note: str = ""
    title: str = ""
    template: str = "avs"
    duration_minutes: int = 0
    client_id: str = ""
    email: str = ""
    name: str = ""
    room: str = ""
    ended_at: str = ""


def _scribe_ingest_authorized(request: Request, secret: str = "") -> None:
    """Staff unlock secret or dedicated edge ingest token."""
    tok = (os.environ.get("SCRIBE_INGEST_TOKEN") or "").strip()
    hdr = ""
    try:
        hdr = (request.headers.get("X-Scribe-Ingest-Token") or "").strip()
    except Exception:
        hdr = ""
    if tok and hdr and hdr == tok:
        return
    _require_staff(secret, request)


@app.post("/api/desk/note/scribe-ingest")
def api_desk_note_scribe_ingest(
    req: DeskScribeIngestRequest,
    request: Request,
    secret: str = Query(default=""),
):
    """Edge-scribe finish → pending blank draft. Body never logged."""
    _scribe_ingest_authorized(request, secret)
    from . import scribe_drafts

    try:
        row = scribe_drafts.put(
            note=req.note,
            transcript=req.transcript,
            title=req.title,
            template=(req.template or "avs").strip() or "avs",
            client_id=req.client_id,
            email=req.email,
            name=req.name,
            duration_minutes=req.duration_minutes,
            room=req.room,
            ended_at=req.ended_at,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "id": row["id"]}


@app.get("/api/desk/note/scribe-pending")
def api_desk_note_scribe_pending(request: Request, secret: str = Query(default="")):
    _require_staff(secret, request)
    from . import scribe_drafts

    return {"ok": True, "drafts": scribe_drafts.list_pending()}


@app.get("/api/desk/note/scribe-pending/{draft_id}")
def api_desk_note_scribe_pending_one(draft_id: str, request: Request, secret: str = Query(default="")):
    _require_staff(secret, request)
    from . import scribe_drafts

    row = scribe_drafts.get(draft_id)
    if not row:
        raise HTTPException(404, "draft not found")
    return {"ok": True, "draft": row}


@app.post("/api/desk/note/scribe-pending/{draft_id}/ack")
def api_desk_note_scribe_pending_ack(draft_id: str, request: Request, secret: str = Query(default="")):
    _require_staff(secret, request)
    from . import scribe_drafts

    ok = scribe_drafts.ack(draft_id)
    if not ok:
        raise HTTPException(404, "draft not found or already claimed")
    return {"ok": True}


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


# CIPHER_P1 sealed intake. Public routes carry ciphertext only; desk routes
# unseal behind staff auth. Default off (CIPHER_P1=0) — legacy path unchanged.


@app.get("/api/cipher/seal-params")
def api_cipher_seal_params():
    """Public seal parameters. Public JWK only — the private half never leaves cipher_seal."""
    return cipher_seal.seal_params(cipher_mode.enabled())


@app.post("/api/cipher/intake")
async def api_cipher_intake(request: Request):
    """Accept a browser-sealed envelope. Server stores ciphertext only."""
    if not cipher_mode.enabled():
        raise HTTPException(404, "cipher mode off")
    try:
        env = await request.json()
    except Exception:
        raise HTTPException(400, "invalid json")
    try:
        cipher_seal.validate_envelope(env)
    except ValueError as e:
        raise HTTPException(400, f"bad envelope: {e}")
    row = cipher_queue.put(env, source="booker")
    return {"ok": True, "id": row["id"], "content_hash": row["content_hash"]}


@app.get("/api/desk/cipher/status")
def api_desk_cipher_status(secret: str = Query(default="")):
    _require_staff(secret)
    return {"flags": cipher_mode.flags(), "pending": cipher_queue.pending_count()}


@app.get("/api/desk/cipher/pending")
def api_desk_cipher_pending(secret: str = Query(default="")):
    _require_staff(secret)
    return {"pending": cipher_queue.list_pending()}


class CipherDrainRequest(BaseModel):
    id: str = ""


@app.post("/api/desk/cipher/drain")
def api_desk_cipher_drain(req: CipherDrainRequest, secret: str = Query(default="")):
    """Staff unseal: server opens the envelope; plaintext goes to the desk vault."""
    _require_staff(secret)
    env_id = (req.id or "").strip()
    if not env_id:
        raise HTTPException(400, "id required")
    env = cipher_queue.get_envelope(env_id)
    if not env:
        raise HTTPException(404, "envelope not found")
    try:
        plaintext = cipher_seal.open_envelope(env)
    except ValueError as e:
        raise HTTPException(400, f"unseal failed: {e}")
    cipher_queue.mark_drained(env_id)
    return {"ok": True, "id": env_id, "plaintext": plaintext}


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
    pub = public_practice("" if _demo_on() else PUBLIC_BASE)
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

    pub = public_practice("" if _demo_on() else PUBLIC_BASE)
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
    pub = public_practice("" if _demo_on() else PUBLIC_BASE)
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
    from .practice import asset_bytes, format_licenses, load as load_practice, next_statement
    from .superbill_pdf import superbill_filename, superbill_pdf

    _require_staff(secret)
    from .billing import letterhead_missing, refusal_message

    missing = letterhead_missing(load_practice())
    if missing:
        raise HTTPException(409, refusal_message(missing))
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
            "letterhead": {
                "name": prac.get("legal_name") or prac.get("display_name") or "",
                "address": prac.get("address") or "",
                "phone": prac.get("phone") or "",
                "email": prac.get("email") or "",
                "ein": prac.get("ein") or "",
                "npi": prac.get("npi") or "",
                "licenses": format_licenses(prac.get("licenses") or []),
            },
            "logo": asset_bytes("logo"),
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


# ---- One-click billing: Bill visit (docs/GO_BILLING_ONE_CLICK.md) ----------
# Nothing is charged, written, or sent until the clinician confirms the visit code.
# BILLING_LIVE unset means every confirm is a dry run (no fee row, no Stripe, no Drive, no sends).


def _bill_client(client_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from . import clients_db

    raw = clients_db.get_by_id((client_id or "").strip()) if client_id else None
    if not raw:
        raise HTTPException(404, "client not found")
    return raw, client_by_id(raw["id"]) or {}


def _bill_billing_folder(view: dict[str, Any]) -> str:
    from .drive_paths import BILLING_CHILD, LIVING_ROOT, person_folder_name, paths_from_row, resolve_client_paths

    billing = paths_from_row(view).get("billing") or ""
    if not billing:
        try:
            billing = resolve_client_paths(view, _drive_list).get("billing") or ""
        except Exception:
            billing = ""
    if not billing:
        dest = person_folder_name(view)
        if dest:
            billing = f"{LIVING_ROOT}/{dest}/{BILLING_CHILD}"
    return billing


def _bill_drive_save(dest: str, data: bytes) -> None:
    import base64

    code, saved = _notify_post("/drive/save", {"path": dest, "b64": base64.b64encode(data).decode("ascii")})
    if code >= 400 or not saved.get("ok"):
        raise HTTPException(502, "drive save failed")


@app.get("/api/desk/bill")
def api_desk_bill_preview(
    secret: str = Query(default=""),
    client_id: str = Query(default=""),
    event_id: str = Query(default=""),
    dos: str = Query(default=""),
    event_type: str = Query(default=""),
    duration_minutes: int | None = Query(default=None),
):
    """Confirm-sheet data: guessed code, CPT lines, standing dx, card, letterhead gaps."""
    _require_staff(secret)
    from . import billing
    from .practice import load as load_practice

    raw, view = _bill_client(client_id)
    codes = billing.load_codes()
    day = (dos or "")[:10] or datetime.now().date().isoformat()
    key = billing.event_key(event_id, raw["id"], day)
    existing = billing.find_billing(key, dry_run=False) or billing.find_billing(key, dry_run=True)
    missing = billing.letterhead_missing(load_practice())
    return {
        "ok": True,
        "client": {
            "id": raw["id"],
            "name": view.get("name") or "",
            "email": view.get("email") or "",
            "has_phone": bool(view.get("phone")),
            "dob": view.get("dob") or "",
        },
        "event_id": event_id,
        "dos": day,
        "guess": billing.guess_code(event_type, duration_minutes),
        "pos": codes["pos"],
        "codes": list(codes["visit_types"].values()),
        "standing_dx": billing.standing_dx(raw),
        "card": {
            "on_file": bool(raw.get("stripe_customer_id") and raw.get("stripe_payment_method_id")),
            "brand": raw.get("stripe_card_brand") or "",
            "last4": raw.get("stripe_card_last4") or "",
        },
        "live": billing.live_enabled(),
        "sms": billing.sms_enabled(),
        "letterhead_missing": missing,
        "letterhead_message": billing.refusal_message(missing) if missing else "",
        "existing": billing.public_billing(existing),
    }


class BillConfirmRequest(BaseModel):
    client_id: str = ""
    event_id: str = ""
    dos: str = ""
    visit_code: str = ""
    confirm_code: str = ""  # must equal visit_code: the clinician's explicit confirm
    diagnoses: list[Any] = []
    save_dx: bool = False
    payment: str = "link"  # link | card
    confirm_charge: bool = False  # required when payment == card
    send_email: bool = True
    send_sms: bool = False
    share_superbill: bool = True
    dry_run: bool = True
    specimen: bool = False  # dry run only: mark missing letterhead [not set] instead of refusing
    share_ttl_seconds: int | None = None  # dry run only (expiry proof); live is always 30 days


class BillSuperbillRequest(BaseModel):
    billing_id: str = ""
    diagnoses: list[Any] = []
    save_dx: bool = False
    share_superbill: bool = True
    send_email: bool = True
    send_sms: bool = False
    specimen: bool = False
    share_ttl_seconds: int | None = None


class BillShareRequest(BaseModel):
    billing_id: str = ""
    pay_link: bool = False
    send_email: bool = True
    send_sms: bool = False
    share_ttl_seconds: int | None = None


def _bill_send(*, kind: str, view: dict[str, Any], fields: dict[str, Any], sms_body: str,
               send_email: bool, send_sms: bool, dry: bool, key: str) -> dict[str, Any]:
    from . import billing
    from .email_templates import render_named
    from .notify import normalize_e164, send_named_email
    from .spruce_api import send_spruce_sms

    out: dict[str, Any] = {"email": {"skipped": True}, "sms": {"skipped": True}}
    email = str(view.get("email") or "")
    phone = str(view.get("phone") or "")
    if send_email:
        if not email:
            out["email"] = {"ok": False, "detail": "no email on file"}
        elif dry:
            subject = render_named(kind, {"email": email, **fields})[0]
            out["email"] = {"ok": True, "dry_run": True, "sent": False, "subject": subject}
        else:
            r = send_named_email(kind=kind, to=email, fields=fields)
            out["email"] = {"ok": bool(r.get("ok")), "sent": bool(r.get("ok"))}
    if send_sms:
        e164 = normalize_e164(phone) or ""
        if not billing.sms_enabled():
            out["sms"] = {"ok": False, "skipped": True, "detail": "SMS is off (BILLING_SMS unset or Spruce not configured)"}
        elif not e164:
            out["sms"] = {"ok": False, "detail": "no phone on file"}
        elif dry:
            out["sms"] = {"ok": True, "dry_run": True, "sent": False, "via": "spruce_api",
                          "to": "•••" + e164[-4:], "text": sms_body[:480]}
        else:
            r = send_spruce_sms(e164, sms_body, idempotency_key=key)
            out["sms"] = {"ok": bool(r.get("ok")), "sent": bool(r.get("sent")), "via": "spruce_api",
                          "request_id": r.get("request_id") or "", "detail": r.get("detail") or ""}
    return out


def _bill_share(rec: dict[str, Any], view: dict[str, Any], *, send_email: bool, send_sms: bool,
                ttl: int | None) -> dict[str, Any]:
    from . import billing
    from .superbill_pdf import superbill_filename

    if rec.get("superbill_status") != "saved":
        raise HTTPException(409, "No saved superbill to share yet.")
    dry = bool(rec.get("dry_run"))
    seconds = billing.SHARE_TTL_SECONDS
    if ttl is not None and dry:
        seconds = int(ttl)
    revoked = billing.revoke_shares(rec["id"])
    link = billing.mint_share(
        billing_id=rec["id"],
        client_id=rec["client_id"],
        source=rec.get("superbill_source") or "drive",
        path=rec.get("superbill_path") or "",
        name=superbill_filename(rec["dos"]),
        ttl_seconds=seconds,
    )
    url = f"{PUBLIC_BASE}/sb/{link['token']}"
    inv = rec.get("invoice") or {}
    first = (str(view.get("name") or "there").split() or ["there"])[0]
    sent = _bill_send(
        kind="superbill",
        view=view,
        fields={"name": view.get("name") or "", "title": inv.get("label") or "Visit",
                "amount": inv.get("amount_pretty") or "", "url": url},
        sms_body=f"Hi {first}, your superbill is ready (link good for 30 days): {url} — Dr. B",
        send_email=send_email,
        send_sms=send_sms,
        dry=dry,
        key=f"sb-{link['id']}",
    )
    return {"ok": True, "url": url, "share_id": link["id"], "expires_iso": link["expires_iso"],
            "replaced": revoked, **sent}


def _bill_superbill(rec: dict[str, Any], raw: dict[str, Any], view: dict[str, Any], *,
                    specimen: bool) -> dict[str, Any]:
    from . import billing
    from .practice import asset_bytes, load as load_practice, next_statement
    from .superbill_pdf import superbill_filename, superbill_pdf

    dry = bool(rec.get("dry_run"))
    prac = load_practice()
    missing = billing.letterhead_missing(prac)
    dx = rec.get("dx") or []
    if missing and not (dry and specimen):
        msg = billing.refusal_message(missing)
        billing.update_billing(rec["id"], superbill_status="refused", superbill_detail=msg[:400])
        return {"ok": False, "status": "refused", "missing": missing, "detail": msg}
    if not dx:
        msg = "Superbill not built: add at least one ICD-10 code. The invoice is not affected."
        billing.update_billing(rec["id"], superbill_status="refused", superbill_detail=msg)
        return {"ok": False, "status": "refused", "missing": ["ICD-10 code"], "detail": msg}
    statement = next_statement() if not dry else "DRY-" + rec["id"][:6].upper()
    doc = billing.superbill_doc(
        prac=prac, client=view, rec=rec, dx=dx, statement=statement,
        paid=rec.get("payment_status") == "paid", specimen=bool(missing),
    )
    doc["logo"] = asset_bytes("logo")
    pdf = superbill_pdf(doc)
    fname = superbill_filename(rec["dos"])
    if dry:
        local = f"{rec['id']}.pdf"
        (billing.dryrun_dir() / local).write_bytes(pdf)
        source, path = "local", local
    else:
        folder = _bill_billing_folder(view)
        if not folder:
            msg = "Superbill not saved: no billing folder for this client."
            billing.update_billing(rec["id"], superbill_status="failed", superbill_detail=msg)
            return {"ok": False, "status": "failed", "detail": msg}
        _drive_mkdir(folder)
        path = folder.rstrip("/") + "/" + fname
        _bill_drive_save(path, pdf)
        source = "drive"
    billing.update_billing(
        rec["id"], superbill_status="saved", superbill_source=source, superbill_path=path,
        superbill_detail="specimen (dry run)" if missing else "", statement=statement,
    )
    return {"ok": True, "status": "saved", "source": source, "path": path, "name": fname,
            "statement": statement, "bytes": len(pdf), "specimen": bool(missing)}


@app.post("/api/desk/bill")
def api_desk_bill_confirm(req: BillConfirmRequest, secret: str = Query(default="")):
    """Bill visit: invoice + payment (card or link) + superbill, after the clinician confirms."""
    _require_staff(secret)
    from . import billing
    from .fees import attach_invoice, create as fee_create, set_status
    from .stripe_pay import charge_off_session, create_invoice, ensure_customer, stripe_ready

    raw, view = _bill_client(req.client_id)
    cid = raw["id"]
    codes = billing.load_codes()
    spec = codes["visit_types"].get((req.visit_code or "").strip())
    if not spec:
        raise HTTPException(400, "Pick a visit code.")
    if (req.confirm_code or "").strip() != spec["id"]:
        raise HTTPException(400, "Confirm the visit code first. Nothing was charged or sent.")
    dos = (req.dos or "").strip()[:10]
    try:
        datetime.strptime(dos, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "need a date of service (YYYY-MM-DD)")
    payment = (req.payment or "").strip().lower()
    if payment not in billing.PAYMENTS:
        raise HTTPException(400, "Pick charge card on file or send payment link.")
    card_ready = bool(raw.get("stripe_customer_id") and raw.get("stripe_payment_method_id"))
    if payment == "card":
        if not req.confirm_charge:
            raise HTTPException(400, "Tick the charge confirmation to charge the card on file.")
        if not card_ready:
            raise HTTPException(409, "No card on file. Choose send payment link instead.")
    live = billing.live_enabled()
    dry = bool(req.dry_run) or not live
    if req.specimen and not dry:
        raise HTTPException(400, "specimen is dry-run only")
    if req.share_ttl_seconds is not None and not dry:
        raise HTTPException(400, "share_ttl_seconds is dry-run only")
    if not dry and not stripe_ready():
        raise HTTPException(503, "Stripe is not on the booker")

    dx = billing.clean_dx(req.diagnoses)
    if req.save_dx and dx:
        billing.save_standing_dx(cid, dx)
    key = billing.event_key(req.event_id, cid, dos)
    invoice = billing.build_invoice(spec, codes["pos"], dos)
    rec = billing.claim_billing(
        client_id=cid, key=key, dos=dos, invoice=invoice, dx=dx, payment=payment, dry_run=dry,
    )
    if not rec:
        existing = billing.find_billing(key, dry_run=dry)
        return {"ok": True, "created": False, "detail": "This visit is already billed.",
                "billing": billing.public_billing(existing)}

    pay: dict[str, Any] = {"mode": payment, "dry_run": dry, "charged": False}
    amount = int(invoice["amount_cents"])
    if dry:
        pay["detail"] = (
            f"Dry run: would charge {invoice['amount_pretty']} to the card on file."
            if payment == "card"
            else f"Dry run: would create a {invoice['amount_pretty']} Stripe invoice and send the pay link."
        )
        rec = billing.update_billing(rec["id"], payment_status="dry_run")
    else:
        fee = fee_create(client_id=cid, dos=dos, amount_cents=amount, kind="visit",
                         note=invoice["invoice_code"], event_id=req.event_id or "")
        rec = billing.update_billing(rec["id"], fee_id=str(fee["id"]))
        if payment == "card":
            try:
                result = charge_off_session(
                    customer_id=str(raw["stripe_customer_id"]),
                    payment_method_id=str(raw["stripe_payment_method_id"]),
                    amount_cents=amount, dos=dos, fee_id=str(fee["id"]),
                )
            except RuntimeError as e:
                result = {"ok": False, "error": str(e)[:200], "stripe_id": ""}
            set_status(str(fee["id"]), status="paid" if result.get("ok") else "failed",
                       stripe_id=str(result.get("stripe_id") or ""))
            pay["charged"] = bool(result.get("ok"))
            pay["detail"] = "" if pay["charged"] else str(result.get("error") or "charge failed")
            rec = billing.update_billing(rec["id"], payment_status="paid" if pay["charged"] else "failed",
                                         stripe_id=str(result.get("stripe_id") or ""))
        else:
            try:
                cust = ensure_customer(email=str(raw.get("email") or ""),
                                       existing_id=str(raw.get("stripe_customer_id") or ""), client_id=cid)
                if cust and cust != raw.get("stripe_customer_id"):
                    from . import clients_db

                    clients_db.upsert({"id": cid, "stripe_customer_id": cust})
                inv = create_invoice(customer_id=cust, lines=[fee], client_id=cid)
            except RuntimeError as e:
                rec = billing.update_billing(rec["id"], payment_status="failed")
                raise HTTPException(502, "Invoice not created: " + str(e)[:160]) from e
            attach_invoice([str(fee["id"])], stripe_id=str(inv["stripe_id"]), pay_url=str(inv.get("pay_url") or ""))
            link = billing.mint_share(billing_id=rec["id"], client_id=cid, source="pay", kind="pay",
                                      path=str(inv["stripe_id"]), name=invoice["invoice_code"])
            pay["pay_url"] = f"{PUBLIC_BASE}/pay/{link['token']}"
            rec = billing.update_billing(rec["id"], payment_status="sent", stripe_id=str(inv["stripe_id"]),
                                         pay_url=pay["pay_url"])
    if payment == "link":
        first = (str(view.get("name") or "there").split() or ["there"])[0]
        url = pay.get("pay_url") or (
            f"{SITE_PUBLIC}/pay?" + urlencode({"email": view.get("email") or "", "name": view.get("name") or ""})
        )
        pay["notify"] = _bill_send(
            kind="payment_due", view=view,
            fields={"name": view.get("name") or "", "amount": invoice["amount_pretty"],
                    "title": invoice["label"], "url": url},
            sms_body=f"Hi {first}, your visit balance of {invoice['amount_pretty']} is ready: {url} — Dr. B",
            send_email=req.send_email, send_sms=req.send_sms, dry=dry, key=f"pay-{rec['id']}",
        )

    sb = _bill_superbill(rec, raw, view, specimen=req.specimen)
    rec = billing.get_billing(rec["id"]) or rec
    share: dict[str, Any] = {"skipped": True}
    if sb.get("ok") and req.share_superbill:
        share = _bill_share(rec, view, send_email=req.send_email, send_sms=req.send_sms,
                            ttl=req.share_ttl_seconds)
    return {
        "ok": True,
        "created": True,
        "dry_run": dry,
        "forced_dry_run": dry and not req.dry_run,
        "invoice": invoice,
        "payment": pay,
        "superbill": sb,
        "share": share,
        "billing": billing.public_billing(billing.get_billing(rec["id"])),
    }


@app.post("/api/desk/bill/superbill")
def api_desk_bill_superbill(req: BillSuperbillRequest, secret: str = Query(default="")):
    """Build the superbill for an already-billed visit (e.g. once the letterhead is filled)."""
    _require_staff(secret)
    from . import billing

    rec = billing.get_billing(req.billing_id)
    if not rec:
        raise HTTPException(404, "billing not found")
    if req.specimen and not rec.get("dry_run"):
        raise HTTPException(400, "specimen is dry-run only")
    if req.share_ttl_seconds is not None and not rec.get("dry_run"):
        raise HTTPException(400, "share_ttl_seconds is dry-run only")
    raw, view = _bill_client(rec["client_id"])
    redo_specimen = bool(rec.get("dry_run")) and str(rec.get("superbill_detail") or "").startswith("specimen")
    if rec.get("superbill_status") == "saved" and not (redo_specimen and not req.specimen):
        return {"ok": True, "created": False, "detail": "Superbill already saved.",
                "billing": billing.public_billing(rec)}
    dx = billing.clean_dx(req.diagnoses) or rec.get("dx") or []
    if req.save_dx and dx:
        billing.save_standing_dx(raw["id"], dx)
    rec = billing.update_billing(rec["id"], dx_json=json.dumps(dx))
    sb = _bill_superbill(rec, raw, view, specimen=req.specimen)
    share: dict[str, Any] = {"skipped": True}
    if sb.get("ok") and req.share_superbill:
        share = _bill_share(billing.get_billing(rec["id"]) or rec, view, send_email=req.send_email,
                            send_sms=req.send_sms, ttl=req.share_ttl_seconds)
    return {"ok": True, "created": bool(sb.get("ok")), "superbill": sb, "share": share,
            "billing": billing.public_billing(billing.get_billing(rec["id"]))}


@app.post("/api/desk/bill/share")
def api_desk_bill_share(req: BillShareRequest, secret: str = Query(default="")):
    """Rotate: turn off the old branded link and mint a fresh 30-day one (optionally send it)."""
    _require_staff(secret)
    from . import billing

    rec = billing.get_billing(req.billing_id)
    if not rec:
        raise HTTPException(404, "billing not found")
    if req.share_ttl_seconds is not None and not rec.get("dry_run"):
        raise HTTPException(400, "share_ttl_seconds is dry-run only")
    _raw, view = _bill_client(rec["client_id"])
    return _bill_share(rec, view, send_email=req.send_email, send_sms=req.send_sms, ttl=req.share_ttl_seconds)


@app.post("/api/desk/bill/share/revoke")
def api_desk_bill_share_revoke(req: BillShareRequest, secret: str = Query(default="")):
    _require_staff(secret)
    from . import billing

    if not billing.get_billing(req.billing_id):
        raise HTTPException(404, "billing not found")
    return {"ok": True, "revoked": billing.revoke_shares(req.billing_id, "pay" if req.pay_link else "sb")}


def _share_page(message: str, status: int, title: str = "Superbill link") -> HTMLResponse:
    from html import escape

    from .practice import load as load_practice

    who = str(load_practice().get("display_name") or "")
    body = (
        "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<meta name='robots' content='noindex'><title>{escape(title)}</title>"
        "<body style='font-family:Inter,system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#111'>"
        f"<img src='/api/practice/logo' alt='' style='height:56px'><p style='color:#2f57a5;margin:.4rem 0'>{escape(who)}</p>"
        f"<h1 style='font-size:1.2rem'>{escape(title)}</h1><p>{escape(message)}</p></body>"
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex",
                                                            "Referrer-Policy": "no-referrer"})


_PAY_CLOSED = {
    "paid": "This invoice is paid. Thank you!",
    "void": "This invoice is closed. No payment is needed.",
    "uncollectible": "This invoice is closed. Contact the practice with any questions.",
}


@app.get("/pay/{token}")
def public_pay_link(token: str):
    """Branded pay link (signed, 30-day, revocable): 302 to the Stripe hosted invoice while it is open."""
    from . import billing, stripe_pay

    try:
        row = billing.verify_share(token, kind="pay")
    except billing.ShareError as e:
        return _share_page(e.message, e.status, title="Payment link")
    try:
        inv = stripe_pay._get("/invoices/" + quote(str(row.get("path") or ""), safe=""))
    except RuntimeError:
        return _share_page("The invoice could not be loaded right now. Try again shortly.", 502, title="Payment link")
    status = str(inv.get("status") or "")
    if status in _PAY_CLOSED:
        return _share_page(_PAY_CLOSED[status], 200, title="Payment link")
    url = str(inv.get("hosted_invoice_url") or "")
    if status != "open" or not url.startswith("https://invoice.stripe.com/"):
        return _share_page("This invoice is not available. Contact the practice.", 404, title="Payment link")
    billing.note_hit(row["id"])
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex",
                                                           "Referrer-Policy": "no-referrer"})


@app.get("/sb/{token}")
def public_superbill_share(token: str):
    """Branded, signed, expiring superbill link (30 days; rotate or revoke from the desk)."""
    from . import billing

    try:
        row = billing.verify_share(token)
    except billing.ShareError as e:
        return _share_page(e.message, e.status)
    name = (row.get("name") or "Superbill.pdf").replace('"', "")
    if row.get("source") == "local":
        local = billing.dryrun_dir() / Path(str(row.get("path") or "")).name
        if not local.is_file():
            return _share_page("This file is no longer available.", 404)
        data = local.read_bytes()
    else:
        code, data, _ctype = _notify_get("/drive/file?path=" + quote(str(row.get("path") or ""), safe="/"))
        if code != 200:
            return _share_page("The file could not be loaded right now. Try again shortly.", 502)
    billing.note_hit(row["id"])
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "no-store",
            "X-Robots-Tag": "noindex",
            "Referrer-Policy": "no-referrer",
        },
    )


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


def _folder_names(path: str) -> dict[str, str] | None:
    """{lowercase name: real name} of child folders, or None when the folder is missing/unreadable."""
    data = _drive_list(path)
    if not data.get("ok"):
        return None
    return {str(i.get("name") or "").lower(): str(i.get("name") or "")
            for i in (data.get("items") or []) if (i.get("type") or "") == "folder" and i.get("name")}


def _ensure_visit_notes(chart: str, sub: str = "") -> str:
    """Chart/Visit Notes path for signing (or Chart/<sub>, e.g. Consents). Creates what is missing, never duplicates an existing folder:
    a missing living client folder gets the real-client tree (Billing + Chart/Consents, Labs, Letters,
    Orders, Visit Notes); an existing chart only gets Visit Notes if it has none (case-insensitive)."""
    from .drive_paths import BILLING_CHILD, CHART_SUBFOLDERS, LIVING_ROOT, VISIT_NOTES_FOLDER

    want = sub or VISIT_NOTES_FOLDER
    chart = chart.rstrip("/")
    client_dir, _ = chart.rsplit("/", 1)
    kids = _folder_names(chart)
    if kids is None:
        if not client_dir.startswith(LIVING_ROOT + "/"):
            raise HTTPException(400, "chart folder not found (archive tree is never created)")
        if _folder_names(client_dir) is None:
            if _folder_names(LIVING_ROOT) is None:
                raise HTTPException(502, "Drive is not reachable right now; nothing was created")
            _drive_mkdir(client_dir)
            _drive_mkdir(client_dir + "/" + BILLING_CHILD)
        _drive_mkdir(chart)
        for kid in CHART_SUBFOLDERS:
            _drive_mkdir(chart + "/" + kid)
        if want not in CHART_SUBFOLDERS:
            _drive_mkdir(chart + "/" + want)
        return chart + "/" + want
    have = kids.get(want.lower())
    if have:
        return chart + "/" + have
    _drive_mkdir(chart + "/" + want)
    return chart + "/" + want


def _unique_pdf_name(folder: str, stem: str) -> str:
    """`stem.pdf`, or `stem-2.pdf`, `stem-3.pdf`… when a same-day note is already there (never overwrite)."""
    data = _drive_list(folder)
    names = {str(i.get("name") or "").lower() for i in (data.get("items") or [])}
    taken = lambda n: n.lower() in names or n[:-4].lower() + " chart.pdf" in names  # noqa: E731
    name, k = f"{stem}.pdf", 2
    while taken(name):
        name, k = f"{stem}-{k}.pdf", k + 1
    return name


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
        sms = send_sms(
            phone,
            f"Hi {first}, a file is ready from the practice: {url} — Dr. B",
            kind="file",
            source="desk",
            template="file",
        )

    ok = bool(mail.get("ok") or sms.get("ok"))
    return {
        "ok": ok,
        "cloaked": cloaked,
        "kind": "cloak" if cloaked else "proton",
        "email": {"ok": bool(mail.get("ok")), "skipped": bool(mail.get("skipped")), "detail": mail.get("detail") or ""},
        "sms": {
            "ok": bool(sms.get("ok")),
            "queued": bool(sms.get("queued")),
            "skipped": bool(sms.get("skipped")),
            "detail": sms.get("detail") or "",
        },
    }


@app.post("/api/desk/agent")
def api_desk_agent(
    req: DeskAgentRequest,
    background_tasks: BackgroundTasks,
    secret: str = Query(default=""),
):
    """Pa (Bedrock Nova Pro): look up, create, send forms/mail, book."""
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


def _demo_on() -> bool:
    return (os.environ.get("DEMO") or "").strip().lower() in ("1", "true", "yes", "on")


@app.get("/desk")
def desk_page():
    path = STATIC / "desk.html"
    if not path.exists():
        raise HTTPException(404)
    html = path.read_text(encoding="utf-8")
    if _demo_on():
        if 'data-demo="' not in html:
            html = html.replace("<html", '<html data-demo="1"', 1)
        html = html.replace(
            'src="https://live.psycharts.org/static/js/host-office.js?v=12"',
            'src="" data-stripped-demo-host-office="1"',
        )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


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
