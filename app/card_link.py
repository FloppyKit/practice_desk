"""Card-change link: send the /pay link to the client's email (and Spruce text) on file. No PHI. Matt 2026-11-04."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

WINDOW_SECONDS = 600  # one link per client per 10 minutes
_LOCK = threading.Lock()

SUBJECT = "Update your card on file"
EMAIL_BODY = (
    "Hello,\n\n"
    "Here is the secure link to update your card on file with Dr. Matt Brown's Clinic:\n\n"
    "{url}\n\n"
    "Nothing is charged when you save.\n\n"
    "Dr. Matt Brown's Clinic"
)
SMS_BODY = "Dr. Matt Brown's Clinic: update your card on file here: {url} Nothing is charged when you save."


def _state_path() -> Path:
    return Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs")) / "card-links.json"


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return ""
    return f"{local[0]}{'*' * max(3, len(local) - 1)}@{domain}"


def mask_phone(phone: str) -> str:
    digits = "".join(c for c in (phone or "") if c.isdigit())
    return f"the mobile ending {digits[-4:]}" if len(digits) >= 4 else ""


def _claim(client_id: str, now: float) -> bool:
    """True if no link went to this client in the last WINDOW_SECONDS (and records this one)."""
    with _LOCK:
        p = _state_path()
        try:
            seen = json.loads(p.read_text()) if p.is_file() else {}
        except (OSError, ValueError):
            seen = {}
        last = float(seen.get(client_id) or 0)
        if now - last < WINDOW_SECONDS:
            return False
        seen = {k: v for k, v in seen.items() if now - float(v) < WINDOW_SECONDS}
        seen[client_id] = now
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(seen))
        tmp.replace(p)
        return True


def send_card_link(
    row: dict[str, Any],
    *,
    pay_url: str,
    send_email: Callable[[str, str, str], dict[str, Any]],
    send_sms: Callable[[str, str], dict[str, Any]] | None,
    now: float | None = None,
) -> dict[str, str]:
    """row comes from the verified login token only. Returns {status, hint}; never the address or token."""
    cid = str(row.get("id") or "")
    email = str(row.get("email") or "").strip()
    phone = str(row.get("phone_e164") or "").strip()
    texting = bool(send_sms and phone and int(row.get("sms_consent") or 0) == 1)
    hints = [h for h in (mask_email(email), mask_phone(phone) if texting else "") if h]
    hint = " and ".join(hints)
    if not cid or not (email or texting):
        return {"status": "no_contact", "hint": ""}
    if not _claim(cid, time.time() if now is None else now):
        return {"status": "rate_limited", "hint": hint}
    ok = False
    if email:
        ok = bool(send_email(email, SUBJECT, EMAIL_BODY.format(url=pay_url)).get("ok")) or ok
    if texting and send_sms:
        ok = bool(send_sms(phone, SMS_BODY.format(url=pay_url)).get("ok")) or ok
    return {"status": "sent" if ok else "failed", "hint": hint if ok else ""}
