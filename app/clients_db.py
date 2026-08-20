"""On-box client phone book. SQLite. No note bodies. No PHI in logs."""
from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("CLIENTS_DB") or "/data/logs/clients.sqlite")

COLUMNS = (
    "id",
    "display_name",
    "preferred_name",
    "given_name",
    "family_name",
    "email",
    "phone_e164",
    "dob",
    "status",
    "source",
    "card_on_file",
    "stripe_customer_id",
    "stripe_payment_method_id",
    "stripe_card_brand",
    "stripe_card_last4",
    "stripe_card_exp_month",
    "stripe_card_exp_year",
    "stripe_card_updated_at",
    "drive_folder_url",
    "drive_folder_short_url",
    "billing_folder_url",
    "billing_folder_short_url",
    "drive_folder_path",
    "billing_folder_path",
    "carepatron_contact_id",
    "created_at",
    "updated_at",
)

_like_safe = re.compile(r"[%_\\]")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ready() -> bool:
    try:
        connect()
        return True
    except Exception:
        return False


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    _init(con)
    return con


def _init(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
          id TEXT PRIMARY KEY,
          display_name TEXT,
          preferred_name TEXT,
          given_name TEXT,
          family_name TEXT,
          email TEXT,
          phone_e164 TEXT,
          dob TEXT,
          status TEXT DEFAULT 'active',
          source TEXT,
          card_on_file INTEGER DEFAULT 0,
          stripe_customer_id TEXT,
          stripe_payment_method_id TEXT,
          stripe_card_brand TEXT,
          stripe_card_last4 TEXT,
          stripe_card_exp_month INTEGER,
          stripe_card_exp_year INTEGER,
          stripe_card_updated_at TEXT,
          drive_folder_url TEXT,
          drive_folder_short_url TEXT,
          billing_folder_url TEXT,
          billing_folder_short_url TEXT,
          drive_folder_path TEXT,
          billing_folder_path TEXT,
          carepatron_contact_id TEXT,
          created_at TEXT,
          updated_at TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_clients_email ON clients(email)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_clients_phone ON clients(phone_e164)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_clients_name ON clients(display_name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_clients_family ON clients(family_name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_clients_pref ON clients(preferred_name)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_clients_stripe ON clients(stripe_customer_id)")
    con.commit()


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    if r is None:
        return None
    d = {k: r[k] for k in r.keys()}
    d["card_on_file"] = bool(d.get("card_on_file"))
    return d


def get_by_id(cid: str) -> dict[str, Any] | None:
    if not cid:
        return None
    with connect() as con:
        return _row(con.execute("SELECT * FROM clients WHERE id = ?", (cid,)).fetchone())


def get_by_email(email: str) -> dict[str, Any] | None:
    em = (email or "").strip().lower()
    if not em:
        return None
    with connect() as con:
        return _row(
            con.execute(
                "SELECT * FROM clients WHERE lower(email) = ? LIMIT 1", (em,)
            ).fetchone()
        )


def get_by_phone(e164: str) -> dict[str, Any] | None:
    if not e164:
        return None
    with connect() as con:
        return _row(
            con.execute(
                "SELECT * FROM clients WHERE phone_e164 = ? LIMIT 1", (e164,)
            ).fetchone()
        )


def get_by_stripe_customer(cust: str) -> dict[str, Any] | None:
    if not cust:
        return None
    with connect() as con:
        return _row(
            con.execute(
                "SELECT * FROM clients WHERE stripe_customer_id = ? LIMIT 1", (cust,)
            ).fetchone()
        )


def list_by_dob(dob: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT * FROM clients WHERE dob = ? LIMIT ?", (dob, limit)
        ).fetchall()
    return [x for x in (_row(r) for r in rows) if x]


def search(q: str, limit: int = 12) -> list[dict[str, Any]]:
    raw = (q or "").strip()
    if len(raw) < 2:
        return []
    limit = max(1, min(int(limit), 25))
    needle = _like_safe.sub("", raw.replace(",", " ")).strip()
    like = f"%{needle}%"
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    with connect() as con:
        for sql, args in (
            ("SELECT * FROM clients WHERE phone_e164 = ? LIMIT ?", (raw, limit)),
            ("SELECT * FROM clients WHERE lower(email) = ? LIMIT ?", (raw.lower(), limit)),
            (
                """SELECT * FROM clients WHERE
                   display_name LIKE ? COLLATE NOCASE
                   OR family_name LIKE ? COLLATE NOCASE
                   OR preferred_name LIKE ? COLLATE NOCASE
                   OR given_name LIKE ? COLLATE NOCASE
                   OR email LIKE ? COLLATE NOCASE
                   LIMIT ?""",
                (like, like, like, like, like, limit),
            ),
        ):
            for r in con.execute(sql, args).fetchall():
                row = _row(r)
                if not row or row["id"] in seen:
                    continue
                if (row.get("status") or "").lower() == "archived":
                    continue
                seen.add(row["id"])
                out.append(row)
                if len(out) >= limit:
                    return out
    return out


def upsert(fields: dict[str, Any]) -> dict[str, Any]:
    data = {k: fields.get(k) for k in COLUMNS if k in fields or k == "id"}
    cid = str(data.get("id") or "").strip() or str(uuid.uuid4())
    data["id"] = cid
    data["updated_at"] = _now()
    if not data.get("created_at"):
        existing = get_by_id(cid)
        data["created_at"] = (existing or {}).get("created_at") or _now()
    if "card_on_file" in data:
        data["card_on_file"] = 1 if data.get("card_on_file") else 0
    cols = [k for k in COLUMNS if k in data]
    placeholders = ",".join("?" for _ in cols)
    assignments = ",".join(f"{k}=excluded.{k}" for k in cols if k != "id")
    values = [data.get(k) for k in cols]
    with connect() as con:
        con.execute(
            f"INSERT INTO clients ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {assignments}",
            values,
        )
        con.commit()
    got = get_by_id(cid)
    if not got:
        raise RuntimeError("client upsert failed")
    return got


def create(
    *,
    name: str,
    email: str,
    phone: str = "",
    source: str = "desk",
) -> dict[str, Any]:
    parts = (name or "").split(None, 1)
    payload: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "display_name": name,
        "preferred_name": parts[0] if parts else "",
        "given_name": parts[0] if parts else "",
        "family_name": parts[1] if len(parts) > 1 else "",
        "email": email,
        "status": "active",
        "source": source,
        "created_at": _now(),
    }
    if phone:
        payload["phone_e164"] = phone
    return upsert(payload)


def count() -> int:
    with connect() as con:
        row = con.execute("SELECT COUNT(*) AS n FROM clients").fetchone()
    return int(row["n"] if row else 0)
