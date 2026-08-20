"""Visit-fee ledger. Ops money, not a chart. Same SQLite as the phone book."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from . import clients_db

KINDS = ("visit", "no_show", "other")
STATUSES = ("owed", "paid", "waived", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS fees (
          id TEXT PRIMARY KEY,
          client_id TEXT NOT NULL,
          dos TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'visit',
          amount_cents INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'owed',
          event_id TEXT,
          stripe_id TEXT,
          pay_url TEXT,
          note TEXT,
          created_at TEXT,
          updated_at TEXT
        )
        """
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(fees)").fetchall()}
    if "pay_url" not in cols:
        con.execute("ALTER TABLE fees ADD COLUMN pay_url TEXT")
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_fees_client_dos ON fees(client_id, dos)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_fees_status ON fees(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_fees_stripe ON fees(stripe_id)")


def _row(r) -> dict[str, Any] | None:
    if r is None:
        return None
    d = {k: r[k] for k in r.keys()}
    d["amount_cents"] = int(d.get("amount_cents") or 0)
    return d


def dollars(cents: int) -> str:
    n = int(cents or 0)
    sign = "-" if n < 0 else ""
    n = abs(n)
    return f"{sign}${n // 100:,}.{n % 100:02d}"


def parse_amount(raw: Any) -> int:
    if raw is None or raw == "":
        raise ValueError("need an amount")
    if isinstance(raw, int):
        return raw
    s = str(raw).strip().replace("$", "").replace(",", "")
    if not s:
        raise ValueError("need an amount")
    if "." in s:
        return int(round(float(s) * 100))
    n = int(s)
    if n < 1000:
        return n * 100
    return n


def suggest_amount_cents(duration_minutes: int | None, role: str = "psychiatrist") -> int:
    from .config import load_config
    from .practice import service_set

    dur = int(duration_minutes or 30)
    try:
        cfg = load_config()
        for et in (cfg.get("event_types") or {}).values():
            if not isinstance(et, dict) or et.get("enabled") is False:
                continue
            by = et.get("price_by_duration") or {}
            if dur in by:
                return int(round(float(by[dur]) * 100))
            if str(dur) in by:
                return int(round(float(by[str(dur)]) * 100))
            raw_d = et.get("duration_minutes")
            match = raw_d == dur or raw_d == [dur] or (
                isinstance(raw_d, list) and dur in raw_d
            )
            if match and et.get("price_usd") is not None:
                return int(round(float(et.get("price_usd") or 0) * 100))
    except Exception:
        pass
    best = None
    best_gap = 10_000
    for spec in service_set(role):
        d = int(spec.get("duration") or 0)
        gap = abs(d - dur)
        if gap < best_gap:
            best_gap = gap
            best = spec
    if not best:
        return 26500 if role != "therapist" else 0
    total = 0
    for line in best.get("lines") or []:
        try:
            total += parse_amount(line.get("paid") or line.get("cost") or 0)
        except ValueError:
            continue
    return total or (26500 if role != "therapist" else 0)


def public_line(row: dict[str, Any]) -> dict[str, Any]:
    cents = int(row.get("amount_cents") or 0)
    return {
        "id": row.get("id"),
        "client_id": row.get("client_id"),
        "dos": row.get("dos"),
        "kind": row.get("kind"),
        "amount_cents": cents,
        "amount_pretty": dollars(cents),
        "status": row.get("status"),
        "event_id": row.get("event_id") or "",
        "stripe_id": row.get("stripe_id") or "",
        "pay_url": row.get("pay_url") or "",
        "note": row.get("note") or "",
        "updated": row.get("updated_at") or "",
    }


def list_for_client(client_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    if not client_id:
        return []
    with clients_db.connect() as con:
        _ensure(con)
        rows = con.execute(
            "SELECT * FROM fees WHERE client_id = ? ORDER BY dos DESC, created_at DESC LIMIT ?",
            (client_id, max(1, min(int(limit), 40))),
        ).fetchall()
        con.commit()
    return [public_line(x) for x in (_row(r) for r in rows) if x]


def list_open(client_id: str) -> list[dict[str, Any]]:
    if not client_id:
        return []
    with clients_db.connect() as con:
        _ensure(con)
        rows = con.execute(
            "SELECT * FROM fees WHERE client_id = ? AND status IN ('owed', 'failed') "
            "ORDER BY dos ASC, created_at ASC",
            (client_id,),
        ).fetchall()
        con.commit()
    return [public_line(x) for x in (_row(r) for r in rows) if x]


def owed_cents(client_id: str) -> int:
    if not client_id:
        return 0
    with clients_db.connect() as con:
        _ensure(con)
        row = con.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS n FROM fees "
            "WHERE client_id = ? AND status IN ('owed', 'failed')",
            (client_id,),
        ).fetchone()
        con.commit()
    return int(row["n"] if row else 0)


def find_no_show(event_id: str) -> dict[str, Any] | None:
    eid = (event_id or "").strip()
    if not eid:
        return None
    with clients_db.connect() as con:
        _ensure(con)
        row = _row(
            con.execute(
                "SELECT * FROM fees WHERE event_id = ? AND kind = 'no_show' "
                "ORDER BY created_at DESC LIMIT 1",
                (eid,),
            ).fetchone()
        )
        con.commit()
    return public_line(row) if row else None


def no_show_event_ids(client_id: str) -> list[str]:
    if not client_id:
        return []
    with clients_db.connect() as con:
        _ensure(con)
        rows = con.execute(
            "SELECT DISTINCT event_id FROM fees WHERE client_id = ? AND kind = 'no_show' "
            "AND event_id != ''",
            (client_id,),
        ).fetchall()
        con.commit()
    return [str(r["event_id"]) for r in rows if r["event_id"]]


def get(fee_id: str) -> dict[str, Any] | None:
    if not fee_id:
        return None
    with clients_db.connect() as con:
        _ensure(con)
        row = _row(con.execute("SELECT * FROM fees WHERE id = ?", (fee_id,)).fetchone())
        con.commit()
    return public_line(row) if row else None


def create(
    *,
    client_id: str,
    dos: str,
    amount_cents: int,
    kind: str = "visit",
    note: str = "",
    event_id: str = "",
    status: str = "owed",
) -> dict[str, Any]:
    if not client_id:
        raise ValueError("need a client")
    dos = (dos or "")[:10]
    if len(dos) != 10:
        raise ValueError("need a date of service")
    cents = int(amount_cents)
    if cents <= 0 or cents > 5_000_00:
        raise ValueError("amount must be between $0.01 and $5,000")
    kind = kind if kind in KINDS else "visit"
    status = status if status in STATUSES else "owed"
    now = _now()
    fid = str(uuid.uuid4())
    with clients_db.connect() as con:
        _ensure(con)
        con.execute(
            "INSERT INTO fees (id, client_id, dos, kind, amount_cents, status, "
            "event_id, stripe_id, pay_url, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fid,
                client_id,
                dos,
                kind,
                cents,
                status,
                (event_id or "")[:80],
                "",
                "",
                (note or "")[:80],
                now,
                now,
            ),
        )
        con.commit()
    got = get(fid)
    if not got:
        raise RuntimeError("fee insert failed")
    return got


def set_status(
    fee_id: str,
    *,
    status: str,
    stripe_id: str | None = None,
    pay_url: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError("bad status")
    with clients_db.connect() as con:
        _ensure(con)
        sets = ["status = ?", "updated_at = ?"]
        args: list[Any] = [status, _now()]
        if stripe_id is not None:
            sets.append("stripe_id = ?")
            args.append(stripe_id[:120])
        if pay_url is not None:
            sets.append("pay_url = ?")
            args.append(pay_url[:400])
        args.append(fee_id)
        con.execute(f"UPDATE fees SET {', '.join(sets)} WHERE id = ?", args)
        con.commit()
    got = get(fee_id)
    if not got:
        raise ValueError("fee not found")
    return got


def attach_invoice(fee_ids: list[str], *, stripe_id: str, pay_url: str = "") -> int:
    n = 0
    for fid in fee_ids:
        if not fid:
            continue
        set_status(fid, status="owed", stripe_id=stripe_id, pay_url=pay_url)
        n += 1
    return n


def mark_paid_by_stripe(stripe_id: str) -> int:
    sid = (stripe_id or "").strip()
    if not sid:
        return 0
    with clients_db.connect() as con:
        _ensure(con)
        cur = con.execute(
            "UPDATE fees SET status = 'paid', updated_at = ? "
            "WHERE stripe_id = ? AND status != 'paid'",
            (_now(), sid),
        )
        con.commit()
        return int(cur.rowcount or 0)


def _period_bounds(month: str = "", year: str = "") -> tuple[str, str, str]:
    """Inclusive start, exclusive end, label key (YYYY-MM or YYYY)."""
    now = datetime.now(timezone.utc)
    m = (month or "").strip()
    y = (year or "").strip()
    if len(m) == 7 and m[4] == "-" and m[:4].isdigit() and m[5:].isdigit():
        yy, mm = int(m[:4]), int(m[5:7])
        if 1 <= mm <= 12:
            start = f"{yy:04d}-{mm:02d}-01"
            if mm == 12:
                end = f"{yy + 1:04d}-01-01"
            else:
                end = f"{yy:04d}-{mm + 1:02d}-01"
            return start, end, f"{yy:04d}-{mm:02d}"
    yy = int(y) if y.isdigit() and len(y) == 4 else now.year
    return f"{yy:04d}-01-01", f"{yy + 1:04d}-01-01", f"{yy:04d}"


def _month_label(key: str) -> str:
    if len(key) == 7:
        try:
            dt = datetime.strptime(key + "-01", "%Y-%m-%d")
            return dt.strftime("%B %Y")
        except ValueError:
            return key
    return key


def _summarize(rows: list[dict[str, Any]], *, period: str, with_lines: bool = False) -> dict[str, Any]:
    collected = owed = waived = failed = 0
    visits = no_shows = 0
    for r in rows:
        cents = int(r.get("amount_cents") or 0)
        st = r.get("status") or ""
        kind = r.get("kind") or ""
        if st == "paid":
            collected += cents
        elif st == "waived":
            waived += cents
        elif st == "failed":
            failed += cents
            owed += cents
        else:
            owed += cents
        if kind == "no_show":
            no_shows += 1
        elif kind == "visit":
            visits += 1
    out = {
        "period": period,
        "label": _month_label(period) if len(period) == 7 else period,
        "collected_cents": collected,
        "collected_pretty": dollars(collected),
        "owed_cents": owed,
        "owed_pretty": dollars(owed),
        "waived_cents": waived,
        "waived_pretty": dollars(waived),
        "failed_cents": failed,
        "visits": visits,
        "no_shows": no_shows,
        "count": len(rows),
    }
    if with_lines:
        named = []
        for r in rows[:16]:
            line = public_line(r)
            who = ""
            cid = str(r.get("client_id") or "")
            if cid:
                crow = clients_db.get_by_id(cid)
                if crow:
                    who = str(crow.get("display_name") or crow.get("email") or "")
            line["client_name"] = who
            named.append(line)
        out["lines"] = named
    return out


def clinic_books(*, month: str = "") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    key = (month or "").strip() or now.strftime("%Y-%m")
    m0, m1, mkey = _period_bounds(month=key)
    y0, y1, ykey = _period_bounds(year=mkey[:4])
    with clients_db.connect() as con:
        _ensure(con)
        month_rows = [
            x
            for x in (
                _row(r)
                for r in con.execute(
                    "SELECT * FROM fees WHERE dos >= ? AND dos < ? ORDER BY dos DESC, created_at DESC",
                    (m0, m1),
                ).fetchall()
            )
            if x
        ]
        year_rows = [
            x
            for x in (
                _row(r)
                for r in con.execute(
                    "SELECT * FROM fees WHERE dos >= ? AND dos < ? ORDER BY dos DESC",
                    (y0, y1),
                ).fetchall()
            )
            if x
        ]
        con.commit()
    return {
        "ok": True,
        "month": _summarize(month_rows, period=mkey, with_lines=True),
        "year": _summarize(year_rows, period=ykey, with_lines=False),
    }


def client_books(client_id: str, *, duration_minutes: int | None = None, role: str = "") -> dict[str, Any]:
    from .stripe_pay import stripe_ready

    lines = list_for_client(client_id, limit=8)
    owed = owed_cents(client_id)
    suggest = suggest_amount_cents(duration_minutes, role)
    pay_url = ""
    for ln in lines:
        if ln.get("pay_url") and ln.get("status") in ("owed", "failed"):
            pay_url = ln["pay_url"]
            break
    return {
        "owed_cents": owed,
        "owed_pretty": dollars(owed),
        "suggest_cents": suggest,
        "suggest_pretty": dollars(suggest),
        "stripe_ready": stripe_ready(),
        "invoice_url": pay_url,
        "open_count": len(list_open(client_id)),
        "no_show_event_ids": no_show_event_ids(client_id),
        "lines": lines,
    }
