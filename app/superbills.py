"""Superbill v2: pick visits, fees, and imported visits onto one statement.

Rows are calendar visits (past, not cancelled), older imported visits, and standalone fees.
CPT lines and fees come from config/billing-codes.yaml (billing.load_codes); visit type via guess_code.
A visit may sit on any number of superbills; older superbills and PDFs are never touched.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from . import billing, clients_db

FEE_LABELS = {"no_show": "No-show fee", "other": "Fee", "visit": "Visit fee"}
_KEY = re.compile(r"^(ev|imp|fee):[A-Za-z0-9_.@:\-]{1,160}$")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS superbills (
          id TEXT PRIMARY KEY,
          client_id TEXT NOT NULL,
          statement TEXT,
          name TEXT,
          path TEXT,
          first_dos TEXT,
          last_dos TEXT,
          created_at TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS superbill_items (
          id TEXT PRIMARY KEY,
          superbill_id TEXT NOT NULL,
          client_id TEXT NOT NULL,
          item_key TEXT NOT NULL,
          dos TEXT,
          created_at TEXT
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_sbi_client ON superbill_items(client_id, item_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sb_client ON superbills(client_id)")


def valid_key(key: Any) -> bool:
    return isinstance(key, str) and bool(_KEY.match(key))


def _cents(n: int) -> str:
    return f"{int(n or 0) / 100:.2f}"


def _codes() -> dict[str, Any]:
    try:
        return billing.load_codes()
    except Exception:
        return {"pos": "10", "visit_types": {}}


def _is_intro(text: str) -> bool:
    return "intro" in (text or "").lower()


def _lines_for(code: str, codes: dict[str, Any], dos: str, paid: bool) -> tuple[str, int, list[dict[str, str]]]:
    spec = (codes.get("visit_types") or {}).get(code)
    if not spec:
        return "", 0, []
    pos = str(codes.get("pos") or "10")
    lines = []
    for ln in spec["lines"]:
        fee = _cents(ln["fee_cents"])
        lines.append({"dos": dos, "pos": pos, "service": ln["service"], "code": ln["code"],
                      "modifier": ln["modifier"] or "95", "units": "1", "cost": fee,
                      "paid": fee if paid else "0.00"})
    return spec["label"], int(spec["price_cents"]), lines


def links_for_client(client_id: str) -> dict[str, list[str]]:
    """item_key -> statement numbers it already sits on (v2 superbills + live Bill-visit superbills)."""
    out: dict[str, list[str]] = {}
    with clients_db.connect() as con:
        ensure(con)
        billing._ensure(con)
        for r in con.execute(
            "SELECT i.item_key, s.statement FROM superbill_items i JOIN superbills s ON s.id = i.superbill_id "
            "WHERE i.client_id = ? ORDER BY s.created_at", (client_id,)
        ).fetchall():
            out.setdefault(r[0], []).append(str(r[1] or ""))
        for r in con.execute(
            "SELECT event_key, statement FROM billings WHERE client_id = ? AND superbill_status = 'saved' "
            "AND dry_run = 0", (client_id,)
        ).fetchall():
            ek = str(r[0] or "")
            if ek and not ek.startswith("manual:"):
                out.setdefault("ev:" + ek, []).append(str(r[1] or ""))
        con.commit()
    return {k: [s for s in dict.fromkeys(v)] for k, v in out.items()}


def _paid_events(client_id: str) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Event ids recorded as paid (billings or fee ledger), and all fee rows by id."""
    paid: set[str] = set()
    fees: dict[str, dict[str, Any]] = {}
    with clients_db.connect() as con:
        billing._ensure(con)
        try:
            for r in con.execute(
                "SELECT event_key, payment_status FROM billings WHERE client_id = ? AND dry_run = 0", (client_id,)
            ).fetchall():
                if str(r[1] or "") == "paid":
                    paid.add(str(r[0] or ""))
        except Exception:
            pass
        try:
            for r in con.execute("SELECT * FROM fees WHERE client_id = ?", (client_id,)).fetchall():
                d = {k: r[k] for k in r.keys()}
                fees[str(d["id"])] = d
                if d.get("status") == "paid" and d.get("event_id"):
                    paid.add(str(d["event_id"]))
        except Exception:
            pass
        con.commit()
    return paid, fees


def _imported(client_id: str, now_utc: str) -> list[dict[str, Any]]:
    with clients_db.connect() as con:
        try:
            rows = con.execute(
                "SELECT id, start_at, start_utc, service, status, duration_min, lines_json FROM imported_appointments "
                "WHERE client_id = ? AND start_utc < ? ORDER BY start_utc DESC", (client_id, now_utc)
            ).fetchall()
        except Exception:
            rows = []
    return [{k: r[k] for k in r.keys()} for r in rows]


def _in_range(dos: str, start: str, end: str) -> bool:
    return (not start or dos >= start) and (not end or dos <= end)


def build_list(client_id: str, cal_visits: list[dict[str, Any]], *, start: str = "", end: str = "",
               now_utc: str | None = None) -> dict[str, Any]:
    codes = _codes()
    links = links_for_client(client_id)
    paid_events, fee_rows = _paid_events(client_id)
    items: list[dict[str, Any]] = []
    seen_slots: set[str] = set()
    cal_ids: set[str] = set()
    for v in cal_visits:
        if v.get("upcoming") or not v.get("event_id"):
            continue
        st = str(v.get("start_iso") or "")
        dos = st[:10]
        if len(dos) != 10 or _is_intro(str(v.get("event_type") or "")):
            continue
        eid = str(v["event_id"])
        cal_ids.add(eid)
        seen_slots.add(st[:16].replace("T", " "))
        code = billing.guess_code(v.get("event_type") or "", v.get("duration_minutes"))
        paid = eid in paid_events
        label, cents, lines = _lines_for(code, codes, dos, paid)
        items.append({"key": "ev:" + eid, "kind": "visit", "source": "calendar", "dos": dos, "time": st[11:16],
                      "visit_code": code, "label": label or "Visit", "fee_cents": cents, "paid": paid,
                      "lines": lines})
    for r in _imported(client_id, now_utc or _now()):
        status = str(r.get("status") or "")
        if status in ("Cancelled", "DidNotAttend"):
            continue
        local = str(r.get("start_at") or "")
        dos = local[:10]
        svc = str(r.get("service") or "")
        if len(dos) != 10 or _is_intro(svc):
            continue
        slot = local[:16].replace("T", " ")
        if slot in seen_slots:
            continue  # same visit is already on the calendar list
        seen_slots.add(slot)
        try:
            old = json.loads(r.get("lines_json") or "[]") or []
        except ValueError:
            old = []
        paid = any("paid" in str((x or {}).get("invoice") or "").lower() for x in old if isinstance(x, dict))
        code = billing.guess_code("new" if "initial" in svc.lower() else svc, r.get("duration_min"))
        label, cents, lines = _lines_for(code, codes, dos, paid)
        items.append({"key": "imp:" + str(r["id"]), "kind": "visit", "source": "imported", "dos": dos,
                      "time": local[11:16], "visit_code": code, "label": label or "Visit", "fee_cents": cents,
                      "paid": paid, "lines": lines})
    for fid, f in fee_rows.items():
        kind = str(f.get("kind") or "other")
        if f.get("status") == "waived":
            continue
        if kind == "visit" and f.get("event_id") and str(f["event_id"]) in cal_ids:
            continue  # the visit row already carries this fee
        if kind == "visit" and f.get("event_id"):
            continue  # visit fee for a visit outside the list (e.g. upcoming): not standalone
        dos = str(f.get("dos") or "")[:10]
        cents = int(f.get("amount_cents") or 0)
        paid = f.get("status") == "paid"
        label = FEE_LABELS.get(kind, "Fee")
        items.append({"key": "fee:" + fid, "kind": "fee", "source": "fees", "dos": dos, "time": "",
                      "visit_code": "", "label": label, "fee_cents": cents, "paid": paid, "optional": True,
                      "lines": [{"dos": dos, "pos": str(codes.get("pos") or "10"), "service": label, "code": "",
                                 "modifier": "", "units": "1", "cost": _cents(cents),
                                 "paid": _cents(cents) if paid else "0.00"}]})
    items = [i for i in items if _in_range(i["dos"], start, end)]
    items.sort(key=lambda i: (i["dos"], i["time"]), reverse=True)
    unlinked, linked = [], []
    for i in items:
        on = links.get(i["key"]) or []
        i["statements"] = on
        (linked if on else unlinked).append(i)
    return {"unlinked": unlinked, "linked": linked}


def record(client_id: str, *, statement: str, name: str, path: str, keys: list[str], dos_by_key: dict[str, str],
           all_dos: list[str]) -> str:
    sid = uuid.uuid4().hex
    now = _now()
    ds = sorted(d for d in all_dos if d)
    with clients_db.connect() as con:
        ensure(con)
        con.execute(
            "INSERT INTO superbills (id, client_id, statement, name, path, first_dos, last_dos, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sid, client_id, statement, name, path, ds[0] if ds else "", ds[-1] if ds else "", now),
        )
        for k in dict.fromkeys(keys):
            if valid_key(k):
                con.execute(
                    "INSERT INTO superbill_items (id, superbill_id, client_id, item_key, dos, created_at) "
                    "VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, sid, client_id, k, dos_by_key.get(k, ""), now),
                )
        con.commit()
    return sid


def count(client_id: str = "") -> int:
    with clients_db.connect() as con:
        ensure(con)
        q = "SELECT count(*) FROM superbills" + (" WHERE client_id = ?" if client_id else "")
        n = con.execute(q, (client_id,) if client_id else ()).fetchone()[0]
        con.commit()
    return int(n)
