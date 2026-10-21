"""One-click billing: visit codes, letterhead gate, billing records, expiring share links.

Ops money and pointers, not a chart. Same SQLite as the phone book. No PHI in logs.
Plan: docs/GO_BILLING_ONE_CLICK.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import clients_db

SHARE_TTL_SECONDS = 30 * 24 * 3600
LICENSE_STATES = ("KS", "MO", "IL")
PAYMENTS = ("link", "card")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def live_enabled() -> bool:
    """BILLING_LIVE=true lets Confirm write fees, call Stripe, save to Drive, and send.
    Unset means every Bill visit is a dry run."""
    return _flag("BILLING_LIVE")


def sms_enabled() -> bool:
    """Billing SMS goes only through the practice Spruce API line (not Twilio, not ENABLE_SMS).
    On when BILLING_SMS=true and the Spruce token + phone endpoint are configured."""
    from .spruce_api import spruce_config

    token, endpoint = spruce_config()
    return _flag("BILLING_SMS") and bool(token and endpoint)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---- visit codes ---------------------------------------------------------


def codes_path() -> Path:
    raw = os.environ.get("BILLING_CODES", "").strip()
    if raw:
        return Path(raw)
    beside = Path(os.environ.get("BOOKER_CONFIG", "/app/config/event-types.yaml")).parent / "billing-codes.yaml"
    if beside.is_file():
        return beside
    return Path(__file__).resolve().parent.parent / "config" / "billing-codes.yaml"


def load_codes(path: Path | None = None) -> dict[str, Any]:
    raw = yaml.safe_load((path or codes_path()).read_text(encoding="utf-8")) or {}
    pos = str(raw.get("pos") or "10")
    out: dict[str, dict[str, Any]] = {}
    for key, spec in (raw.get("visit_types") or {}).items():
        if not isinstance(spec, dict):
            continue
        price = int(round(float(spec.get("price") or 0) * 100))
        lines = []
        for ln in spec.get("cpt") or []:
            lines.append(
                {
                    "code": str(ln.get("code") or "").strip(),
                    "modifier": str(ln.get("mod") or "95").strip(),
                    "fee_cents": int(round(float(ln.get("fee") or 0) * 100)),
                    "service": str(ln.get("service") or "").strip(),
                }
            )
        if not lines or sum(x["fee_cents"] for x in lines) != price:
            raise ValueError(f"billing code {key}: CPT fees must add up to the price")
        out[str(key)] = {
            "id": str(key),
            "label": str(spec.get("label") or key),
            "invoice_code": str(spec.get("invoice_code") or key).upper(),
            "price_cents": price,
            "lines": lines,
        }
    if not out:
        raise ValueError("no visit types in billing-codes.yaml")
    return {"pos": pos, "visit_types": out}


def guess_code(event_type: str = "", duration_minutes: Any = None) -> str:
    """new-client -> np; follow-up 60 -> fu60; follow-up 30 -> fu30. Clinician can change it."""
    et = (event_type or "").strip().lower()
    try:
        dur = int(duration_minutes or 0)
    except (TypeError, ValueError):
        dur = 0
    if et.startswith("new") or dur >= 75:
        return "np"
    if et.startswith("intro"):
        return ""
    if dur >= 45:
        return "fu60"
    return "fu30"


def dollars(cents: int) -> str:
    n = int(cents or 0)
    return f"${n // 100:,}.{n % 100:02d}"


def build_invoice(spec: dict[str, Any], pos: str, dos: str) -> dict[str, Any]:
    return {
        "invoice_code": spec["invoice_code"],
        "visit_code": spec["id"],
        "label": spec["label"],
        "dos": dos,
        "pos": pos,
        "amount_cents": spec["price_cents"],
        "amount_pretty": dollars(spec["price_cents"]),
        "lines": [
            {
                "code": ln["code"],
                "modifier": ln["modifier"],
                "pos": pos,
                "service": ln["service"],
                "fee_cents": ln["fee_cents"],
                "fee_pretty": dollars(ln["fee_cents"]),
            }
            for ln in spec["lines"]
        ],
    }


# ---- letterhead gate -----------------------------------------------------


def letterhead_missing(prac: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not str(prac.get("address") or "").strip():
        missing.append("practice address")
    if not str(prac.get("ein") or "").strip():
        missing.append("tax ID (EIN)")
    if not str(prac.get("npi") or "").strip():
        missing.append("NPI")
    have = {
        str(lic.get("state") or "").strip().upper()
        for lic in prac.get("licenses") or []
        if str(lic.get("number") or "").strip()
    }
    for st in LICENSE_STATES:
        if st not in have:
            missing.append(f"{st} license number")
    return missing


def refusal_message(missing: list[str]) -> str:
    return (
        "Superbill not built: the letterhead is incomplete. Fill in desk → Practice: "
        + ", ".join(missing)
        + ". Nothing was guessed. The invoice is not affected."
    )


# ---- diagnoses -----------------------------------------------------------


def clean_dx(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:12]:
        if isinstance(item, str):
            item = {"code": item}
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().upper()[:16]
        label = " ".join(str(item.get("label") or "").split())[:160]
        if code:
            out.append({"code": code, "label": label})
    return out


def standing_dx(row: dict[str, Any] | None) -> list[dict[str, str]]:
    try:
        return clean_dx(json.loads((row or {}).get("standing_dx") or "[]"))
    except (TypeError, ValueError):
        return []


def save_standing_dx(client_id: str, dx: list[dict[str, str]]) -> None:
    clients_db.upsert({"id": client_id, "standing_dx": json.dumps(clean_dx(dx))})


# ---- billing records -----------------------------------------------------


def _ensure(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS billings (
          id TEXT PRIMARY KEY,
          client_id TEXT NOT NULL,
          event_key TEXT NOT NULL,
          dos TEXT NOT NULL,
          visit_code TEXT,
          invoice_code TEXT,
          amount_cents INTEGER,
          payment TEXT,
          payment_status TEXT,
          dry_run INTEGER NOT NULL DEFAULT 1,
          invoice_json TEXT,
          dx_json TEXT,
          fee_id TEXT,
          stripe_id TEXT,
          pay_url TEXT,
          superbill_status TEXT,
          superbill_source TEXT,
          superbill_path TEXT,
          superbill_detail TEXT,
          statement TEXT,
          created_at TEXT,
          updated_at TEXT,
          UNIQUE(event_key, dry_run)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS share_links (
          id TEXT PRIMARY KEY,
          billing_id TEXT,
          client_id TEXT,
          source TEXT,
          path TEXT,
          name TEXT,
          expires_at INTEGER,
          revoked_at TEXT,
          hits INTEGER DEFAULT 0,
          created_at TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_share_billing ON share_links(billing_id)")


def event_key(event_id: str, client_id: str, dos: str) -> str:
    eid = (event_id or "").strip()
    return eid[:120] if eid else f"manual:{client_id}:{dos}"


def _row(r) -> dict[str, Any] | None:
    if r is None:
        return None
    d = {k: r[k] for k in r.keys()}
    for k in ("invoice_json", "dx_json"):
        try:
            d[k[:-5]] = json.loads(d.get(k) or "null")
        except ValueError:
            d[k[:-5]] = None
    return d


def get_billing(bid: str) -> dict[str, Any] | None:
    with clients_db.connect() as con:
        _ensure(con)
        return _row(con.execute("SELECT * FROM billings WHERE id = ?", (bid,)).fetchone())


def find_billing(key: str, *, dry_run: bool) -> dict[str, Any] | None:
    with clients_db.connect() as con:
        _ensure(con)
        return _row(
            con.execute(
                "SELECT * FROM billings WHERE event_key = ? AND dry_run = ?",
                (key, 1 if dry_run else 0),
            ).fetchone()
        )


def claim_billing(
    *,
    client_id: str,
    key: str,
    dos: str,
    invoice: dict[str, Any],
    dx: list[dict[str, str]],
    payment: str,
    dry_run: bool,
) -> dict[str, Any] | None:
    """Insert the billing row first so an event can only be billed once. None if already billed."""
    import sqlite3

    bid = uuid.uuid4().hex[:20]
    now = _now()
    try:
        with clients_db.connect() as con:
            _ensure(con)
            con.execute(
                "INSERT INTO billings (id, client_id, event_key, dos, visit_code, invoice_code, "
                "amount_cents, payment, payment_status, dry_run, invoice_json, dx_json, "
                "superbill_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bid,
                    client_id,
                    key,
                    dos,
                    invoice["visit_code"],
                    invoice["invoice_code"],
                    invoice["amount_cents"],
                    payment,
                    "pending",
                    1 if dry_run else 0,
                    json.dumps(invoice),
                    json.dumps(dx),
                    "pending",
                    now,
                    now,
                ),
            )
            con.commit()
    except sqlite3.IntegrityError:
        return None
    return get_billing(bid)


_UPDATABLE = {
    "payment_status", "fee_id", "stripe_id", "pay_url", "superbill_status", "superbill_source",
    "superbill_path", "superbill_detail", "statement", "dx_json",
}


def update_billing(bid: str, **fields: Any) -> dict[str, Any]:
    sets = [k for k in fields if k in _UPDATABLE]
    with clients_db.connect() as con:
        _ensure(con)
        if sets:
            con.execute(
                f"UPDATE billings SET {', '.join(k + ' = ?' for k in sets)}, updated_at = ? WHERE id = ?",
                [fields[k] for k in sets] + [_now(), bid],
            )
            con.commit()
    got = get_billing(bid)
    if not got:
        raise ValueError("billing not found")
    return got


def drop_billing(bid: str) -> None:
    """Release the claim when nothing was charged or written (e.g. Stripe unreachable)."""
    with clients_db.connect() as con:
        _ensure(con)
        con.execute("DELETE FROM billings WHERE id = ?", (bid,))
        con.commit()


def public_billing(rec: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rec:
        return None
    return {
        "id": rec["id"],
        "client_id": rec["client_id"],
        "event_key": rec["event_key"],
        "dos": rec["dos"],
        "visit_code": rec.get("visit_code") or "",
        "invoice": rec.get("invoice"),
        "diagnoses": rec.get("dx") or [],
        "payment": rec.get("payment") or "",
        "payment_status": rec.get("payment_status") or "",
        "dry_run": bool(rec.get("dry_run")),
        "fee_id": rec.get("fee_id") or "",
        "pay_url": rec.get("pay_url") or "",
        "superbill": {
            "status": rec.get("superbill_status") or "",
            "source": rec.get("superbill_source") or "",
            "path": rec.get("superbill_path") or "",
            "detail": rec.get("superbill_detail") or "",
            "statement": rec.get("statement") or "",
        },
        "share": active_share(rec["id"]),
        "pay_link": active_share(rec["id"], "pay"),
        "created": rec.get("created_at") or "",
    }


def dryrun_dir() -> Path:
    d = Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs")) / "billing-dryrun"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- signed, expiring share links ---------------------------------------


class ShareError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_DOMAIN = {"sb": b":superbill-share", "pay": b":pay-link"}


def _sig(body: str, kind: str = "sb") -> str:
    from .tokens import _b64, _secret

    return _b64(hmac.new(_secret() + _DOMAIN[kind], body.encode("ascii"), hashlib.sha256).digest())


def _kind_sql(kind: str) -> str:
    return "COALESCE(source, '') = 'pay'" if kind == "pay" else "COALESCE(source, '') != 'pay'"


def mint_share(
    *,
    billing_id: str,
    client_id: str,
    source: str,
    path: str,
    name: str,
    ttl_seconds: int = SHARE_TTL_SECONDS,
    kind: str = "sb",
) -> dict[str, Any]:
    """kind "sb": superbill PDF link. kind "pay": branded pay link; source "pay", path = Stripe invoice id."""
    from .tokens import _b64

    if kind == "pay":
        source = "pay"

    ttl = max(1, min(int(ttl_seconds), SHARE_TTL_SECONDS))
    sid = uuid.uuid4().hex[:16]
    exp = int(time.time()) + ttl
    with clients_db.connect() as con:
        _ensure(con)
        con.execute(
            "INSERT INTO share_links (id, billing_id, client_id, source, path, name, expires_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, billing_id, client_id, source, path, name[:120], exp, _now()),
        )
        con.commit()
    body = _b64(json.dumps({"k": kind, "i": sid, "x": exp}, separators=(",", ":")).encode())
    return {
        "id": sid,
        "token": f"{body}.{_sig(body, kind)}",
        "expires_at": exp,
        "expires_iso": datetime.fromtimestamp(exp, timezone.utc).isoformat(),
    }


def verify_share(token: str, kind: str = "sb") -> dict[str, Any]:
    from .tokens import _b64d

    try:
        body, sig = (token or "").split(".", 1)
        ok = hmac.compare_digest(_sig(body, kind), sig)
        payload = json.loads(_b64d(body)) if ok else {}
    except Exception:
        ok, payload = False, {}
    if not ok or payload.get("k") != kind or not payload.get("i"):
        raise ShareError(404, "This link is not valid.")
    now = int(time.time())
    if int(payload.get("x") or 0) < now:
        raise ShareError(410, "This link has expired. Ask the practice for a new one.")
    with clients_db.connect() as con:
        _ensure(con)
        r = con.execute("SELECT * FROM share_links WHERE id = ?", (payload["i"],)).fetchone()
    if r is None or (r["source"] == "pay") != (kind == "pay"):
        raise ShareError(404, "This link is not valid.")
    row = {k: r[k] for k in r.keys()}
    if row.get("revoked_at"):
        raise ShareError(410, "This link was turned off. Ask the practice for a new one.")
    if int(row.get("expires_at") or 0) < now:
        raise ShareError(410, "This link has expired. Ask the practice for a new one.")
    return row


def note_hit(sid: str) -> None:
    with clients_db.connect() as con:
        _ensure(con)
        con.execute("UPDATE share_links SET hits = COALESCE(hits, 0) + 1 WHERE id = ?", (sid,))
        con.commit()


def revoke_shares(billing_id: str, kind: str = "sb") -> int:
    with clients_db.connect() as con:
        _ensure(con)
        cur = con.execute(
            "UPDATE share_links SET revoked_at = ? WHERE billing_id = ? AND revoked_at IS NULL AND " + _kind_sql(kind),
            (_now(), billing_id),
        )
        con.commit()
        return int(cur.rowcount or 0)


def active_share(billing_id: str, kind: str = "sb") -> dict[str, Any] | None:
    with clients_db.connect() as con:
        _ensure(con)
        r = con.execute(
            "SELECT id, expires_at, hits, created_at FROM share_links WHERE billing_id = ? "
            "AND revoked_at IS NULL AND expires_at > ? AND " + _kind_sql(kind) + " ORDER BY created_at DESC LIMIT 1",
            (billing_id, int(time.time())),
        ).fetchone()
    if r is None:
        return None
    return {
        "id": r["id"],
        "expires_iso": datetime.fromtimestamp(int(r["expires_at"]), timezone.utc).isoformat(),
        "hits": int(r["hits"] or 0),
    }


# ---- superbill document ---------------------------------------------------


def superbill_doc(
    *,
    prac: dict[str, Any],
    client: dict[str, Any],
    rec: dict[str, Any],
    dx: list[dict[str, str]],
    statement: str,
    paid: bool,
    specimen: bool = False,
) -> dict[str, Any]:
    from .practice import format_licenses

    inv = rec.get("invoice") or {}
    dos = rec["dos"]
    pointers = ",".join(str(i) for i in range(1, min(len(dx), 4) + 1))
    lines = []
    for ln in inv.get("lines") or []:
        fee = f"{int(ln['fee_cents']) / 100:.2f}"
        lines.append(
            {
                "dos": dos,
                "pos": ln.get("pos") or inv.get("pos") or "10",
                "service": ln.get("service") or "",
                "code": ln.get("code") or "",
                "modifier": ln.get("modifier") or "95",
                "dx": pointers,
                "units": "1",
                "cost": fee,
                "paid": fee if paid else "0.00",
            }
        )
    licenses = format_licenses(prac.get("licenses") or [])
    blank = "[not set]"
    return {
        "statement": statement,
        "issued": datetime.now().date().isoformat(),
        "dos": dos,
        "specimen": bool(specimen),
        "letterhead": {
            "name": prac.get("legal_name") or prac.get("display_name") or "",
            "address": prac.get("address") or (blank if specimen else ""),
            "phone": prac.get("phone") or "",
            "email": prac.get("email") or "",
            "ein": prac.get("ein") or (blank if specimen else ""),
            "npi": prac.get("npi") or "",
            "licenses": licenses or ([blank] if specimen else []),
        },
        "practice": {
            "name": prac.get("legal_name") or prac.get("display_name") or "",
            "address": "",
            "ein": prac.get("ein") or (blank if specimen else ""),
            "npi": prac.get("npi") or "",
        },
        "provider": {
            "name": " ".join(
                x for x in (prac.get("clinician_name") or "", prac.get("clinician_title") or "") if x
            ),
            "email": prac.get("email") or "",
            "phone": prac.get("phone") or "",
            "npi": prac.get("npi") or "",
        },
        "client": {
            "name": client.get("name") or "",
            "email": client.get("email") or "",
            "phone": client.get("phone") or "",
            "dob": client.get("dob") or "",
        },
        "licenses": licenses or ([blank] if specimen else []),
        "diagnoses": dx,
        "lines": lines,
    }
