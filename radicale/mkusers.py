#!/usr/bin/env python3
"""Write radicale/users as bcrypt from CALDAV_PASSWORD (stdin or env)."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def hash_pw(pw: str) -> str:
    try:
        from passlib.hash import bcrypt as b

        return b.using(ident="2y", rounds=12).hash(pw)
    except Exception:
        import bcrypt

        return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()


def main() -> None:
    pw = (os.environ.get("CALDAV_PASSWORD") or "").strip()
    if not pw and not sys.stdin.isatty():
        pw = sys.stdin.read().strip()
    if not pw:
        sys.exit("set CALDAV_PASSWORD or pipe the password")
    user = (os.environ.get("CALDAV_USER") or "booker").strip()
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "users")
    out.write_text(f"{user}:{hash_pw(pw)}\n")
    out.chmod(0o600)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
