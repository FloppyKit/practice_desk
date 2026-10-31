"""Staff sign-in for the practice desk: emailed 6-digit code -> 90-day rolling session cookie.

Types
  send_code(email, ip) -> None              same outcome whether or not the email is staff
  verify_code(email, code, ip) -> str|None  session token on success
  check(token) -> str|None                  staff email; rolls expiry (writes at most hourly)
  revoke(token) / revoke_all() -> int
  legacy_allowed() -> bool                  old shared secret accepted in the browser until the first code login

Storage: STATE (sqlite next to clients.sqlite). Only SHA-256 of session tokens and HMAC of codes are stored.

  python -m app.desk_auth revoke-all
  python -m app.desk_auth status
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from . import clients_db

COOKIE = "dmb-desk-s"
SESSION_TTL = 90 * 86400
TOUCH_EVERY = 3600
CODE_TTL = 600
CODE_TRIES = 5
SEND_PER_EMAIL = (3, 900)     # 3 codes / 15 min
SEND_PER_IP = (10, 3600)      # 10 codes / hour
VERIFY_PER_IP = (20, 3600)    # 20 code checks / hour
DB_PATH = Path(os.environ.get("DESK_AUTH_DB") or (clients_db.DB_PATH.parent / "desk-auth.sqlite"))


def staff_emails() -> set[str]:
    raw = os.environ.get("DESK_STAFF_EMAILS", "").strip() or "matt@drmattbrown.com"
    return {e.strip().lower() for e in raw.split(",") if "@" in e}


def _con() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE IF NOT EXISTS sessions (hash TEXT PRIMARY KEY, email TEXT, created INTEGER, last_used INTEGER,"
        " expires INTEGER, revoked INTEGER DEFAULT 0);"
        "CREATE TABLE IF NOT EXISTS codes (email TEXT PRIMARY KEY, mac TEXT, expires INTEGER, tries INTEGER);"
        "CREATE TABLE IF NOT EXISTS hits (key TEXT, at INTEGER);"
        "CREATE INDEX IF NOT EXISTS hits_key ON hits(key, at);"
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);"
    )
    return con


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _code_mac(email: str, code: str) -> str:
    key = (os.environ.get("REMIND_CRON_SECRET", "") + "|desk-code").encode()
    return hmac.new(key, f"{email}|{code}".encode(), hashlib.sha256).hexdigest()


def _limited(con: sqlite3.Connection, key: str, limit: tuple[int, int]) -> bool:
    now = int(time.time())
    n, window = limit
    con.execute("DELETE FROM hits WHERE at < ?", (now - 86400,))
    used = con.execute("SELECT count(*) FROM hits WHERE key=? AND at>=?", (key, now - window)).fetchone()[0]
    if used >= n:
        return True
    con.execute("INSERT INTO hits (key, at) VALUES (?,?)", (key, now))
    return False


def send_code(email: str, ip: str, *, sender=None) -> None:
    em = (email or "").strip().lower()
    with _con() as con:
        if _limited(con, "ip-send:" + ip, SEND_PER_IP) or _limited(con, "em-send:" + em, SEND_PER_EMAIL):
            return
        if em not in staff_emails():
            return
        code = f"{secrets.randbelow(1_000_000):06d}"
        con.execute(
            "INSERT OR REPLACE INTO codes (email, mac, expires, tries) VALUES (?,?,?,0)",
            (em, _code_mac(em, code), int(time.time()) + CODE_TTL),
        )
    if sender is None:
        from .notify import send_email as sender
    sender(em, f"Practice desk sign-in code: {code}",
           f"Your practice desk sign-in code is {code}.\n\nIt works once and expires in 10 minutes. "
           "If you didn't ask for it, ignore this email.\n")


def verify_code(email: str, code: str, ip: str) -> str | None:
    em = (email or "").strip().lower()
    given = "".join(ch for ch in str(code or "") if ch.isdigit())
    now = int(time.time())
    with _con() as con:
        if _limited(con, "ip-verify:" + ip, VERIFY_PER_IP):
            return None
        row = con.execute("SELECT * FROM codes WHERE email=?", (em,)).fetchone()
        if not row or row["expires"] < now or row["tries"] >= CODE_TRIES or len(given) != 6:
            if row:
                con.execute("UPDATE codes SET tries=tries+1 WHERE email=?", (em,))
            return None
        if not hmac.compare_digest(row["mac"], _code_mac(em, given)):
            con.execute("UPDATE codes SET tries=tries+1 WHERE email=?", (em,))
            return None
        con.execute("DELETE FROM codes WHERE email=?", (em,))
        con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('code_login_done', ?)", (str(now),))
    return mint(em)


def mint(email: str) -> str:
    token = "ds1." + secrets.token_urlsafe(32)
    now = int(time.time())
    with _con() as con:
        con.execute(
            "INSERT INTO sessions (hash, email, created, last_used, expires) VALUES (?,?,?,?,?)",
            (_sha(token), email, now, now, now + SESSION_TTL),
        )
    return token


def check(token: str) -> str | None:
    if not (token or "").startswith("ds1."):
        return None
    now = int(time.time())
    with _con() as con:
        row = con.execute("SELECT * FROM sessions WHERE hash=?", (_sha(token),)).fetchone()
        if not row or row["revoked"] or row["expires"] < now:
            return None
        if now - row["last_used"] >= TOUCH_EVERY:
            con.execute("UPDATE sessions SET last_used=?, expires=? WHERE hash=?", (now, now + SESSION_TTL, row["hash"]))
    return row["email"]


def revoke(token: str) -> int:
    with _con() as con:
        return con.execute("UPDATE sessions SET revoked=1 WHERE hash=?", (_sha(token or ""),)).rowcount


def revoke_all() -> int:
    with _con() as con:
        return con.execute("UPDATE sessions SET revoked=1 WHERE revoked=0").rowcount


def legacy_allowed() -> bool:
    with _con() as con:
        return con.execute("SELECT 1 FROM meta WHERE k='code_login_done'").fetchone() is None


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "revoke-all":
        print({"revoked": revoke_all()})
    elif cmd == "status":
        with _con() as c:
            act = c.execute("SELECT count(*) FROM sessions WHERE revoked=0 AND expires>?", (int(time.time()),)).fetchone()[0]
        print({"active_sessions": act, "legacy_secret_in_browser": legacy_allowed(), "staff_emails": len(staff_emails())})
    else:
        print(__doc__)
