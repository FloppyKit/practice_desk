"""Pre-authed chat-reply links: /c/<code>. Opaque random code (no PHI), stored server-side,
scoped to one thread + client, reusable for 7 days (link scanners would burn a single-use code),
revocable per thread. A GET only shows a button; the POST mints the normal portal session."""
from __future__ import annotations

import secrets
import sqlite3
import string
import sys
import time
from pathlib import Path

TTL_SECONDS = 7 * 24 * 3600
ALPHABET = string.ascii_letters + string.digits
CODE_LEN = 10  # ~59 bits


def _db() -> sqlite3.Connection:
    from .chat_threads import _dir

    con = sqlite3.connect(str(Path(_dir()) / "_links.sqlite"), timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS links (code TEXT PRIMARY KEY, thread TEXT, client_id TEXT, "
                "issued_at REAL, revoked INTEGER DEFAULT 0)")
    return con


def issue(thread: str, client_id: str, *, now: float | None = None) -> str:
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    with _db() as con:
        con.execute("INSERT INTO links (code, thread, client_id, issued_at) VALUES (?,?,?,?)",
                    (code, thread, str(client_id), now if now is not None else time.time()))
    return code


def lookup(code: str, *, now: float | None = None) -> dict | None:
    """Valid, unrevoked, unexpired -> {thread, client_id}; anything else -> None."""
    if not code or len(code) != CODE_LEN or any(c not in ALPHABET for c in code):
        return None
    with _db() as con:
        r = con.execute("SELECT thread, client_id, issued_at, revoked FROM links WHERE code=?", (code,)).fetchone()
    if not r or r[3]:
        return None
    if (now if now is not None else time.time()) - float(r[2]) > TTL_SECONDS:
        return None
    return {"thread": r[0], "client_id": r[1]}


def revoke_thread(thread: str) -> int:
    with _db() as con:
        return con.execute("UPDATE links SET revoked=1 WHERE thread=? AND revoked=0", (thread,)).rowcount


if __name__ == "__main__":  # python -m app.chat_links revoke <thread-token>
    if sys.argv[1:2] == ["revoke"] and len(sys.argv) == 3:
        print("revoked", revoke_thread(sys.argv[2]))
