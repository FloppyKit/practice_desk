"""Website chat threads. Patient writes here. A Spruce note is the inbox.

When SPRUCE_INBOX_API is on, the message becomes an internal Spruce note.
A later staff note in that conversation is the reply, pulled on the next
thread read (and by the webhook when one is registered). SMS only if they
asked for a text and consent is on file. Email to SPRUCE_INBOX is the
fallback when the API flag is off or the push fails.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,48}$")
HEAR = {"chat", "sms", "email"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dir() -> Path:
    root = Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs")) / "chat-threads"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path(token: str) -> Path:
    if not TOKEN_RE.match(token or ""):
        raise ValueError("bad token")
    return _dir() / f"{token}.json"


def _read(token: str) -> dict[str, Any] | None:
    path = _path(token)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write(row: dict[str, Any]) -> None:
    path = _path(row["token"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row), encoding="utf-8")
    tmp.replace(path)


def public_thread(row: dict[str, Any]) -> dict[str, Any]:
    site = os.environ.get("SITE_PUBLIC_URL", "https://site.psycharts.org").rstrip("/")
    token = row["token"]
    return {
        "ok": True,
        "token": token,
        "patient_url": f"{site}/portal?t={token}",
        "hear_back": row.get("hear_back") or "chat",
        "messages": [
            {"role": m.get("role"), "body": m.get("body"), "at": m.get("at")}
            for m in row.get("messages") or []
        ],
    }


def _notify_email(to: str, subject: str, body: str) -> dict[str, Any]:
    url = os.environ.get("NOTIFY_WEBHOOK_URL", "http://127.0.0.1:8791/notify")
    token = os.environ.get("NOTIFY_WEBHOOK_TOKEN", "")
    if not token:
        return {"ok": False, "detail": "notify token unset"}
    req = urllib.request.Request(
        url,
        data=json.dumps(
            {"kind": "email", "to": to, "subject": subject, "body": body}
        ).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Notify-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            data = json.loads(raw) if raw else {"ok": True}
            return {"ok": bool(data.get("ok", True)), "detail": data.get("detail") or "sent"}
    except Exception as e:
        return {"ok": False, "detail": type(e).__name__}


def _spruce_inbox() -> str:
    return (os.environ.get("SPRUCE_INBOX") or "").strip()


def _index_path() -> Path:
    return _dir() / "_spruce_index.json"


def _read_index() -> dict[str, str]:
    path = _index_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _remember_conversation(conversation_id: str, token: str) -> None:
    if not conversation_id or not token:
        return
    idx = _read_index()
    idx[conversation_id] = token
    path = _index_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx), encoding="utf-8")
    tmp.replace(path)


def _lookup_conversation(conversation_id: str) -> str:
    if not conversation_id:
        return ""
    token = _read_index().get(conversation_id) or ""
    if token:
        try:
            if _path(token).exists():
                return token
        except ValueError:
            return ""
    for path in _dir().glob("*.json"):
        if path.name.startswith("_"):
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("spruce_conversation_id") == conversation_id:
            return str(row.get("token") or "")
    return ""


def _compose_note(row: dict[str, Any], text: str) -> str:
    site = os.environ.get("SITE_PUBLIC_URL", "https://site.psycharts.org").rstrip("/")
    token = row["token"]
    name = (row.get("name") or "").strip()
    lines = [
        "Website chat",
        f"Name: {name or '(not given)'}",
        f"Thread: {token}",
        f"Return: {site}/portal?t={token}",
        f"Hear back: {row.get('hear_back') or 'chat'}",
        f"SMS consent: {'yes' if row.get('sms_consent') else 'no'}",
    ]
    phone = (row.get("phone") or "").strip()
    email = (row.get("email") or "").strip()
    if phone:
        lines.append(f"Phone: {phone}")
    if email:
        lines.append(f"Email: {email}")
    lines.extend(["", "They wrote:", text])
    return "\n".join(lines)[:2000]


def _push_spruce(
    row: dict[str, Any],
    text: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> bool:
    """Create or continue the Spruce note. False means use the email fallback."""
    from .spruce_api import (
        create_note_conversation,
        inbox_api_enabled,
        post_internal_note,
    )

    if not inbox_api_enabled():
        return False
    composed = _compose_note(row, text)
    token = row["token"]
    n = len(row.get("messages") or [])
    conversation_id = str(row.get("spruce_conversation_id") or "")
    if conversation_id:
        result = post_internal_note(
            conversation_id,
            composed,
            idempotency_key=f"chat:{token}:{n}",
            opener=opener,
        )
    else:
        who = (row.get("name") or "").strip()
        title = f"Website chat — {who}" if who else f"Website chat {token}"
        result = create_note_conversation(
            title[:120],
            composed,
            idempotency_key=f"chat-open:{token}",
            opener=opener,
        )
        conversation_id = str(result.get("conversation_id") or "")
        if conversation_id:
            row["spruce_conversation_id"] = conversation_id
            if result.get("app_url"):
                row["spruce_app_url"] = str(result["app_url"])[:300]
            _remember_conversation(conversation_id, token)
    if not result.get("ok"):
        row["spruce_error"] = str(result.get("detail") or "push failed")[:80]
        _write(row)
        return False
    request_id = str(result.get("request_id") or "")
    if request_id:
        ids = list(row.get("spruce_request_ids") or [])
        if request_id not in ids:
            ids.append(request_id)
        row["spruce_request_ids"] = ids[-100:]
    posted = list(row.get("spruce_posted") or [])
    posted.append(composed)
    row["spruce_posted"] = posted[-50:]
    row.pop("spruce_error", None)
    _write(row)
    return bool(row.get("spruce_conversation_id"))


def post_message(
    *,
    body: str,
    token: str = "",
    label: str = "",
    email: str = "",
    phone: str = "",
    hear_back: str = "",
    sms_consent: bool = False,
    name: str = "",
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    text = (body or "").strip()
    if not text:
        raise ValueError("empty message")
    if len(text) > 2000:
        text = text[:2000]
    row = _read(token) if token else None
    if row is None:
        token = secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:20]
        row = {
            "token": token,
            "created_at": _now(),
            "label": (label or "Website chat")[:80],
            "name": (name or "")[:80],
            "email": (email or "")[:120],
            "phone": (phone or "")[:20],
            "hear_back": hear_back if hear_back in HEAR else "chat",
            "sms_consent": bool(sms_consent),
            "messages": [],
            "seen_reply_ids": [],
        }
    else:
        if name:
            row["name"] = name[:80]
        if email:
            row["email"] = email[:120]
        if phone:
            row["phone"] = phone[:20]
        if hear_back in HEAR:
            row["hear_back"] = hear_back
        if sms_consent:
            row["sms_consent"] = True
    row.setdefault("messages", []).append(
        {"role": "patient", "body": text, "at": _now()}
    )
    _write(row)
    if not _push_spruce(row, text, opener=opener):
        _email_spruce(row, text)
    return public_thread(row)


def _email_spruce(row: dict[str, Any], text: str) -> None:
    inbox = _spruce_inbox()
    if not inbox:
        return
    token = row["token"]
    subject = f"[chat {token}] website chat"
    hear = row.get("hear_back") or "chat"
    lines = [
        "Website chat request. Reply to this email to answer in their chat.",
        "",
        f"Thread: {token}",
        f"Hear back: {hear}",
        f"SMS consent: {'yes' if row.get('sms_consent') else 'no'}",
        f"Phone: {row.get('phone') or '(none)'}",
        f"Email: {row.get('email') or '(none)'}",
        "",
        "They wrote:",
        text,
    ]
    _notify_email(inbox, subject, "\n".join(lines))


def get_thread(token: str, *, opener: Callable[..., Any] | None = None) -> dict[str, Any] | None:
    row = _read(token)
    if not row:
        return None
    if row.get("spruce_conversation_id"):
        _pull_spruce_items(row, opener=opener)
    else:
        _pull_replies(row)
    return public_thread(row)


def _item_text(item: dict[str, Any]) -> str:
    text = item.get("text")
    if text:
        return str(text).strip()
    message = item.get("message")
    if isinstance(message, dict) and message.get("text"):
        return str(message["text"]).strip()
    return ""


def _item_request_id(item: dict[str, Any]) -> str:
    return str(item.get("requestId") or item.get("requestID") or "").strip()


def _is_echo(row: dict[str, Any], item: dict[str, Any], text: str) -> bool:
    request_id = _item_request_id(item)
    if request_id and request_id in set(row.get("spruce_request_ids") or []):
        return True
    return bool(text) and text in set(row.get("spruce_posted") or [])


def apply_spruce_item(row: dict[str, Any], item: dict[str, Any]) -> str:
    """Append a staff reply. Our own notes are echoes and are not shown."""
    item_id = str(item.get("id") or "").strip()
    text = _item_text(item)[:2000]
    if not item_id or not text:
        return "skip"
    seen = set(row.get("seen_reply_ids") or [])
    if item_id in seen:
        return "seen"
    seen.add(item_id)
    row["seen_reply_ids"] = list(seen)[-200:]
    if _is_echo(row, item, text):
        _write(row)
        return "echo"
    row.setdefault("messages", []).append(
        {"role": "staff", "body": text, "at": item.get("createdAt") or _now(), "id": item_id}
    )
    _write(row)
    _maybe_sms(row, text)
    return "reply"


def ingest_spruce_item(item: dict[str, Any]) -> str:
    conversation = item.get("conversation") if isinstance(item.get("conversation"), dict) else {}
    conversation_id = str(item.get("conversationId") or conversation.get("id") or "")
    token = _lookup_conversation(conversation_id)
    if not token:
        return "unlinked"
    row = _read(token)
    if not row:
        return "unlinked"
    return apply_spruce_item(row, item)


def handle_spruce_webhook(raw: bytes, signature_b64: str) -> dict[str, Any]:
    """Verify Spruce's signature, then copy a staff note into the website thread."""
    from .spruce_api import verify_webhook_signature

    secret = os.environ.get("SPRUCE_WEBHOOK_SECRET", "").strip()
    if not secret or not verify_webhook_signature(raw, signature_b64, secret):
        return {"unauthorized": True, "accepted": 0}
    try:
        event = json.loads(raw.decode() or "{}")
    except json.JSONDecodeError:
        return {"ok": True, "accepted": 0, "ignored": "bad json"}
    if not isinstance(event, dict) or event.get("type") != "conversationItem.created":
        return {"ok": True, "accepted": 0, "ignored": str(event.get("type") if isinstance(event, dict) else "")[:80]}
    obj = (event.get("data") or {}).get("object") if isinstance(event.get("data"), dict) else None
    if not isinstance(obj, dict):
        return {"ok": True, "accepted": 0}
    result = ingest_spruce_item(obj)
    return {"ok": True, "accepted": 1 if result == "reply" else 0, "result": result}


def _pull_spruce_items(
    row: dict[str, Any],
    *,
    opener: Callable[..., Any] | None = None,
) -> None:
    from .spruce_api import inbox_api_enabled, list_conversation_items

    if not inbox_api_enabled():
        return
    conversation_id = str(row.get("spruce_conversation_id") or "")
    if not conversation_id:
        return
    status, items = list_conversation_items(conversation_id, opener=opener)
    if status < 200 or status >= 300:
        return
    # newest_first: apply oldest of this page last-to-first so the thread stays in order
    for item in reversed(items):
        apply_spruce_item(row, item)


def _pull_replies(row: dict[str, Any]) -> None:
    url = os.environ.get("NOTIFY_WEBHOOK_URL", "http://127.0.0.1:8791/notify")
    base = url[:-7] if url.endswith("/notify") else url.rstrip("/")
    token = os.environ.get("NOTIFY_WEBHOOK_TOKEN", "")
    if not token:
        return
    req = urllib.request.Request(
        base + "/mail/chat-replies",
        data=json.dumps({"token": row["token"]}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Notify-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode() or "{}")
    except Exception:
        return
    seen = set(row.get("seen_reply_ids") or [])
    changed = False
    for item in data.get("replies") or []:
        mid = str(item.get("id") or "")
        text = str(item.get("text") or "").strip()
        if not mid or not text or mid in seen:
            continue
        seen.add(mid)
        row.setdefault("messages", []).append(
            {"role": "staff", "body": text[:2000], "at": _now(), "id": mid}
        )
        changed = True
        _maybe_sms(row, text)
    if changed:
        row["seen_reply_ids"] = list(seen)[-200:]
        _write(row)


def _maybe_sms(row: dict[str, Any], text: str) -> None:
    if row.get("hear_back") != "sms" or not row.get("sms_consent"):
        return
    phone = (row.get("phone") or "").strip()
    if not phone:
        return
    from .notify import send_sms

    send_sms(
        phone,
        f"Dr. Matt Brown: {text[:400]}",
        kind="chat_reply",
        source="chat",
        idempotency_key=f"chat:{row['token']}:{len(row.get('messages') or [])}",
        template="chat_reply",
    )
