"""Staff click-to-call.

Two handsets, one caller ID (the practice voice DID):
- browser: this computer’s mic via Twilio Voice SDK
- phone: Twilio rings the clinician cell, then the patient

The personal handset number is never sent to the patient and is not logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .notify import normalize_e164
from .patients import client_by_id, search_clients

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_VOICE_FROM = (
    os.environ.get("TWILIO_PHONE_NUMBER")
    or os.environ.get("TWILIO_VOICE_FROM")
    or ""
).strip()

TWILIO_API_KEY_SID = os.environ.get("TWILIO_API_KEY_SID", "").strip()
TWILIO_API_KEY_SECRET = os.environ.get("TWILIO_API_KEY_SECRET", "").strip()
TWILIO_DESK_TWIML_APP_SID = os.environ.get("TWILIO_DESK_TWIML_APP_SID", "").strip()

LOG_DIR = Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs"))
_US = re.compile(r"^\+1(\d{10})$")
_PENDING: dict[str, dict[str, Any]] = {}
_PENDING_TTL = 180


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def mask_e164(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 4:
        return "••••"
    return "•••-" + digits[-4:]


def pretty_e164(raw: str) -> str:
    e164 = normalize_e164(raw) or ""
    m = _US.match(e164)
    if m:
        d = m.group(1)
        return f"({d[0:3]}) {d[3:6]}-{d[6:10]}"
    return e164 or (raw or "").strip()


def voice_from() -> str:
    env = normalize_e164(TWILIO_VOICE_FROM) or ""
    if env:
        return env
    try:
        from .practice import load as load_practice

        return normalize_e164(str((load_practice() or {}).get("phone") or "")) or ""
    except Exception:
        return ""


def clinician_callback() -> str:
    from .practice import load as load_practice

    return normalize_e164(str((load_practice() or {}).get("clinician_callback") or "")) or ""


def twilio_ready() -> bool:
    return bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and voice_from())


def browser_ready() -> bool:
    return bool(
        twilio_ready()
        and TWILIO_API_KEY_SID
        and TWILIO_API_KEY_SECRET
        and TWILIO_DESK_TWIML_APP_SID
    )


def preferred_handset() -> str:
    try:
        from .practice import load as load_practice

        raw = str((load_practice() or {}).get("call_handset") or "").strip().lower()
    except Exception:
        raw = ""
    if raw == "phone":
        return "phone"
    return "browser"


def config() -> dict[str, Any]:
    did = voice_from()
    cb = clinician_callback()
    handset = preferred_handset()
    br = browser_ready()
    ready = twilio_ready() and (br if handset == "browser" else bool(cb))
    detail = ""
    if not twilio_ready():
        detail = "Twilio voice is not configured on the booker."
    elif handset == "browser" and not br:
        detail = "Desktop calling is not wired yet (Twilio Voice app)."
    elif handset == "phone" and not cb:
        detail = "Set your handset number in Practice → Office line, or switch to This computer."
    return {
        "ok": True,
        "ready": ready,
        "has_twilio": twilio_ready(),
        "has_callback": bool(cb),
        "browser_ready": br,
        "handset": handset,
        "did": did,
        "did_pretty": pretty_e164(did) if did else "",
        "callback_masked": mask_e164(cb) if cb else "",
        "detail": detail,
    }


def _append_log(row: dict[str, Any]) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / "desk-calls.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def _twilio(method: str, path: str, form: dict[str, str] | None = None) -> dict[str, Any]:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN):
        raise RuntimeError("Twilio credentials unset")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}{path}"
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Basic " + auth,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"twilio {e.code}: {body}") from e


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def voice_access_token(*, identity: str = "desk", ttl: int = 300) -> str:
    if not (TWILIO_API_KEY_SID and TWILIO_API_KEY_SECRET and TWILIO_ACCOUNT_SID):
        raise RuntimeError("Twilio API key unset")
    now = int(time.time())
    header = {"typ": "JWT", "alg": "HS256", "cty": "twilio-fpa;v=1"}
    payload = {
        "jti": TWILIO_API_KEY_SID + "-" + str(uuid.uuid4()),
        "iss": TWILIO_API_KEY_SID,
        "sub": TWILIO_ACCOUNT_SID,
        "nbf": now,
        "exp": now + max(60, min(int(ttl), 600)),
        "grants": {
            "identity": identity,
            "voice": {
                "outgoing": {"application_sid": TWILIO_DESK_TWIML_APP_SID},
                "incoming": {"allow": False},
            },
        },
    }
    signing = (
        _b64url(json.dumps(header, separators=(",", ":"), sort_keys=True).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(TWILIO_API_KEY_SECRET.encode(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + _b64url(sig)


def _prune_pending() -> None:
    now = time.time()
    dead = [k for k, v in _PENDING.items() if float(v.get("exp") or 0) < now]
    for k in dead:
        _PENDING.pop(k, None)


def remember_browser_call(*, to: str, from_did: str, name: str = "", client_id: str = "") -> str:
    _prune_pending()
    nonce = secrets.token_urlsafe(18)
    _PENDING[nonce] = {
        "to": to,
        "from": from_did,
        "name": name,
        "client_id": client_id,
        "exp": time.time() + _PENDING_TTL,
    }
    return nonce


def take_pending(nonce: str) -> dict[str, Any] | None:
    _prune_pending()
    rec = _PENDING.pop((nonce or "").strip(), None)
    return rec


def valid_twilio_signature(url: str, params: dict[str, str], signature: str) -> bool:
    if not TWILIO_AUTH_TOKEN or not signature:
        return False
    bits = [url]
    for key in sorted(params):
        bits.append(key + str(params[key] or ""))
    digest = hmac.new(TWILIO_AUTH_TOKEN.encode(), "".join(bits).encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def browser_twiml(to_e164: str, from_did: str) -> str:
    dest = html.escape(to_e164, quote=True)
    cid = html.escape(from_did, quote=True)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Dial callerId="{cid}" timeout="30" answerOnBridge="true">'
        f"<Number>{dest}</Number>"
        "</Dial>"
        "</Response>"
    )


def reject_twiml(message: str = "Call cannot be completed.") -> str:
    msg = html.escape(message[:80])
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Say>{msg}</Say><Hangup/></Response>"
    )


def connect_twiml(to_e164: str, from_did: str) -> str:
    dest = html.escape(to_e164, quote=True)
    cid = html.escape(from_did, quote=True)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say voice=\"Polly.Joanna\">Connecting your practice call.</Say>"
        f'<Dial callerId="{cid}" timeout="30" hangupOnStar="true">'
        f"<Number>{dest}</Number>"
        "</Dial>"
        "</Response>"
    )


def _public_call(data: dict[str, Any], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    sid = str(data.get("sid") or "")
    status = str(data.get("status") or "")
    out = {
        "ok": True,
        "sid": sid,
        "status": status,
        "duration": data.get("duration") or "",
    }
    if extra:
        out.update(extra)
    return out


def preview_call(
    *,
    client_id: str = "",
    name: str = "",
    e164: str = "",
) -> dict[str, Any]:
    """Resolve who to call. Does not place the call."""
    cfg = config()
    to = normalize_e164(e164) or ""
    label = (name or "").strip()
    cid = (client_id or "").strip()

    if cid:
        row = client_by_id(cid)
        if not row:
            return {"ok": False, "error": "client not found"}
        label = label or str(row.get("name") or "")
        to = to or normalize_e164(str(row.get("phone") or "")) or ""
    elif not to and label:
        hits = search_clients(label, limit=5)
        with_phone = [h for h in hits if normalize_e164(str(h.get("phone") or ""))]
        if not with_phone:
            return {"ok": False, "error": "no matching client with a phone"}
        if len(with_phone) > 1:
            return {
                "ok": False,
                "error": "ambiguous match — pick one",
                "matches": [
                    {
                        "id": h.get("id"),
                        "name": h.get("name"),
                        "phone_masked": mask_e164(str(h.get("phone") or "")),
                    }
                    for h in with_phone[:5]
                ],
            }
        hit = with_phone[0]
        cid = str(hit.get("id") or "")
        label = str(hit.get("name") or label)
        to = normalize_e164(str(hit.get("phone") or "")) or ""

    if not to:
        return {"ok": False, "error": "need a phone number"}
    if not cfg.get("has_twilio"):
        return {"ok": False, "error": cfg.get("detail") or "Twilio unset"}
    handset = str(cfg.get("handset") or "browser")
    if handset == "phone" and not cfg.get("has_callback"):
        return {"ok": False, "error": cfg.get("detail") or "handset number not set"}
    if handset == "browser" and not cfg.get("browser_ready"):
        return {"ok": False, "error": cfg.get("detail") or "desktop calling unset"}
    did = str(cfg.get("did") or "")
    if did and to == did:
        return {"ok": False, "error": "that is the office line"}

    return {
        "ok": True,
        "pending": True,
        "action": "confirm_call",
        "type": "confirm_call",
        "name": label,
        "to": to,
        "to_masked": mask_e164(to),
        "to_pretty": pretty_e164(to),
        "client_id": cid,
        "from_did": did,
        "from_pretty": cfg.get("did_pretty") or pretty_e164(did),
        "handset": handset,
    }


def place_call(
    *,
    to: str,
    client_id: str = "",
    name: str = "",
    handset: str = "",
) -> dict[str, Any]:
    preview = preview_call(client_id=client_id, name=name, e164=to)
    if not preview.get("ok"):
        return preview
    dest = str(preview["to"])
    did = str(preview["from_did"])
    mode = (handset or preview.get("handset") or preferred_handset()).strip().lower()
    if mode not in ("browser", "phone"):
        mode = preferred_handset()

    extra = {
        "to_masked": preview.get("to_masked"),
        "to_pretty": preview.get("to_pretty"),
        "from_pretty": preview.get("from_pretty"),
        "name": preview.get("name") or "",
        "client_id": preview.get("client_id") or "",
        "handset": mode,
        "mode": mode,
    }

    if mode == "browser":
        if not browser_ready():
            return {"ok": False, "error": "desktop calling unset"}
        nonce = remember_browser_call(
            to=dest,
            from_did=did,
            name=str(preview.get("name") or ""),
            client_id=str(preview.get("client_id") or ""),
        )
        token = voice_access_token()
        _append_log(
            {
                "ts": _now(),
                "sid": "",
                "status": "browser",
                "client_id": preview.get("client_id") or "",
                "to_masked": preview.get("to_masked"),
                "from_did": did,
            }
        )
        return {
            "ok": True,
            "sid": "",
            "status": "connecting",
            "token": token,
            "nonce": nonce,
            "identity": "desk",
            **extra,
        }

    cell = clinician_callback()
    if not cell:
        return {"ok": False, "error": "handset number not set"}

    twiml = connect_twiml(dest, did)
    data = _twilio(
        "POST",
        "/Calls.json",
        {
            "To": cell,
            "From": did,
            "Twiml": twiml,
        },
    )
    _append_log(
        {
            "ts": _now(),
            "sid": data.get("sid"),
            "status": data.get("status"),
            "client_id": preview.get("client_id") or "",
            "to_masked": preview.get("to_masked"),
            "from_did": did,
        }
    )
    return _public_call(data, extra=extra)


def call_status(sid: str) -> dict[str, Any]:
    sid = (sid or "").strip()
    if not sid.startswith("CA") or len(sid) < 20:
        return {"ok": False, "error": "bad call id"}
    data = _twilio("GET", f"/Calls/{sid}.json")
    return _public_call(data)


def hangup_call(sid: str) -> dict[str, Any]:
    sid = (sid or "").strip()
    if not sid.startswith("CA") or len(sid) < 20:
        return {"ok": False, "error": "bad call id"}
    data = _twilio(
        "POST",
        f"/Calls/{sid}.json",
        {"Status": "completed"},
    )
    _append_log({"ts": _now(), "sid": sid, "status": "hangup"})
    return _public_call(data)
