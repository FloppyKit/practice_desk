from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 180  # 180 days


def _secret() -> bytes:
    path = os.environ.get("CANCEL_SECRET_FILE", "/secrets/booker-cancel.secret")
    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        raise RuntimeError("empty cancel secret")
    return raw.encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def mint_cancel_token(
    *,
    event_id: str,
    calendar_id: str,
    email: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    title: str = "",
    when_label: str = "",
    name: str = "",
    event_type: str = "",
    start_iso: str = "",
    duration_minutes: int | None = None,
    purpose: str = "",
) -> str:
    payload = {
        "e": event_id,
        "c": calendar_id,
        "x": int(time.time()) + int(ttl_seconds),
    }
    # Residual (P1): email stays in the token so the cancel/reschedule email
    # path keeps working. Token is a capability link, not calendar detail.
    if email:
        payload["m"] = email
    if purpose:
        payload["p"] = str(purpose)[:16]
    # Optional display fields for cancel / reschedule / one-event ICS
    if title:
        payload["t"] = title[:120]
    if when_label:
        payload["w"] = when_label[:80]
    if name:
        payload["n"] = name[:80]
    if event_type:
        payload["et"] = str(event_type)[:40]
    if start_iso:
        payload["s"] = str(start_iso)[:48]
    if duration_minutes:
        payload["d"] = int(duration_minutes)
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_cancel_token(token: str) -> dict[str, Any]:
    try:
        body, sig = token.split(".", 1)
    except ValueError as e:
        raise ValueError("malformed token") from e
    expect = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        raise ValueError("invalid signature")
    payload = json.loads(_b64d(body))
    if int(payload.get("x", 0)) < int(time.time()):
        raise ValueError("token expired")
    if not payload.get("e") or not payload.get("c"):
        raise ValueError("incomplete token")
    return payload


VERIFY_TTL_SECONDS = 60 * 30  # 30 minutes to finish booking


def mint_verify_token(
    *,
    client_id: str,
    email: str = "",
    name: str = "",
    matched: str = "",
    ttl_seconds: int = VERIFY_TTL_SECONDS,
) -> str:
    payload = {
        "k": "pv",
        "id": str(client_id),
        "m": (email or "")[:120],
        "n": (name or "")[:80],
        "h": (matched or "")[:20],
        "x": int(time.time()) + int(ttl_seconds),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_patient_token(token: str) -> dict[str, Any]:
    try:
        body, sig = token.split(".", 1)
    except ValueError as e:
        raise ValueError("malformed token") from e
    expect = _b64(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expect, sig):
        raise ValueError("invalid signature")
    payload = json.loads(_b64d(body))
    if payload.get("k") != "pv":
        raise ValueError("wrong token type")
    if int(payload.get("x", 0)) < int(time.time()):
        raise ValueError("token expired")
    if not payload.get("id"):
        raise ValueError("incomplete token")
    return payload
