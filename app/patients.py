"""Client phone book — on-box SQLite. No notes. No PHI in logs."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from . import clients_db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FOLDER_HOSTS = {"go.psycharts.org", "go.drmattbrown.com", "drive.proton.me"}


def folder_url(short: str = "", long: str = "") -> str:
    """Prefer the cloaked short. Never invent a path. Only known hosts."""
    for raw in (short, long):
        u = (raw or "").strip()
        if not u.startswith("https://"):
            continue
        if urlparse(u).netloc.lower() in FOLDER_HOSTS:
            return u
    return ""


def supabase_ready() -> bool:
    """Historic name. Directory is local SQLite now."""
    return clients_db.ready()


def clients_ready() -> bool:
    return clients_db.ready()


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) >= 10:
        return "+" + digits
    return None


def normalize_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (raw or "").lower()).strip()


def parse_dob(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(s.replace(",", " "), fmt).date().isoformat()
        except ValueError:
            continue
    # 1990-4-7
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            return None
    return None


def _public_row(row: dict[str, Any], matched: str) -> dict[str, Any]:
    status = (row.get("status") or "active").lower()
    if status == "archived":
        return {"ok": False, "reason": "not_found"}
    name = row.get("preferred_name") or row.get("display_name") or "there"
    return {
        "ok": True,
        "matched": matched,
        "client_id": row.get("id"),
        "name": name,
        "email": row.get("email") or "",
        "status": status,
        "current": status == "active",
    }


def _staff_view(row: dict[str, Any]) -> dict[str, Any]:
    name = (
        row.get("display_name")
        or " ".join(
            p for p in (row.get("given_name") or "", row.get("family_name") or "") if p
        ).strip()
        or row.get("preferred_name")
        or ""
    )
    return {
        "id": row.get("id"),
        "name": name,
        "preferred_name": row.get("preferred_name") or "",
        "given_name": row.get("given_name") or "",
        "family_name": row.get("family_name") or "",
        "email": row.get("email") or "",
        "phone": row.get("phone_e164") or "",
        "dob": row.get("dob") or "",
        "status": row.get("status") or "",
        "card_on_file": bool(row.get("card_on_file")),
        "card_brand": row.get("stripe_card_brand") or "",
        "card_last4": row.get("stripe_card_last4") or "",
        "card_exp_month": row.get("stripe_card_exp_month"),
        "card_exp_year": row.get("stripe_card_exp_year"),
        "chart_url": folder_url(
            row.get("drive_folder_short_url") or "",
            row.get("drive_folder_url") or "",
        ),
        "billing_url": folder_url(
            row.get("billing_folder_short_url") or "",
            row.get("billing_folder_url") or "",
        ),
        "drive_folder_path": row.get("drive_folder_path") or "",
        "billing_folder_path": row.get("billing_folder_path") or "",
    }


def search_clients(q: str, limit: int = 12) -> list[dict[str, Any]]:
    """Staff lookup. Returns fill-in fields only — no notes/DX dump."""
    if not clients_ready():
        return []
    e164 = normalize_phone(q)
    raw = (q or "").strip()
    if e164:
        hit = clients_db.get_by_phone(e164)
        rows = [hit] if hit else clients_db.search(raw, limit)
    else:
        rows = clients_db.search(raw, limit)
    out = []
    for row in rows:
        view = _staff_view(row)
        out.append(
            {
                "id": view["id"],
                "name": view["name"],
                "preferred_name": view["preferred_name"],
                "email": view["email"],
                "phone": view["phone"],
                "dob": view["dob"],
                "status": view["status"],
                "card_on_file": view["card_on_file"],
                "card_brand": view["card_brand"],
                "card_last4": view["card_last4"],
            }
        )
    return out[:limit]


def create_client(*, name: str, email: str, phone: str = "") -> dict[str, Any]:
    """Staff add — directory only, no notes/DX."""
    if not clients_ready():
        raise RuntimeError("clients db unset")
    name = (name or "").strip()
    email = (email or "").strip().lower()
    if not name or not EMAIL_RE.match(email):
        raise RuntimeError("need name and a valid email")
    existing = clients_db.get_by_email(email)
    if existing:
        row = existing
    else:
        row = clients_db.create(
            name=name, email=email, phone=normalize_phone(phone) or "", source="desk"
        )
    return {
        "ok": True,
        "id": row.get("id"),
        "name": row.get("display_name") or name,
        "email": row.get("email") or email,
        "phone": row.get("phone_e164") or "",
    }


def client_by_id(client_id: str) -> dict[str, Any] | None:
    if not clients_ready() or not client_id:
        return None
    row = clients_db.get_by_id(client_id)
    return _staff_view(row) if row else None


def client_by_email(email: str) -> dict[str, Any] | None:
    em = (email or "").strip().lower()
    if not clients_ready() or not em or not EMAIL_RE.match(em):
        return None
    row = clients_db.get_by_email(em)
    return _staff_view(row) if row else None


def record_card_on_file(
    *,
    email: str = "",
    customer_id: str = "",
    payment_method_id: str = "",
    brand: str = "",
    last4: str = "",
    exp_month: int | None = None,
    exp_year: int | None = None,
) -> dict[str, Any]:
    """Stripe pointers only. Never PAN. No new row if nobody matches."""
    if not clients_ready():
        return {"ok": False, "detail": "clients db unset"}
    row = clients_db.get_by_email(email) if email else None
    if not row and customer_id:
        row = clients_db.get_by_stripe_customer(customer_id)
    if not row:
        return {"ok": True, "detail": "stripe-only"}
    clients_db.upsert(
        {
            "id": row["id"],
            "stripe_customer_id": customer_id or row.get("stripe_customer_id"),
            "stripe_payment_method_id": payment_method_id,
            "stripe_card_brand": brand,
            "stripe_card_last4": last4,
            "stripe_card_exp_month": exp_month,
            "stripe_card_exp_year": exp_year,
            "stripe_card_updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "card_on_file": True,
        }
    )
    return {"ok": True}


def verify_patient(
    *,
    email: str = "",
    phone: str = "",
    full_name: str = "",
    first_name: str = "",
    last_name: str = "",
    dob: str = "",
) -> dict[str, Any]:
    """Current-patient check.

    Enough: matching email, OR matching phone, OR full name + DOB.
    Never leak whether the email exists if the other half is wrong.
    """
    if not clients_ready():
        return {"ok": False, "reason": "verify_unavailable"}

    em = (email or "").strip().lower()
    if em and EMAIL_RE.match(em):
        row = clients_db.get_by_email(em)
        if row:
            return _public_row(row, "email")
        return {"ok": False, "reason": "not_found"}

    e164 = normalize_phone(phone) if phone else None
    if e164:
        row = clients_db.get_by_phone(e164)
        if row:
            return _public_row(row, "phone")
        return {"ok": False, "reason": "not_found"}

    dob_iso = parse_dob(dob)
    name = normalize_name(full_name)
    if not name and (first_name or last_name):
        name = normalize_name(f"{first_name} {last_name}")
    parts = [p for p in name.split() if p]
    if dob_iso and len(parts) >= 2:
        rows = clients_db.list_by_dob(dob_iso, limit=20)
        hits = []
        for row in rows:
            given = normalize_name(row.get("given_name") or "")
            family = normalize_name(row.get("family_name") or "")
            display = normalize_name(row.get("display_name") or "")
            pref = normalize_name(row.get("preferred_name") or "")
            first, last = parts[0], parts[-1]
            if family == last and (given == first or pref == first or display.startswith(first)):
                hits.append(row)
            elif display == name or (pref and pref == first and family == last):
                hits.append(row)
        if len(hits) == 1:
            return _public_row(hits[0], "name_dob")
        return {"ok": False, "reason": "not_found"}

    return {"ok": False, "reason": "need_identifier"}
