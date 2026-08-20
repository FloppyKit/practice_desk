"""Email (Proton webhook) + optional SMS (Twilio) for booker confirm/remind."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("booker.notify")

NOTIFY_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "http://127.0.0.1:8791/notify")
NOTIFY_TOKEN = os.environ.get("NOTIFY_WEBHOOK_TOKEN", "")
STAFF_NOTIFY = os.environ.get("STAFF_NOTIFY_EMAIL", "matt@drmattbrown.com")
# Comma/semicolon separated extras always notified (in addition to STAFF_NOTIFY_EMAIL)
STAFF_NOTIFY_EXTRA = os.environ.get("STAFF_NOTIFY_EXTRA", "")
PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "https://book.psycharts.org").rstrip("/")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_email_list(raw: str, *, limit: int = 8) -> list[str]:
    """Split comma / semicolon / whitespace list; validate + dedupe (order preserved)."""
    if not raw or not str(raw).strip():
        return []
    parts = re.split(r"[,;\s]+", str(raw).strip())
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        e = p.strip().lower()
        if not e:
            continue
        if not EMAIL_RE.match(e):
            continue
        if e in seen:
            continue
        seen.add(e)
        out.append(e)
        if len(out) >= limit:
            break
    return out


def default_staff_emails() -> list[str]:
    """Primary STAFF_NOTIFY_EMAIL may itself be a list; plus STAFF_NOTIFY_EXTRA."""
    primary = parse_email_list(STAFF_NOTIFY, limit=8)
    extra = parse_email_list(STAFF_NOTIFY_EXTRA, limit=8)
    if not primary:
        primary = ["matt@drmattbrown.com"]
    seen = set(primary)
    for e in extra:
        if e not in seen:
            primary.append(e)
            seen.add(e)
    return primary

# SMS is OFF until A2P approved + ENABLE_SMS=true + credentials
ENABLE_SMS = os.environ.get("ENABLE_SMS", "false").lower() in ("1", "true", "yes")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_SMS_FROM", "")  # e.g. +1913...

LOG_DIR = Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs"))
PHONE_RE = re.compile(r"^\+?[1-9]\d{9,14}$")


def _ensure_log_dir() -> Path:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return LOG_DIR


def append_log(name: str, row: dict[str, Any]) -> None:
    path = _ensure_log_dir() / name
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        log.warning("log write failed %s: %s", path, e)


def send_email(
    to: str,
    subject: str,
    body: str,
    *,
    html: bool = False,
    inline_logo: bool = False,
) -> dict[str, Any]:
    if not NOTIFY_TOKEN:
        result = {"ok": False, "channel": "email", "detail": "NOTIFY_WEBHOOK_TOKEN not set"}
        append_log("notify-failures.jsonl", {**result, "to": to, "subject": subject})
        return result
    payload = {
        "kind": "email",
        "to": to,
        "subject": subject,
        "body": body,
        "html": bool(html),
        "inline_logo": bool(inline_logo and html),
    }
    req = urllib.request.Request(
        NOTIFY_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Notify-Token": NOTIFY_TOKEN,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            data = json.loads(raw) if raw else {"ok": True}
            if not data.get("ok", True):
                append_log(
                    "notify-failures.jsonl",
                    {"channel": "email", "to": to, "subject": subject, "detail": data},
                )
            return {"ok": bool(data.get("ok", True)), "channel": "email", "detail": data}
    except urllib.error.HTTPError as e:
        detail = f"http {e.code}: {e.read()[:200]!r}"
        append_log(
            "notify-failures.jsonl",
            {"ok": False, "channel": "email", "to": to, "subject": subject, "detail": detail},
        )
        return {"ok": False, "channel": "email", "detail": detail}
    except Exception as e:
        append_log(
            "notify-failures.jsonl",
            {"ok": False, "channel": "email", "to": to, "subject": subject, "detail": str(e)},
        )
        return {"ok": False, "channel": "email", "detail": str(e)}


def normalize_e164(phone: str) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"[^\d+]", "", phone.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.isdigit() and len(digits) == 10:
        digits = "+1" + digits
    elif digits.isdigit() and len(digits) == 11 and digits.startswith("1"):
        digits = "+" + digits
    elif not digits.startswith("+") and digits.isdigit():
        digits = "+" + digits
    if PHONE_RE.match(digits):
        return digits
    return None


def send_sms(to_phone: str, body: str) -> dict[str, Any]:
    """Send SMS via Twilio REST. No-op unless ENABLE_SMS and credentials set."""
    if not ENABLE_SMS:
        return {"ok": False, "channel": "sms", "skipped": True, "detail": "ENABLE_SMS=false"}
    e164 = normalize_e164(to_phone)
    if not e164:
        return {"ok": False, "channel": "sms", "detail": "invalid phone"}
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM):
        result = {
            "ok": False,
            "channel": "sms",
            "detail": "Twilio credentials incomplete",
        }
        append_log("notify-failures.jsonl", {**result, "to": e164})
        return result
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    )
    form = urllib.parse.urlencode(
        {"To": e164, "From": TWILIO_FROM, "Body": body[:1500]}
    ).encode()
    import base64

    auth = base64.b64encode(
        f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()
    ).decode()
    req = urllib.request.Request(
        url,
        data=form,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            return {
                "ok": True,
                "channel": "sms",
                "sid": data.get("sid"),
                "to": e164,
            }
    except urllib.error.HTTPError as e:
        detail = f"http {e.code}: {e.read()[:300]!r}"
        append_log(
            "notify-failures.jsonl",
            {"ok": False, "channel": "sms", "to": e164, "detail": detail},
        )
        return {"ok": False, "channel": "sms", "detail": detail}
    except Exception as e:
        append_log(
            "notify-failures.jsonl",
            {"ok": False, "channel": "sms", "to": e164, "detail": str(e)},
        )
        return {"ok": False, "channel": "sms", "detail": str(e)}


# urllib.parse for sms
import urllib.parse  # noqa: E402


def patient_confirm_email(
    *,
    name: str,
    email: str,
    title: str,
    when_label: str,
    cancel_url: str,
    price_label: str = "",
    location_line: str = "Video visit · https://meet.drmattbrown.com",
    rebook_url: str = "",
    duration_minutes: int | None = None,
    start_iso: str | None = None,
    timezone: str = "America/Chicago",
    notes: str = "",
    ics_url: str = "",
) -> dict[str, Any]:
    from .email_templates import render_visit_email

    subject, html, _text = render_visit_email(
        kind="confirm",
        name=name,
        email=email,
        title=title,
        when_label=when_label,
        cancel_url=cancel_url,
        rebook_url=(rebook_url or PUBLIC_BASE).rstrip("/"),
        price_label=price_label,
        location_line=location_line,
        duration_minutes=duration_minutes,
        start_iso=start_iso,
        timezone=timezone,
        notes=notes,
        ics_url=ics_url,
    )
    return send_email(email, subject, html, html=True, inline_logo=True)


def patient_confirm_sms(
    *,
    phone: str,
    title: str,
    when_label: str,
    sms_consent: bool,
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    if not sms_consent:
        return {"ok": False, "channel": "sms", "skipped": True, "detail": "no consent"}
    from .email_templates import confirm_sms

    body = confirm_sms(
        title=title,
        when_label=when_label,
        duration_minutes=duration_minutes,
    )
    return send_sms(phone, body)


def staff_notify_email(
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
    extra_notify: list[str] | None = None,
) -> dict[str, Any]:
    """Notify default staff list + any booking-form extras."""
    recipients = default_staff_emails()
    for e in extra_notify or []:
        el = e.strip().lower()
        if el and el not in recipients and EMAIL_RE.match(el):
            recipients.append(el)

    from .email_templates import render_visit_email

    notify_line = ", ".join(recipients)
    subject, html, _text = render_visit_email(
        kind="staff",
        name=name,
        email=email,
        title=f"{title}: {name}",
        when_label=when_label,
        cancel_url=cancel_url,
        rebook_url="",
        price_label=price_label,
        location_line="Video visit (telehealth) · https://meet.drmattbrown.com",
        notes=notes,
        phone=phone or "",
        sms_consent=sms_consent,
        extra_lines=[
            ("Type", event_type),
            ("Notify list", notify_line),
        ],
    )
    # Keep the subject scannable for the inbox (Cal-style patient mails stay pretty).
    subject = f"[Booker] {title}: {name} — {when_label}"
    results = []
    any_ok = False
    for to in recipients:
        r = send_email(to, subject, html, html=True, inline_logo=True)
        results.append({"to": to, **r})
        if r.get("ok"):
            any_ok = True
    return {"ok": any_ok, "channel": "email", "recipients": results}


def patient_reminder_email(
    *,
    name: str,
    email: str,
    title: str,
    when_label: str,
    kind: str,  # "24h" | "1h"
    cancel_url: str = "",
    rebook_url: str = "",
    duration_minutes: int | None = None,
    start_iso: str | None = None,
    timezone: str = "America/Chicago",
    price_label: str = "",
    location_line: str = "",
    ics_url: str = "",
) -> dict[str, Any]:
    from .email_templates import render_visit_email

    template_kind = "remind_24h" if kind == "24h" else "remind_1h"
    subject, html, _text = render_visit_email(
        kind=template_kind,
        name=name,
        email=email,
        title=title,
        when_label=when_label,
        cancel_url=cancel_url,
        rebook_url=(rebook_url or PUBLIC_BASE).rstrip("/"),
        price_label=price_label,
        location_line=location_line,
        duration_minutes=duration_minutes,
        start_iso=start_iso,
        timezone=timezone,
        ics_url=ics_url,
    )
    return send_email(email, subject, html, html=True, inline_logo=True)


def patient_reminder_sms(
    *,
    phone: str,
    title: str,
    when_label: str,
    kind: str,
    sms_consent: bool,
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    if not sms_consent:
        return {"ok": False, "channel": "sms", "skipped": True, "detail": "no consent"}
    from .email_templates import reminder_sms

    sms_kind = kind if kind in ("15m", "30m", "24h") else "30m"
    body = reminder_sms(
        title=title,
        when_label=when_label,
        kind=sms_kind,
        duration_minutes=duration_minutes,
    )
    return send_sms(phone, body)


def send_named_email(*, kind: str, to: str, fields: dict[str, Any]) -> dict[str, Any]:
    from .email_templates import render_named

    data = dict(fields)
    data.setdefault("email", to)
    subject, html, _text, _sms = render_named(kind, data)
    return send_email(to, subject, html, html=True, inline_logo=True)


def send_late(
    *,
    kind: str,
    name: str,
    email: str,
    phone: str,
    title: str,
    duration_minutes: int | None,
    start_iso: str,
    sms_consent: bool,
    rebook_url: str = "",
    now_iso: str | None = None,
) -> dict[str, Any]:
    from .email_templates import render_late_email

    subject, html, _text, sms = render_late_email(
        kind=kind,
        name=name,
        email=email,
        title=title,
        duration_minutes=duration_minutes,
        start_iso=start_iso,
        now_iso=now_iso,
        rebook_url=rebook_url or PUBLIC_BASE,
    )
    mail: dict[str, Any] = {"ok": False, "detail": "no email"}
    if email:
        mail = send_email(email, subject, html, html=True, inline_logo=True)
    # Staff-fired late notes: send SMS when we have a number (care coordination).
    sms_result: dict[str, Any] = {"ok": False, "skipped": True, "detail": "no phone"}
    if phone:
        sms_result = send_sms(phone, sms)
    return {
        "ok": bool(mail.get("ok") or sms_result.get("ok")),
        "email": mail,
        "sms": sms_result,
        "sms_consent": sms_consent,
    }
