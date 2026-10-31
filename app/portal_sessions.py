"""Long-lived website-portal sessions: opaque, revocable, 90-day rolling.

Issued only after an email/SMS code is confirmed (verify_codes.confirm_code). The browser holds
"ps1.<random>"; the server stores only its SHA-256, so a leaked database cannot be replayed.
Each use within the window pushes expiry out to 90 days (written at most once an hour).

  python -m app.portal_sessions revoke-client <client_id>
  python -m app.portal_sessions count
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from . import clients_db

PREFIX = "ps1."
TTL_SECONDS = int(os.environ.get("PORTAL_SESSION_DAYS", "90")) * 86400
TOUCH_EVERY = 3600
DB_PATH = Path(os.environ.get("PORTAL_SESSIONS_DB") or (clients_db.DB_PATH.parent / "portal-sessions.sqlite"))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, name TEXT, "
        "email TEXT, matched TEXT, created INTEGER, last_used INTEGER, expires INTEGER, revoked INTEGER DEFAULT 0)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS sessions_client ON sessions(client_id)")
    return con


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_session_token(token: str) -> bool:
    return (token or "").startswith(PREFIX)


def mint(*, client_id: str, email: str = "", name: str = "", matched: str = "") -> str:
    token = PREFIX + secrets.token_urlsafe(32)
    now = int(time.time())
    with _connect() as con:
        con.execute(
            "INSERT INTO sessions (hash, client_id, name, email, matched, created, last_used, expires) VALUES (?,?,?,?,?,?,?,?)",
            (_hash(token), str(client_id), name[:80], email[:120], matched[:20], now, now, now + TTL_SECONDS),
        )
    return token


def check(token: str) -> dict[str, Any]:
    """Payload shaped like verify_patient_token's. Raises ValueError when unknown, revoked or expired."""
    now = int(time.time())
    with _connect() as con:
        row = con.execute("SELECT * FROM sessions WHERE hash=?", (_hash(token),)).fetchone()
        if not row or row["revoked"]:
            raise ValueError("invalid token")
        if int(row["expires"] or 0) < now:
            raise ValueError("token expired")
        expires = int(row["expires"])
        if now - int(row["last_used"] or 0) >= TOUCH_EVERY:
            expires = now + TTL_SECONDS
            con.execute("UPDATE sessions SET last_used=?, expires=? WHERE hash=?", (now, expires, row["hash"]))
    return {"k": "pv", "id": row["client_id"], "m": row["email"] or "", "n": row["name"] or "",
            "h": row["matched"] or "", "x": expires, "s": "portal"}


def revoke(token: str) -> int:
    with _connect() as con:
        return con.execute("UPDATE sessions SET revoked=1 WHERE hash=?", (_hash(token),)).rowcount


def revoke_client(client_id: str) -> int:
    with _connect() as con:
        return con.execute("UPDATE sessions SET revoked=1 WHERE client_id=? AND revoked=0", (str(client_id),)).rowcount


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "revoke-client" and len(sys.argv) > 2:
        print({"revoked": revoke_client(sys.argv[2])})
    elif cmd == "count":
        with _connect() as c:
            print(dict(c.execute("SELECT count(*) total, sum(revoked=0 AND expires>?) active FROM sessions", (int(time.time()),)).fetchone()))
    else:
        print(__doc__)
