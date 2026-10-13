"""Send a logistics SMS through the Spruce API.

The bearer token is already base64(access_id:secret). It lives in the
environment or in gitignored secrets/spruce.env. Never log it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

API = "https://api.sprucehealth.com/v1"


def spruce_api_enabled() -> bool:
    return os.environ.get("SMS_BRIDGE", "").strip().lower() == "spruce_api"


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def spruce_config() -> tuple[str, str]:
    token = os.environ.get("SPRUCE_API_TOKEN", "").strip()
    endpoint = os.environ.get("SPRUCE_PHONE_ENDPOINT_ID", "").strip()
    if token and endpoint:
        return token, endpoint
    explicit = os.environ.get("SPRUCE_ENV_FILE", "").strip()
    path = Path(explicit) if explicit else Path(__file__).resolve().parents[1] / "secrets" / "spruce.env"
    vals = _parse_env_file(path)
    return token or vals.get("SPRUCE_API_TOKEN", ""), endpoint or vals.get(
        "SPRUCE_PHONE_ENDPOINT_ID", ""
    )


def spruce_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    token: str = "",
    idempotency_key: str = "",
    opener: Callable[..., Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Authenticated Spruce call. Returns status and JSON. Does not log the token."""
    if not token:
        token, _endpoint = spruce_config()
    if not token:
        return 0, {"message": "spruce credentials missing"}
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = (idempotency_key or "").strip()
    if key:
        headers["s-idempotency-key"] = key[:255]
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        API + path,
        data=data,
        headers=headers,
        method=method,
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(req, timeout=30) as resp:
            raw = resp.read().decode()
            body = json.loads(raw) if raw else {}
            return int(getattr(resp, "status", 200)), body if isinstance(body, dict) else {"data": body}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {"message": "non-json error"}
        if not isinstance(body, dict):
            body = {"message": "non-json error"}
        return e.code, body


def send_spruce_sms(
    to_e164: str,
    body: str,
    *,
    idempotency_key: str = "",
    token: str = "",
    endpoint_id: str = "",
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """POST a text from the practice Spruce phone line. 2xx or duplicate key = sent."""
    if not token or not endpoint_id:
        loaded_token, loaded_endpoint = spruce_config()
        token = token or loaded_token
        endpoint_id = endpoint_id or loaded_endpoint
    if not token or not endpoint_id:
        return {
            "ok": False,
            "channel": "spruce",
            "detail": "spruce credentials missing",
        }
    text = (body or "").strip()
    if not text:
        return {"ok": False, "channel": "spruce", "detail": "empty body"}
    url = (
        f"{API}/internalendpoints/{quote(endpoint_id, safe='')}/conversations"
    )
    payload = {
        "destination": {"smsOrEmailEndpoint": to_e164},
        "message": {"body": [{"type": "text", "value": text[:480]}]},
    }
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    key = (idempotency_key or "").strip()
    if key:
        headers["s-idempotency-key"] = key[:255]
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(req, timeout=30) as resp:
            raw = resp.read().decode()
            data = json.loads(raw) if raw else {}
            request_id = (
                data.get("requestId")
                or data.get("requestID")
                or data.get("id")
                or ""
            )
            return {
                "ok": True,
                "channel": "spruce",
                "sent": True,
                "settled": True,
                "request_id": str(request_id)[:80],
            }
    except urllib.error.HTTPError as e:
        if e.code == 422:
            return {
                "ok": True,
                "channel": "spruce",
                "sent": True,
                "already": True,
                "settled": True,
                "detail": "already accepted",
            }
        return {"ok": False, "channel": "spruce", "detail": f"http {e.code}"}
    except Exception as e:
        return {"ok": False, "channel": "spruce", "detail": type(e).__name__}


def _digits(value: str) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def phone_destination_id(contact: dict[str, Any], e164: str) -> str:
    want = _digits(e164)[-10:]
    for ph in contact.get("phoneNumbers") or []:
        if not isinstance(ph, dict):
            continue
        if _digits(str(ph.get("value") or "")).endswith(want):
            return str(ph.get("id") or "")
    return ""


def create_contact(
    *,
    given_name: str,
    family_name: str,
    phone_e164: str,
    idempotency_key: str = "",
    token: str = "",
    opener: Callable[..., Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    return spruce_request(
        "POST",
        "/contacts",
        {
            "category": "patient",
            "givenName": given_name,
            "familyName": family_name,
            "phoneNumbers": [{"value": phone_e164, "label": "Cell"}],
        },
        token=token,
        idempotency_key=idempotency_key,
        opener=opener,
    )


def search_contacts(
    free_text: str,
    *,
    token: str = "",
    opener: Callable[..., Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    return spruce_request(
        "POST",
        "/contacts/search",
        {"freeText": free_text},
        token=token,
        opener=opener,
    )


def spruce_token() -> str:
    token = os.environ.get("SPRUCE_API_TOKEN", "").strip()
    if token:
        return token
    explicit = os.environ.get("SPRUCE_ENV_FILE", "").strip()
    path = Path(explicit) if explicit else Path(__file__).resolve().parents[1] / "secrets" / "spruce.env"
    return _parse_env_file(path).get("SPRUCE_API_TOKEN", "")


def _env_flag(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    explicit = os.environ.get("SPRUCE_ENV_FILE", "").strip()
    path = Path(explicit) if explicit else Path(__file__).resolve().parents[1] / "secrets" / "spruce.env"
    return _parse_env_file(path).get(name, "").strip()


def inbox_api_enabled() -> bool:
    """Website chat uses the Spruce API only when this flag is on."""
    return _env_flag("SPRUCE_INBOX_API").lower() in ("1", "true", "yes", "on")


def note_member_ids(
    *,
    token: str = "",
    opener: Callable[..., Any] | None = None,
) -> list[str]:
    """Teammates who should see a note. Empty memberIds makes the note private to the API."""
    raw = os.environ.get("SPRUCE_NOTE_MEMBER_IDS", "").strip()
    if raw:
        return [part.strip() for part in raw.split(",") if part.strip()]
    status, body = spruce_request(
        "GET", "/organization/members", token=token, opener=opener
    )
    if status < 200 or status >= 300:
        return []
    ids: list[str] = []
    for member in body.get("members") or []:
        if not isinstance(member, dict):
            continue
        mid = str(member.get("id") or "").strip()
        if mid:
            ids.append(mid)
    return ids


def create_note_conversation(
    title: str,
    text: str,
    *,
    member_ids: list[str] | None = None,
    idempotency_key: str = "",
    token: str = "",
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Internal Spruce note. Associated contacts are not used. Nobody is texted."""
    if not token:
        token = spruce_token()
    if not token:
        return {"ok": False, "detail": "spruce credentials missing"}
    members = note_member_ids(token=token, opener=opener) if member_ids is None else member_ids
    note: dict[str, Any] = {
        "title": (title or "Website chat")[:120],
        "message": {
            "internal": True,
            "body": [{"type": "text", "value": (text or "")[:2000]}],
        },
    }
    if members:
        note["memberIds"] = members[:50]
    status, body = spruce_request(
        "POST",
        "/conversations",
        {"type": "note", "note": note},
        token=token,
        idempotency_key=idempotency_key,
        opener=opener,
    )
    if status < 200 or status >= 300:
        return {"ok": False, "detail": f"http {status}" if status else "spruce credentials missing"}
    conversation = body.get("conversation") if isinstance(body.get("conversation"), dict) else {}
    return {
        "ok": True,
        "conversation_id": str(conversation.get("id") or ""),
        "app_url": str(conversation.get("appURL") or ""),
        "request_id": str(body.get("postMessageRequestId") or body.get("requestId") or ""),
    }


def post_internal_note(
    conversation_id: str,
    text: str,
    *,
    idempotency_key: str = "",
    token: str = "",
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Post into an existing note conversation. internal=true is required for notes."""
    if not token:
        token = spruce_token()
    if not token or not conversation_id:
        return {"ok": False, "detail": "spruce credentials missing"}
    status, body = spruce_request(
        "POST",
        f"/conversations/{quote(conversation_id, safe='')}/messages",
        {
            "internal": True,
            "body": [{"type": "text", "value": (text or "")[:2000]}],
        },
        token=token,
        idempotency_key=idempotency_key,
        opener=opener,
    )
    if status < 200 or status >= 300:
        return {"ok": False, "detail": f"http {status}" if status else "spruce credentials missing"}
    return {
        "ok": True,
        "request_id": str(body.get("requestId") or body.get("requestID") or body.get("id") or ""),
    }


def list_conversation_items(
    conversation_id: str,
    *,
    token: str = "",
    opener: Callable[..., Any] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    if not token:
        token = spruce_token()
    status, body = spruce_request(
        "GET",
        f"/conversations/{quote(conversation_id, safe='')}/items?pageSize=50&order=newest_first",
        token=token,
        opener=opener,
    )
    items = body.get("conversationItems") or body.get("items") or []
    if not isinstance(items, list):
        items = []
    return status, [item for item in items if isinstance(item, dict)]


def verify_webhook_signature(body: bytes, signature_b64: str, secret: str) -> bool:
    """HMAC-SHA256 of the raw body. Header is base64, matching Spruce's Go sample."""
    import base64
    import hashlib
    import hmac

    if not body or not signature_b64 or not secret:
        return False
    try:
        given = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(mac, given)


def invite_contact(
    contact_id: str,
    *,
    destination_id: str,
    internal_endpoint_id: str,
    custom_message: str,
    token: str = "",
    opener: Callable[..., Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    return spruce_request(
        "POST",
        f"/contacts/{quote(contact_id, safe='')}/invite",
        {
            "destinationId": destination_id,
            "internalEndpointId": internal_endpoint_id,
            "customMessage": custom_message[:500],
        },
        token=token,
        opener=opener,
    )
