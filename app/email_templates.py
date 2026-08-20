"""Practice emails — one black/white card, blue logo, easy to read.

Visit mail follows Cal.com (What / When / Who / Where) with the length
in the title: "Follow-up · 30 min". The same chrome covers intake, forms,
payment, documents, superbills, and running-late notes.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import escape
from typing import Any

LOGO_CID = "cid:logo.png"
LOGO_HTTPS = "https://book.psycharts.org/api/practice/logo"

HOST_NAME = "Dr. Matt Brown"
HOST_SHORT = "Dr. B"
HOST_EMAIL = "matt@drmattbrown.com"
HOST_PHONE = "773-312-3858"
PRACTICE = "Psych Arts"
MEET_URL = "https://meet.drmattbrown.com"
MEET_HOST = "meet.drmattbrown.com"
DEFAULT_TZ = "America/Chicago"
SITE_URL = "https://drmattbrown.com"


def _identity() -> dict[str, str]:
    try:
        from .practice import load as load_practice

        p = load_practice()
    except Exception:
        return {}
    return {
        "name": p.get("display_name") or HOST_NAME,
        "legal": p.get("legal_name") or PRACTICE,
        "email": p.get("email") or HOST_EMAIL,
        "phone": p.get("phone") or HOST_PHONE,
        "website": p.get("website") or SITE_URL,
        "meet": p.get("meet") or MEET_URL,
    }
FORMS_URL = "https://site.psycharts.org/forms"
ROI_URL = "https://site.psycharts.org/forms/roi"
PAY_URL = "https://site.psycharts.org/pay"
BOOK_URL = "https://book.psycharts.org"

# Staff compose + notify `template=` names
SUITE: dict[str, str] = {
    "confirm": "Visit confirmed",
    "remind_24h": "Reminder · 24 hours",
    "remind_1h": "Reminder · 1 hour",
    "staff": "Staff: new booking",
    "late_soon": "Running late · just after start",
    "late_25": "Running late · ~25 minutes",
    "intake": "Intake packet link",
    "form_share": "Form to complete",
    "forms_bundle": "Forms to complete",
    "book_link": "Personal booking link",
    "payment_due": "Payment due",
    "payment_received": "Payment received",
    "document": "Visit summary ready",
    "superbill": "Superbill ready",
    "cancel": "Visit cancelled",
    "reschedule": "Visit rescheduled",
    "no_show": "No-show close-out",
    "card_on_file": "Card on file",
    "card_failed": "Card / payment failed",
    "after_visit": "After the visit · book next",
    "waitlist": "An opening came up",
    "tech_check": "How to join your first visit",
}


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value), quote=True)


def first_name(name: str) -> str:
    n = (name or "").strip()
    if not n or n.lower() in ("there", "patient"):
        return "there"
    return n.split()[0]


def clock_ampm(dt: datetime) -> str:
    """5:30pm — matches Matt's running-late voice."""
    return dt.strftime("%I:%M%p").lstrip("0").lower()


def visit_heading(title: str, duration_minutes: int | None) -> str:
    """'Follow-up · 30 min' so length is impossible to miss."""
    base = (title or "Visit").strip()
    if not duration_minutes:
        return base
    if re.search(r"\b\d+\s*min", base, re.I):
        return base
    return f"{base} · {int(duration_minutes)} min"


def length_label(duration_minutes: int | None) -> str:
    if not duration_minutes:
        return ""
    n = int(duration_minutes)
    return f"{n} minute" if n == 1 else f"{n} minutes"


def parse_start(start_iso: str | None) -> datetime | None:
    if not start_iso:
        return None
    raw = str(start_iso).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_when(
    *,
    start_iso: str | None = None,
    duration_minutes: int | None = None,
    timezone: str = DEFAULT_TZ,
    fallback: str = "",
) -> tuple[str, str]:
    start = parse_start(start_iso)
    tz = timezone or DEFAULT_TZ
    if start is not None:
        date_line = start.strftime("%A, %B ") + str(start.day) + start.strftime(", %Y")
        t0 = start.strftime("%I:%M %p").lstrip("0")
        if duration_minutes and int(duration_minutes) > 0:
            end = start + timedelta(minutes=int(duration_minutes))
            t1 = end.strftime("%I:%M %p").lstrip("0")
            time_line = f"{t0} – {t1} ({tz})"
        else:
            time_line = f"{t0} ({tz})"
        return date_line, time_line
    if fallback:
        if "·" in fallback:
            left, right = fallback.split("·", 1)
            return left.strip(), f"{right.strip()} ({tz})"
        return fallback, tz
    return "See confirmation", tz


def _btn_primary(href: str, label: str) -> str:
    return (
        f'<a href="{_esc(href)}" target="_blank" '
        'style="display:inline-block;background:#111111;color:#ffffff;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        "font-size:15px;font-weight:600;letter-spacing:-0.01em;line-height:20px;"
        "text-decoration:none;padding:12px 22px;border-radius:8px;"
        f'border:1px solid #111111;">{_esc(label)}</a>'
    )


def _btn_ghost(href: str, label: str) -> str:
    return (
        f'<a href="{_esc(href)}" target="_blank" '
        'style="display:inline-block;background:#ffffff;color:#111111;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        "font-size:14px;font-weight:600;letter-spacing:-0.01em;line-height:20px;"
        "text-decoration:none;padding:10px 16px;border-radius:8px;"
        f'border:1px solid #111111;">{_esc(label)}</a>'
    )


def _row(label: str, value_html: str) -> str:
    return f"""
      <tr>
        <td style="padding:16px 0 0 0;border-top:1px solid #e4e4e7;">
          <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase;color:#71717a;padding-bottom:6px;">{_esc(label)}</div>
          <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:16px;line-height:1.45;color:#111111;">{value_html}</div>
        </td>
      </tr>"""


def render_card(
    *,
    subject: str,
    kicker: str,
    heading: str,
    lede: str,
    rows: list[tuple[str, str]] | None = None,
    letter: str = "",
    primary: tuple[str, str] | None = None,
    ghosts: list[tuple[str, str]] | None = None,
    manage_note: str = "",
    footer_note: str = "Automated message from the practice.",
    logo_src: str = LOGO_CID,
) -> tuple[str, str, str]:
    """Shared chrome. rows = (label, html_or_plain). letter = pre-escaped paragraphs."""
    ident = _identity()
    host_name = ident.get("name") or HOST_NAME
    practice = ident.get("legal") or PRACTICE
    host_email = ident.get("email") or HOST_EMAIL
    host_phone = ident.get("phone") or HOST_PHONE
    site_url = ident.get("website") or SITE_URL
    row_html = ""
    for lab, val in rows or []:
        row_html += _row(lab, val)
    letter_html = ""
    if letter:
        letter_html = (
            '<tr><td style="padding:18px 0 4px 0;border-top:1px solid #e4e4e7;">'
            '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;'
            'font-size:16px;line-height:1.6;color:#111111;white-space:pre-wrap;">'
            f"{letter}</div></td></tr>"
        )
    cta = ""
    if primary:
        cta = (
            '<tr><td style="padding:24px 0 4px 0;border-top:1px solid #e4e4e7;text-align:center;">'
            f"{_btn_primary(primary[0], primary[1])}</td></tr>"
        )
    manage = ""
    if ghosts or manage_note:
        btns = ghosts or []
        btn_cells = ""
        for i, (href, lab) in enumerate(btns):
            pad = "padding:0 8px 0 0;" if i == 0 else ""
            btn_cells += f'<td style="{pad}">{_btn_ghost(href, lab)}</td>'
        note = (
            f'<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;'
            f'font-size:14px;color:#52525b;padding-bottom:14px;">{_esc(manage_note)}</div>'
            if manage_note
            else ""
        )
        manage = f"""
      <tr>
        <td style="padding:22px 0 0 0;border-top:1px solid #e4e4e7;">
          {note}
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr>{btn_cells}</tr>
          </table>
        </td>
      </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="light" />
<meta name="supported-color-schemes" content="light" />
<title>{_esc(subject)}</title>
</head>
<body style="margin:0;padding:0;background:#f6f6f7;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;color:transparent;">
    {_esc(lede or heading)}
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f6f6f7;margin:0;padding:0;">
    <tr>
      <td align="center" style="padding:28px 12px 36px 12px;">
        <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0" style="width:560px;max-width:560px;background:#ffffff;border:1px solid #e4e4e7;border-radius:16px;">
          <tr>
            <td style="padding:32px 32px 8px 32px;text-align:center;">
              <img src="{_esc(logo_src)}" width="72" height="66" alt="{_esc(host_name)}" style="display:block;margin:0 auto 14px auto;border:0;border-radius:50%;width:72px;height:auto;" />
              <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:12px;font-weight:500;letter-spacing:0.02em;color:#71717a;">{_esc(host_name)} · {_esc(practice)}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 32px 6px 32px;text-align:center;">
              <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:13px;color:#52525b;">{_esc(kicker)}</div>
              <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:26px;font-weight:600;letter-spacing:-0.03em;line-height:1.2;color:#111111;padding:8px 0 4px 0;">{_esc(heading)}</div>
              <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5;color:#52525b;">{_esc(lede)}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 32px 8px 32px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                {row_html}
                {letter_html}
                {cta}
                {manage}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 32px 28px 32px;">
              <div style="border-top:1px solid #e4e4e7;padding-top:18px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:13px;line-height:1.55;color:#71717a;text-align:center;">
                Questions — <a href="mailto:{_esc(host_email)}" style="color:#111111;">{_esc(host_email)}</a>
                · {_esc(host_phone)}<br />
                <a href="{_esc(site_url)}" style="color:#52525b;">{_esc(site_url.replace("https://", ""))}</a><br />
                <span style="font-size:12px;">{_esc(footer_note)}</span>
              </div>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    text_rows = []
    for lab, val in rows or []:
        plain = re.sub(r"<[^>]+>", "", val)
        plain = (
            plain.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
        )
        text_rows.append(f"{lab.upper()}\n{plain.strip()}")
    letter_plain = re.sub(r"<br\s*/?>", "\n", letter)
    letter_plain = re.sub(r"<[^>]+>", "", letter_plain)
    parts = [kicker, "", heading, "", lede, ""]
    if text_rows:
        parts.append("\n\n".join(text_rows))
        parts.append("")
    if letter_plain.strip():
        parts.append(letter_plain.strip())
        parts.append("")
    if primary:
        parts.append(f"{primary[1]}: {primary[0]}")
    for href, lab in ghosts or []:
        parts.append(f"{lab}: {href}")
    parts.extend(
        [
            "",
            f"Questions: {host_email} · {host_phone}",
            site_url,
            "",
            f"— {host_name} · {practice}",
        ]
    )
    return subject, html, "\n".join(parts).strip() + "\n"


def _visit_copy(kind: str, heading: str) -> tuple[str, str, str]:
    if kind == "confirm":
        return (
            f"Confirmed: {heading}",
            "This visit is scheduled.",
            "Your time is on the practice calendar. Details are below.",
        )
    if kind == "remind_24h":
        return (
            f"Reminder: {heading} tomorrow",
            "A reminder from Dr. Matt Brown",
            "Your visit is tomorrow.",
        )
    if kind == "remind_1h":
        return (
            f"Reminder: {heading} in 1 hour",
            "A reminder from Dr. Matt Brown",
            "Your visit is in about an hour.",
        )
    if kind == "cancel":
        return (
            f"Cancelled: {heading}",
            "This visit has been cancelled.",
            "It is no longer on the practice calendar. Book a new time if you still need to meet.",
        )
    if kind == "reschedule":
        return (
            f"Rescheduled: {heading}",
            "Your visit has a new time.",
            "The old time is off the calendar. Details for the new visit are below.",
        )
    return (
        f"[Booker] {heading}",
        "New booking",
        "A visit was just added on the practice calendar.",
    )


def render_visit_email(
    *,
    kind: str,
    name: str,
    email: str,
    title: str,
    when_label: str,
    cancel_url: str = "",
    rebook_url: str = "",
    price_label: str = "",
    location_line: str = "",
    duration_minutes: int | None = None,
    start_iso: str | None = None,
    timezone: str = DEFAULT_TZ,
    notes: str = "",
    phone: str = "",
    sms_consent: bool | None = None,
    extra_lines: list[tuple[str, str]] | None = None,
    logo_src: str = LOGO_CID,
    meet_url: str = MEET_URL,
    ics_url: str = "",
) -> tuple[str, str, str]:
    heading = visit_heading(title, duration_minutes)
    subject, kicker, lede = _visit_copy(kind, heading)
    date_line, time_line = format_when(
        start_iso=start_iso,
        duration_minutes=duration_minutes,
        timezone=timezone,
        fallback=when_label,
    )
    greet = (name or "").strip() or "there"
    loc_name = "Video visit (telehealth)"
    loc_url = meet_url or MEET_URL
    if location_line:
        if "·" in location_line:
            loc_name, rest = location_line.split("·", 1)
            loc_name = loc_name.strip()
            rest = rest.strip()
            if rest:
                loc_url = rest
        else:
            loc_name = location_line.strip()

    who_html = (
        f'<div style="font-weight:600;">{_esc(HOST_NAME)}</div>'
        f'<div style="font-size:14px;color:#52525b;padding-top:2px;">Organizer · {_esc(HOST_EMAIL)}</div>'
        f'<div style="height:12px;line-height:12px;font-size:12px;">&nbsp;</div>'
        f'<div style="font-weight:600;">{_esc(greet)}</div>'
        f'<div style="font-size:14px;color:#52525b;padding-top:2px;">{_esc(email)}</div>'
    )
    when_html = (
        f'<div style="font-weight:600;">{_esc(date_line)}</div>'
        f'<div style="font-size:15px;padding-top:2px;">{_esc(time_line)}</div>'
    )
    where_html = (
        f'<div style="font-weight:600;">{_esc(loc_name)}</div>'
        f'<div style="font-size:15px;padding-top:2px;">'
        f'<a href="{_esc(loc_url)}" style="color:#111111;">{_esc(MEET_HOST)}</a></div>'
    )
    rows: list[tuple[str, str]] = [
        ("What", f'<div style="font-weight:600;">{_esc(heading)}</div>'),
    ]
    length = length_label(duration_minutes)
    if length:
        rows.append(("Length", f'<div style="font-weight:600;">{_esc(length)}</div>'))
    rows.extend(
        [
            ("When", when_html),
            ("Who", who_html),
            ("Where", where_html),
        ]
    )
    if price_label:
        fee_note = (
            "No fee for this visit."
            if price_label.lower() in ("no charge", "free", "$0")
            else "Due on the day of the visit."
        )
        rows.append(
            (
                "Fee",
                f'<div style="font-weight:600;">{_esc(price_label)}</div>'
                f'<div style="font-size:14px;color:#52525b;padding-top:2px;">{fee_note}</div>',
            )
        )
    if notes and kind != "staff":
        rows.append(("Notes", f'<div style="white-space:pre-wrap;">{_esc(notes)}</div>'))
    if kind == "staff":
        if phone:
            rows.append(("Phone", _esc(phone)))
        if sms_consent is not None:
            rows.append(("SMS consent", "yes" if sms_consent else "no"))
        if notes:
            rows.append(("Notes", _esc(notes)))
    for lab, val in extra_lines or []:
        rows.append((lab, _esc(val)))

    primary = None if kind == "cancel" else (loc_url, "Join visit")
    ghosts: list[tuple[str, str]] = []
    manage = ""
    if kind == "cancel":
        if rebook_url:
            primary = (rebook_url, "Book a new time")
    else:
        if ics_url and kind != "staff":
            ghosts.append((ics_url, "Add to calendar"))
        if cancel_url or rebook_url:
            manage = "Cancel this visit, then pick a new time if you need to reschedule."
        if cancel_url:
            ghosts.append((cancel_url, "Cancel"))
        if rebook_url:
            ghosts.append((rebook_url, "Book a new time"))

    return render_card(
        subject=subject,
        kicker=kicker,
        heading=heading,
        lede=lede,
        rows=rows,
        primary=primary,
        ghosts=ghosts,
        manage_note=manage,
        footer_note="Automated message from the practice booker.",
        logo_src=logo_src,
    )


def late_soon_text(*, name: str, start: datetime, now: datetime) -> str:
    who = first_name(name)
    return (
        f"Hey {who},\n\n"
        f"We had an appointment today online at {clock_ampm(start)}. "
        f"It’s currently {clock_ampm(now)} and I have not seen you pop on yet. "
        f"Are you just running a few min behind schedule?\n\n"
        f"As a reminder, to access your visit go to: {MEET_HOST}\n\n"
        f"{HOST_SHORT}"
    )


def late_25_text(*, now: datetime) -> str:
    return (
        f"Hello again. It's currently {clock_ampm(now)} and I still haven't seen "
        f"or heard from you. I do hope all is well. Clearly something must have come up. "
        f"Please let me know when you might like to reschedule."
    )


def render_late_email(
    *,
    kind: str,  # late_soon | late_25
    name: str,
    email: str,
    title: str,
    duration_minutes: int | None,
    start_iso: str,
    now_iso: str | None = None,
    timezone: str = DEFAULT_TZ,
    rebook_url: str = BOOK_URL,
    logo_src: str = LOGO_CID,
) -> tuple[str, str, str, str]:
    """Return subject, html, text, sms."""
    start = parse_start(start_iso) or datetime.now()
    now = parse_start(now_iso) or datetime.now(start.tzinfo)
    heading = visit_heading(title, duration_minutes)
    if kind == "late_25":
        letter = late_25_text(now=now)
        sms = letter
        subject = f"Checking in — {heading}"
        kicker = "A note from Dr. B"
        lede = "I still haven’t seen you on the visit."
        primary = (rebook_url or BOOK_URL, "Book a new time")
        ghosts = [(MEET_URL, "Join anyway")]
    else:
        letter = late_soon_text(name=name, start=start, now=now)
        sms = letter
        subject = f"Are you running a few minutes behind? — {heading}"
        kicker = "A note from Dr. B"
        lede = "Just checking in — the visit time has started."
        primary = (MEET_URL, "Join visit")
        ghosts = [(rebook_url or BOOK_URL, "Need to reschedule?")]
    # letter is plain; escape for HTML card
    subject_out, html, text = render_card(
        subject=subject,
        kicker=kicker,
        heading=heading,
        lede=lede,
        letter=_esc(letter),
        primary=primary,
        ghosts=ghosts,
        logo_src=logo_src,
        footer_note="Sent from the practice booker.",
    )
    return subject_out, html, text, sms


def reminder_sms(*, title: str, when_label: str, kind: str, duration_minutes: int | None = None) -> str:
    heading = visit_heading(title, duration_minutes)
    if kind == "15m":
        horizon = "in ~15 min"
    elif kind == "30m":
        horizon = "in ~30 min"
    elif kind == "24h":
        horizon = "tomorrow"
    else:
        horizon = "soon"
    return (
        f"Brown Psychiatric Services: Reminder — {heading} {horizon} "
        f"({when_label} CT). Join: {MEET_HOST} "
        f"Reply STOP to opt out, HELP for help. Msg & data rates may apply."
    )


def confirm_sms(*, title: str, when_label: str, duration_minutes: int | None = None) -> str:
    heading = visit_heading(title, duration_minutes)
    return (
        f"Brown Psychiatric Services: Confirmed {heading} on {when_label} CT. "
        f"Join: {MEET_HOST} Reply STOP to opt out, HELP for help. "
        f"Msg & data rates may apply."
    )


def _f(fields: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        if fields.get(k) not in (None, ""):
            return str(fields[k])
    return default


def _f_int(fields: dict[str, Any], *keys: str) -> int | None:
    for k in keys:
        v = fields.get(k)
        if v in (None, ""):
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def render_named(
    kind: str,
    fields: dict[str, Any] | None = None,
    *,
    logo_src: str = LOGO_CID,
) -> tuple[str, str, str, str]:
    """Render any suite kind. Returns subject, html, text, sms (sms may be empty)."""
    f = dict(fields or {})
    kind = (kind or "").strip()
    name = _f(f, "name", "patient_name")
    email = _f(f, "email", "to")
    title = _f(f, "title", default="Visit")
    when_label = _f(f, "when_label")
    duration = _f_int(f, "duration_minutes")
    start_iso = _f(f, "start_iso") or None
    timezone = _f(f, "timezone", default=DEFAULT_TZ)
    url = _f(f, "url", "link")
    logo = _f(f, "logo_src", default=logo_src)

    visit_kinds = {"confirm", "remind_24h", "remind_1h", "staff", "cancel", "reschedule"}
    if kind in visit_kinds:
        extra: list[tuple[str, str]] = []
        if kind == "staff":
            if _f(f, "event_type"):
                extra.append(("Type", _f(f, "event_type")))
            if _f(f, "notify_list"):
                extra.append(("Notify list", _f(f, "notify_list")))
        sub, html, text = render_visit_email(
            kind=kind,
            name=name,
            email=email,
            title=title,
            when_label=when_label,
            cancel_url=_f(f, "cancel_url"),
            rebook_url=_f(f, "rebook_url", default=BOOK_URL),
            price_label=_f(f, "price_label", "amount"),
            location_line=_f(f, "location_line"),
            duration_minutes=duration,
            start_iso=start_iso,
            timezone=timezone,
            notes=_f(f, "notes"),
            phone=_f(f, "phone"),
            sms_consent=f.get("sms_consent") if "sms_consent" in f else None,
            extra_lines=extra or None,
            logo_src=logo,
            ics_url=_f(f, "ics_url"),
        )
        sms = ""
        if kind == "confirm":
            sms = confirm_sms(title=title, when_label=when_label, duration_minutes=duration)
        return sub, html, text, sms

    if kind in ("late_soon", "late_25"):
        return render_late_email(
            kind=kind,
            name=name,
            email=email,
            title=title,
            duration_minutes=duration,
            start_iso=start_iso or datetime.now().isoformat(),
            now_iso=_f(f, "now_iso") or None,
            timezone=timezone,
            rebook_url=_f(f, "rebook_url", default=BOOK_URL),
            logo_src=logo,
        )

    if kind == "intake":
        who = first_name(name) if name else ""
        hi = f"Hi {who}," if who and who != "there" else "Hi,"
        link = url
        letter = _esc(
            f"{hi}\n\n"
            "Please complete your intake packet with this personal link. "
            "Opening it helps confirm this email belongs to you. It expires in about 3 weeks.\n\n"
            "The packet covers personal information, consents and policies, and payment authorization "
            "(no card number on the form).\n\n"
            "Prefer not to forward this if you want the signature binding tied to your inbox."
        )
        sub, html, text = render_card(
            subject="Your secure intake link — Dr. Matt Brown",
            kicker="A note from Dr. Matt Brown",
            heading="Your intake packet",
            lede="A personal link to complete forms before we meet.",
            letter=letter,
            primary=(link, "Open intake") if link else None,
            rows=[("Link", f'<a href="{_esc(link)}" style="color:#111111;word-break:break-all;">{_esc(link)}</a>')]
            if link
            else None,
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "form_share":
        form_name = _f(f, "form_name", "form", default="a form")
        note = _f(f, "notes", "note")
        link = url or ROI_URL
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            f"I am sending {form_name} for you to complete when you have a moment."
            + (f"\n\n{note}" if note else "")
        )
        sub, html, text = render_card(
            subject=f"A form to complete — {form_name}",
            kicker="From Dr. Matt Brown",
            heading=form_name,
            lede="Please open the form when you can.",
            letter=letter,
            rows=[("Form", f'<div style="font-weight:600;">{_esc(form_name)}</div>')],
            primary=(link, "Open form"),
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "forms_bundle":
        raw_links = f.get("links") or []
        pairs: list[tuple[str, str]] = []
        if isinstance(raw_links, list):
            for row in raw_links:
                if isinstance(row, dict) and row.get("url"):
                    pairs.append((str(row.get("label") or "Form"), str(row["url"])))
        if not pairs and url:
            pairs.append((_f(f, "form_name", default="Form"), url))
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "Here are the forms to complete when you have a moment."
            + (f"\n\n{_f(f, 'notes', 'note')}" if _f(f, "notes", "note") else "")
        )
        rows = [
            (
                lab,
                f'<a href="{_esc(href)}" style="color:#111111;word-break:break-all;">{_esc(href)}</a>',
            )
            for lab, href in pairs
        ]
        sub, html, text = render_card(
            subject="Forms to complete — Dr. Matt Brown",
            kicker="From Dr. Matt Brown",
            heading="A few forms for you",
            lede="Open each link when you can.",
            letter=letter,
            primary=(pairs[0][1], "Open first form") if pairs else None,
            rows=rows or None,
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "book_link":
        visit = _f(f, "title", "form_name", default="a visit")
        link = url or f"{BOOK_URL}/follow-up"
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            f"Here is your personal link to book {visit}. "
            "Your name and contact are already filled in — pick a time and confirm."
        )
        sub, html, text = render_card(
            subject=f"Book your {visit} — Dr. Matt Brown",
            kicker="From Dr. Matt Brown",
            heading="Pick a time",
            lede="One tap after you choose a slot. Your details are already on the form.",
            letter=letter,
            primary=(link, "Pick a time"),
            rows=[
                ("Visit", f'<div style="font-weight:600;">{_esc(visit)}</div>'),
                ("Link", f'<a href="{_esc(link)}" style="color:#111111;word-break:break-all;">{_esc(link)}</a>'),
            ],
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "payment_due":
        amount = _f(f, "amount", "price_label")
        due = _f(f, "due_date", "when_label")
        visit = visit_heading(_f(f, "title", default="Visit"), duration)
        link = url or PAY_URL
        rows = [("Visit", f'<div style="font-weight:600;">{_esc(visit)}</div>')]
        if amount:
            rows.append(("Amount due", f'<div style="font-weight:600;">{_esc(amount)}</div>'))
        if due:
            rows.append(("Due", _esc(due)))
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "This is a note that payment is due for your visit. "
            "HSA/FSA cards are welcome. Reply if anything looks off."
        )
        sub, html, text = render_card(
            subject=f"Payment due — {amount or visit}",
            kicker="A note from the practice",
            heading="Payment due",
            lede="A balance is waiting when you are ready.",
            letter=letter,
            rows=rows,
            primary=(link, "Pay now"),
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "payment_received":
        amount = _f(f, "amount", "price_label")
        when = _f(f, "when_label", "paid_at")
        visit = visit_heading(_f(f, "title", default="Visit"), duration)
        rows = [("Visit", f'<div style="font-weight:600;">{_esc(visit)}</div>')]
        if amount:
            rows.append(("Amount", f'<div style="font-weight:600;">{_esc(amount)}</div>'))
        if when:
            rows.append(("Received", _esc(when)))
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "Thank you — this payment is on the practice books. "
            "A superbill can be sent separately when you need one for your insurer."
        )
        sub, html, text = render_card(
            subject=f"Payment received — {amount or visit}",
            kicker="Thank you",
            heading="Payment received",
            lede="This is your receipt note from the practice.",
            letter=letter,
            rows=rows,
            primary=(url, "View receipt") if url else None,
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "document":
        doc = _f(f, "doc_title", "title", default="Visit summary")
        link = url
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            f"A new document is available for you: {doc}. "
            "You can open it from your documents folder with the button below."
        )
        sub, html, text = render_card(
            subject=f"A document is ready — {doc}",
            kicker="From Dr. Matt Brown",
            heading="A document is ready",
            lede=doc,
            letter=letter,
            rows=[("Document", f'<div style="font-weight:600;">{_esc(doc)}</div>')],
            primary=(link, "Open document") if link else None,
            logo_src=logo,
        )
        sms = ""
        if link:
            sms = (
                f"Hi {first_name(name)}, a file is ready from the practice: {link} — Dr. B"
            )
        return sub, html, text, sms

    if kind == "superbill":
        visit = visit_heading(_f(f, "title", default="Visit"), duration)
        amount = _f(f, "amount")
        link = url
        rows = [("Visit", f'<div style="font-weight:600;">{_esc(visit)}</div>')]
        if amount:
            rows.append(("Amount", _esc(amount)))
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "Your superbill is ready to download from the billing folder. "
            "You can submit it to a PPO yourself for possible out-of-network reimbursement."
        )
        sub, html, text = render_card(
            subject="Your superbill is ready — Dr. Matt Brown",
            kicker="Billing",
            heading="Your superbill is ready",
            lede="Download it from the billing folder when you like.",
            letter=letter,
            rows=rows,
            primary=(link, "Download superbill") if link else None,
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "no_show":
        visit = visit_heading(title, duration)
        date_line, time_line = format_when(
            start_iso=start_iso, duration_minutes=duration, timezone=timezone, fallback=when_label
        )
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "I did not see you on the visit today, and I hope everything is all right. "
            "When a visit is missed without notice, the time is held for you and is charged "
            "in full per practice policy.\n\n"
            "When you are ready, pick a new time below. Reply if something came up "
            "and we should talk first."
        )
        sub, html, text = render_card(
            subject=f"We missed you today — {visit}",
            kicker="A note from Dr. B",
            heading="We missed you today",
            lede=visit,
            letter=letter,
            rows=[
                ("Visit", f'<div style="font-weight:600;">{_esc(visit)}</div>'),
                ("When it was", f'<div style="font-weight:600;">{_esc(date_line)}</div><div style="font-size:15px;padding-top:2px;">{_esc(time_line)}</div>'),
            ],
            primary=(_f(f, "rebook_url", default=BOOK_URL), "Book a new time"),
            logo_src=logo,
        )
        sms = (
            f"Hi {first_name(name)}, I did not see you on today's visit and hope you are well. "
            f"Please book a new time when you can: {BOOK_URL.replace('https://', '')} — Dr. B"
        )
        return sub, html, text, sms

    if kind == "card_on_file":
        link = url or PAY_URL
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "Please keep a card on file for visits and for late cancellations, per practice policy. "
            "HSA/FSA cards are welcome. We do not store the full card number on this form — "
            "you will use the secure payment page."
        )
        sub, html, text = render_card(
            subject="Card on file — Dr. Matt Brown",
            kicker="A note from the practice",
            heading="Card on file",
            lede="A secure page to save a payment method.",
            letter=letter,
            primary=(link, "Save a card"),
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "card_failed":
        amount = _f(f, "amount", "price_label")
        visit = visit_heading(_f(f, "title", default="Visit"), duration)
        link = url or PAY_URL
        rows = []
        if visit:
            rows.append(("Visit", f'<div style="font-weight:600;">{_esc(visit)}</div>'))
        if amount:
            rows.append(("Amount", f'<div style="font-weight:600;">{_esc(amount)}</div>'))
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "The payment method on file did not go through. Nothing else is needed from you "
            "except to update the card (or try another method) so we can close this out.\n\n"
            "If you think this is a bank block or HSA PIN issue, reply and we will sort it."
        )
        sub, html, text = render_card(
            subject=f"Payment did not go through — {amount or visit}",
            kicker="A note from the practice",
            heading="Payment did not go through",
            lede="Please update the card on file when you can.",
            letter=letter,
            rows=rows or None,
            primary=(link, "Update payment method"),
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "after_visit":
        visit = visit_heading(title, duration)
        next_url = url or _f(f, "rebook_url", default=f"{BOOK_URL}/follow-up")
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "Thank you for meeting today. When you are ready to schedule the next visit, "
            "use the button below — it goes straight to follow-up times.\n\n"
            + (_f(f, "notes") or "If we talked about a specific interval, book that window so the calendar stays honest.")
        )
        sub, html, text = render_card(
            subject="Next visit — Dr. Matt Brown",
            kicker="After today",
            heading="Book the next visit",
            lede=visit if title else "Whenever you are ready.",
            letter=letter,
            primary=(next_url, "Pick a follow-up time"),
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "waitlist":
        visit = visit_heading(_f(f, "title", default="Follow-up"), duration)
        slot = _f(f, "when_label", "slot")
        link = url or _f(f, "rebook_url", default=BOOK_URL)
        rows = [("Visit type", f'<div style="font-weight:600;">{_esc(visit)}</div>')]
        if slot:
            rows.append(("Opening", f'<div style="font-weight:600;">{_esc(slot)}</div>'))
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "An opening came up on the calendar"
            + (f" — {slot}" if slot else "")
            + ". If you want it, grab it with the button below. "
            "If someone else books it first, the link will simply show what is still open."
        )
        sub, html, text = render_card(
            subject=f"An opening came up — {visit}",
            kicker="A note from Dr. Matt Brown",
            heading="An opening came up",
            lede="Yours if you want it.",
            letter=letter,
            rows=rows,
            primary=(link, "Take this time"),
            logo_src=logo,
        )
        return sub, html, text, ""

    if kind == "tech_check":
        visit = visit_heading(title or "New client visit", duration)
        date_line, time_line = format_when(
            start_iso=start_iso, duration_minutes=duration, timezone=timezone, fallback=when_label
        )
        letter = _esc(
            f"Hi {first_name(name)},\n\n"
            "A few minutes before we meet, this is all you need:\n\n"
            "• Join at meet.drmattbrown.com — same link every time.\n"
            "• Use Chrome or Safari on a computer if you can. Phones work; a laptop is easier.\n"
            "• Allow camera and microphone when the browser asks.\n"
            "• Join about 5 minutes early so we are not starting with a software hiccup.\n"
            "• Headphones help. Sit somewhere private with a stable connection.\n\n"
            "If the page will not open, try a different browser or reply and we will sort it."
        )
        rows = [("Visit", f'<div style="font-weight:600;">{_esc(visit)}</div>')]
        if date_line and date_line != "See confirmation":
            rows.append(
                (
                    "When",
                    f'<div style="font-weight:600;">{_esc(date_line)}</div>'
                    f'<div style="font-size:15px;padding-top:2px;">{_esc(time_line)}</div>',
                )
            )
        rows.append(("Where", f'<a href="{_esc(MEET_URL)}" style="color:#111111;">{_esc(MEET_HOST)}</a>'))
        sub, html, text = render_card(
            subject=f"How to join — {visit}",
            kicker="Before your first visit",
            heading="How to join",
            lede="Five minutes early. Chrome or Safari. Camera on.",
            letter=letter,
            rows=rows,
            primary=(MEET_URL, "Test the join link"),
            logo_src=logo,
        )
        return sub, html, text, ""

    raise ValueError(f"unknown email kind: {kind}")


def preview_samples(logo_src: str = LOGO_HTTPS) -> dict[str, tuple[str, str, str]]:
    common = dict(
        name="Alex Rivera",
        email="alex@example.com",
        title="Follow-up",
        when_label="Tue Aug 18 · 2:00 PM",
        cancel_url="https://book.psycharts.org/cancel?token=preview",
        rebook_url="https://book.psycharts.org/follow-up",
        price_label="$265",
        location_line="Video visit (telehealth) · https://meet.drmattbrown.com",
        duration_minutes=30,
        start_iso="2026-08-18T14:00:00-05:00",
        timezone=DEFAULT_TZ,
        ics_url="https://book.psycharts.org/cal/visit.ics?token=preview",
        logo_src=logo_src,
    )
    out: dict[str, tuple[str, str, str]] = {
        "confirm": render_visit_email(kind="confirm", **common),
        "remind_24h": render_visit_email(kind="remind_24h", **common),
        "remind_1h": render_visit_email(kind="remind_1h", **common),
        "confirm_60": render_visit_email(kind="confirm", **{**common, "duration_minutes": 60, "price_label": "$420"}),
    }
    return out
