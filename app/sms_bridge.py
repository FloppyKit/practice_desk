"""Queue SMS for the desk Mac to send through Spruce.app.

Twilio stays off. Spruce's developer API is not used. The booker only
writes a job. A worker on the awake Mac claims it and clicks send.

Visit reminders expire at the visit start so a late wake does not send
"in 30 min" after the visit. Chat and voice logistics texts hold 72 hours
so an overnight or weekend request still goes the next morning.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

DIRS = ("pending", "leased", "sent", "failed", "uncertain", "expired")
ID_RE = re.compile(r"^[a-f0-9]{20}$")
AGENT_TEMPLATES = ("book_link", "join")
HOLD = {
    "remind_15m": timedelta(hours=2),
    "remind_30m": timedelta(hours=2),
    "remind_24h": timedelta(hours=26),
    "confirm": timedelta(days=7),
    "book_link": timedelta(hours=72),
    "join": timedelta(hours=72),
    "file": timedelta(hours=72),
    "late": timedelta(hours=6),
}
LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3


def bridge_enabled() -> bool:
    return os.environ.get("SMS_BRIDGE", "").strip().lower() in ("spruce_mac", "spruce", "mac")


def bridge_secret() -> str:
    return os.environ.get("SMS_BRIDGE_SECRET", "").strip()


def secret_ok(presented: str) -> bool:
    import hmac

    expected = bridge_secret()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented, expected)


def sms_settled(result: dict[str, Any]) -> bool:
    """True when the reminder cron should stop retrying this SMS."""
    if result.get("settled") or result.get("sent"):
        return True
    if result.get("queued") or result.get("skipped"):
        return False
    return bool(result.get("ok"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bridge_root() -> Path:
    root = Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs")) / "sms-bridge"
    for name in DIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    fh = (root / ".lock").open("a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _job_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def _safe_id(job_id: str) -> str:
    if not ID_RE.match(job_id or ""):
        raise ValueError("bad id")
    return job_id


def _path_for(root: Path, job_id: str) -> Path | None:
    name = f"{_safe_id(job_id)}.json"
    for folder in DIRS:
        path = root / folder / name
        if path.exists():
            return path
    return None


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(row, default=str), encoding="utf-8")
    tmp.replace(path)


def _move(path: Path, folder: str, row: dict[str, Any]) -> Path:
    dest = path.parent.parent / folder / path.name
    row["status"] = folder
    _write(dest, row)
    if path.exists() and path.resolve() != dest.resolve():
        path.unlink()
    return dest


def _default_key(to: str, body: str, source: str) -> str:
    day = _now().strftime("%Y-%m-%d")
    digest = hashlib.sha256(body.encode()).hexdigest()[:12]
    return f"{source}:{to}:{day}:{digest}"


def enqueue(
    *,
    to: str,
    body: str,
    kind: str = "file",
    source: str = "booker",
    idempotency_key: str = "",
    not_after: str = "",
    template: str = "",
) -> dict[str, Any]:
    phone = (to or "").strip()
    text = (body or "").strip()
    if not phone or not text:
        return {"ok": False, "channel": "sms_bridge", "detail": "missing phone or body"}
    if len(text) > 480:
        text = text[:480]
    root = bridge_root()
    key = (idempotency_key or "").strip() or _default_key(phone, text, source)
    job_id = _job_id(key)
    hold = HOLD.get(kind, timedelta(hours=72))
    expires = _parse(not_after) or (_now() + hold)
    with _lock(root):
        existing = _path_for(root, job_id)
        if existing:
            row = _read(existing)
            status = existing.parent.name
            base = {
                "ok": True,
                "channel": "sms_bridge",
                "id": job_id,
                "already": True,
                "status": status,
            }
            if status == "sent":
                return {**base, "sent": True, "settled": True}
            if status in ("uncertain", "expired", "failed"):
                return {
                    **base,
                    "ok": True,
                    "skipped": True,
                    "settled": True,
                    "detail": f"not retrying ({status})",
                }
            return {**base, "queued": True, "detail": "already queued"}
        row = {
            "id": job_id,
            "idempotency_key": key,
            "to": phone,
            "body": text,
            "kind": kind,
            "source": source,
            "template": template,
            "created_at": _iso(_now()),
            "not_before": _iso(_now()),
            "not_after": _iso(expires),
            "attempts": 0,
            "status": "pending",
        }
        _write(root / "pending" / f"{job_id}.json", row)
    return {
        "ok": True,
        "channel": "sms_bridge",
        "id": job_id,
        "queued": True,
        "detail": "queued for Spruce on the desk Mac",
    }


def due_summary() -> dict[str, int]:
    root = bridge_root()
    now = _now()
    due = 0
    pending = 0
    with _lock(root):
        for path in (root / "pending").glob("*.json"):
            pending += 1
            row = _read(path)
            start = _parse(row.get("not_before"))
            end = _parse(row.get("not_after"))
            if start and start > now:
                continue
            if end and end <= now:
                continue
            due += 1
    return {"pending": pending, "due": due}


def claim() -> dict[str, Any] | None:
    """Lease the oldest due job. None if the queue has nothing to send."""
    root = bridge_root()
    now = _now()
    with _lock(root):
        rows: list[tuple[str, Path, dict[str, Any]]] = []
        for path in (root / "pending").glob("*.json"):
            row = _read(path)
            rows.append((row.get("created_at") or "", path, row))
        rows.sort(key=lambda item: item[0])
        for _created, path, row in rows:
            end = _parse(row.get("not_after"))
            if end and end <= now:
                _move(path, "expired", row)
                continue
            start = _parse(row.get("not_before"))
            if start and start > now:
                continue
            row["leased_at"] = _iso(now)
            row["lease_until"] = _iso(now + LEASE)
            row["attempts"] = int(row.get("attempts") or 0) + 1
            _move(path, "leased", row)
            return {
                "id": row["id"],
                "to": row["to"],
                "body": row["body"],
                "kind": row.get("kind") or "",
                "source": row.get("source") or "",
            }
    return None


def ack(job_id: str, status: str) -> dict[str, Any]:
    root = bridge_root()
    job_id = _safe_id(job_id)
    status = (status or "").strip().lower()
    if status not in ("sent", "failed", "uncertain", "release"):
        return {"ok": False, "detail": "bad status"}
    with _lock(root):
        path = _path_for(root, job_id)
        if not path:
            return {"ok": False, "detail": "missing"}
        row = _read(path)
        if status == "sent":
            row["sent_at"] = _iso(_now())
            _move(path, "sent", row)
            return {"ok": True, "status": "sent", "settled": True}
        if status == "uncertain":
            row["uncertain_at"] = _iso(_now())
            _move(path, "uncertain", row)
            return {"ok": True, "status": "uncertain", "settled": True}
        if status == "release":
            row["not_before"] = _iso(_now())
            _move(path, "pending", row)
            return {"ok": True, "status": "pending", "queued": True}
        attempts = int(row.get("attempts") or 0)
        if attempts < MAX_ATTEMPTS:
            row["not_before"] = _iso(_now() + timedelta(minutes=5))
            row["last_error"] = "send failed"
            _move(path, "pending", row)
            return {"ok": True, "status": "pending", "queued": True, "attempts": attempts}
        row["failed_at"] = _iso(_now())
        _move(path, "failed", row)
        return {"ok": True, "status": "failed", "settled": True, "attempts": attempts}


def render_agent_template(
    template: str,
    *,
    title: str = "",
    when_label: str = "",
    duration_minutes: int | None = None,
) -> str:
    from .email_templates import book_link_sms, join_sms

    if template == "book_link":
        return book_link_sms()
    if template == "join":
        return join_sms()
    raise ValueError("template not allowed from an agent")
