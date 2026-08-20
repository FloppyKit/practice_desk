"""Week-calendar people for desk search. Pointers only — no note bodies."""
from __future__ import annotations

import re
from typing import Any

SKIP_TITLES = re.compile(
    r"^(busy|blocked|hold|ooo|out of office|lunch|break)$", re.I
)


def _norm_name(raw: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (raw or "").lower()).strip()


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def person_from_visit(v: dict[str, Any]) -> dict[str, Any] | None:
    """One searchable person from a week event. Skips holds / busy blocks."""
    email = (v.get("email") or "").strip()
    name = (v.get("name") or "").strip()
    title = (v.get("title") or "").strip()
    phone = (v.get("phone") or "").strip()
    who = name
    if not who and title and not SKIP_TITLES.match(title):
        who = title
    if not email and (not who or SKIP_TITLES.match(who)):
        return None
    return {
        "id": v.get("client_id") or "",
        "name": who,
        "preferred_name": "",
        "email": email,
        "phone": phone,
        "dob": "",
        "status": "this-week",
        "card_on_file": False,
        "card_brand": "",
        "card_last4": "",
        "source": "week",
    }


def match_person(p: dict[str, Any], q: str) -> bool:
    raw = (q or "").strip()
    if not raw:
        return True
    blob = " ".join(
        [
            str(p.get("name") or ""),
            str(p.get("email") or ""),
            str(p.get("phone") or ""),
        ]
    ).lower()
    if raw.lower() in blob:
        return True
    qd = _digits(raw)
    if len(qd) >= 4 and qd in _digits(str(p.get("phone") or "")):
        return True
    return False


def people_from_visits(
    visits: list[dict[str, Any]], q: str, limit: int = 12
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for v in visits or []:
        p = person_from_visit(v)
        if not p:
            continue
        key = (p.get("email") or "").strip().lower() or _norm_name(p.get("name") or "")
        if not key or key in seen:
            continue
        if not match_person(p, q):
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def merge_client_hits(
    book: list[dict[str, Any]],
    week: list[dict[str, Any]],
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Directory rows first, then calendar-only people. Dedupe email/name."""
    out: list[dict[str, Any]] = []
    seen_email: set[str] = set()
    seen_name: set[str] = set()
    for row in list(book or []) + list(week or []):
        email = (row.get("email") or "").strip().lower()
        name = _norm_name(row.get("name") or "")
        if email and email in seen_email:
            continue
        if not email and name and name in seen_name:
            continue
        if email:
            seen_email.add(email)
        if name:
            seen_name.add(name)
        out.append(row)
        if len(out) >= limit:
            break
    return out
