"""One-time code to the email or mobile already on the account.

Name and date of birth are not verifiers. A code is sent only to a channel
that matches the phone book. The public start response does not say whether
the account exists and does not return a chart link.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from .clients_db import get_by_email, get_by_phone, list_by_email, list_by_phone
from .patients import normalize_phone

CODE_TTL_SECONDS = 10 * 60
MAX_SENDS = 3
SEND_WINDOW_SECONDS = 15 * 60
MAX_ATTEMPTS = 5

Sender = Callable[[str, str, str], dict[str, Any]]


def _dir() -> Path:
    root = Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs")) / "verify-codes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _secret() -> bytes:
    raw = os.environ.get("VERIFY_CODE_SECRET", "").strip()
    if raw:
        return raw.encode()
    path = _dir() / "secret"
    if not path.exists():
        path.write_text(secrets.token_hex(32), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return path.read_text(encoding="utf-8").strip().encode()


def _mac(value: str) -> str:
    return hmac.new(_secret(), value.encode(), hashlib.sha256).hexdigest()


def _path(channel: str, destination: str) -> Path:
    return _dir() / (_mac(f"{channel}:{destination}") + ".json")


def _read(channel: str, destination: str) -> dict[str, Any] | None:
    path = _path(channel, destination)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write(channel: str, destination: str, row: dict[str, Any]) -> None:
    path = _path(channel, destination)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row), encoding="utf-8")
    tmp.replace(path)


def recognize_for_message(*, email: str = "", phone: str = "") -> dict[str, Any]:
    """Phone or email on file can file a message. Never returns the name (no identity before a code)."""
    found = _lookup(email, phone)
    if not found:
        return {"ok": False, "level": "none", "reason": "not_found"}
    return {"ok": True, "level": "message"}


def _public_pending() -> dict[str, Any]:
    return {
        "ok": True,
        "delivery": "pending",
        "message": "If that email or mobile is on the account, a code is on the way.",
    }


def _lookup(email: str, phone: str) -> tuple[str, str, dict[str, Any]] | None:
    em = (email or "").strip().lower()
    if em and "@" in em:
        row = get_by_email(em)
        if row and (row.get("status") or "active").lower() != "archived":
            return "email", em, row
        return None
    e164 = normalize_phone(phone) if phone else None
    if e164:
        row = get_by_phone(e164)
        if row and (row.get("status") or "active").lower() != "archived":
            return "sms", e164, row
    return None


def _default_sender(channel: str, destination: str, code: str) -> dict[str, Any]:
    body = f"Dr. Matt Brown code: {code}. It expires in 10 minutes."
    if channel == "sms":
        from .notify import send_sms

        return send_sms(
            destination,
            body,
            kind="verify_code",
            source="verify",
            idempotency_key=f"verify:{destination}:{int(time.time()) // 30}",
        )
    from .notify import send_email

    return send_email(
        destination,
        "Your Dr. Matt Brown code",
        body,
    )


def start_verify(
    *,
    email: str = "",
    phone: str = "",
    sender: Sender | None = None,
) -> dict[str, Any]:
    """Send a code if the channel matches an account. Never match on name or DOB."""
    found = _lookup(email, phone)
    if not found:
        return _public_pending()
    channel, destination, row = found
    now = int(time.time())
    current = _read(channel, destination) or {}
    sends = [int(t) for t in current.get("sends") or [] if now - int(t) < SEND_WINDOW_SECONDS]
    if len(sends) >= MAX_SENDS:
        return _public_pending()
    code = f"{secrets.randbelow(1_000_000):06d}"
    send = sender or _default_sender
    result = send(channel, destination, code)
    if not result.get("ok", True):
        return _public_pending()
    name = row.get("preferred_name") or row.get("display_name") or ""
    _write(
        channel,
        destination,
        {
            "channel": channel,
            "code_hash": _mac(code),
            "client_id": str(row.get("id") or ""),
            "email": str(row.get("email") or ""),
            "name": str(name)[:80],
            "exp": now + CODE_TTL_SECONDS,
            "attempts": 0,
            "sends": sends + [now],
        },
    )
    return _public_pending()


def confirm_code(
    *,
    email: str = "",
    phone: str = "",
    code: str = "",
) -> dict[str, Any]:
    found = _lookup(email, phone)
    if not found:
        return {"ok": False, "reason": "invalid_code"}
    channel, destination, _row = found
    stored = _read(channel, destination)
    if not stored:
        return {"ok": False, "reason": "invalid_code"}
    now = int(time.time())
    if int(stored.get("exp") or 0) < now:
        return {"ok": False, "reason": "invalid_code"}
    attempts = int(stored.get("attempts") or 0)
    if attempts >= MAX_ATTEMPTS:
        return {"ok": False, "reason": "invalid_code"}
    given = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(given) != 6 or not hmac.compare_digest(_mac(given), str(stored.get("code_hash") or "")):
        stored["attempts"] = attempts + 1
        _write(channel, destination, stored)
        return {"ok": False, "reason": "invalid_code"}
    from .portal_sessions import mint as mint_portal_session
    from .portal_sessions import mint_contact

    accounts = contact_accounts(channel, destination)
    if len(accounts) > 1:
        # Verified the contact, not a row: they pick which account to open. No session yet.
        _write(channel, destination, {**stored, "exp": 0, "attempts": MAX_ATTEMPTS})
        return {
            "ok": True,
            "multi": True,
            "contact_token": mint_contact(channel=channel, destination=destination),
            "accounts": account_choices(accounts),
        }

    # Code-confirmed logins get a revocable 90-day rolling portal session (name/DOB-only checks stay 30 min).
    token = mint_portal_session(
        client_id=str(stored.get("client_id") or ""),
        email=str(stored.get("email") or ""),
        name=str(stored.get("name") or ""),
        matched=channel,
    )
    _write(channel, destination, {**stored, "exp": 0, "attempts": MAX_ATTEMPTS})
    return {"ok": True, "verify_token": token}


def contact_accounts(channel: str, destination: str) -> list[dict[str, Any]]:
    """Every active client on a verified email or phone, oldest first. Call only after a code matched."""
    rows = list_by_email(destination) if channel == "email" else list_by_phone(destination)
    return [r for r in rows if (r.get("status") or "active").lower() != "archived"]


def account_label(row: dict[str, Any]) -> str:
    """First name plus last initial, e.g. "Jim J."."""
    full = str(row.get("display_name") or "").strip()
    if "," in full:
        last, _, first = full.partition(",")
    else:
        bits = full.split()
        first, last = (" ".join(bits[:-1]), bits[-1]) if len(bits) > 1 else (full, "")
    first = (str(row.get("preferred_name") or "").strip() or first.strip()).split(" ")[0]
    last = last.strip()
    return f"{first} {last[0].upper()}." if first and last else (first or "Account")


def account_choices(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"ref": i + 1, "label": account_label(r)} for i, r in enumerate(rows)]


def list_contact_accounts(contact_token: str) -> dict[str, Any]:
    from .portal_sessions import check_contact

    try:
        channel, destination = check_contact(contact_token)
    except ValueError:
        return {"ok": False, "reason": "expired"}
    return {"ok": True, "accounts": account_choices(contact_accounts(channel, destination))}


def pick_account(contact_token: str, ref: int, old_token: str = "") -> dict[str, Any]:
    """Session scoped to one client on the verified contact. Replaces (revokes) the browser's old session."""
    from .portal_sessions import check_contact, is_session_token, mint, revoke

    try:
        channel, destination = check_contact(contact_token)
    except ValueError:
        return {"ok": False, "reason": "expired"}
    rows = contact_accounts(channel, destination)
    if not 1 <= int(ref or 0) <= len(rows):
        return {"ok": False, "reason": "bad_choice", "accounts": account_choices(rows)}
    row = rows[int(ref) - 1]
    if is_session_token(old_token or ""):
        revoke(old_token.strip())
    name = str(row.get("preferred_name") or row.get("display_name") or "")[:80]
    token = mint(client_id=str(row.get("id") or ""), email=str(row.get("email") or ""), name=name, matched=channel)
    return {"ok": True, "verify_token": token, "name": name, "label": account_label(row),
            "multi": len(rows) > 1}
