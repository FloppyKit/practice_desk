"""Client copies of signed intake documents: which PDFs to list and when a (re)send is due.

The link itself is a billing.mint_share(kind="docs") row (30 days, revocable, own HMAC domain).
No form answers are stored or logged here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import billing, clients_db, onboarding

LABELS = {
    "card-auth": "Card-on-file authorization",
    "personal": "Personal information",
    "consents": "Consents & practice policies (informed consent, telehealth, policies, privacy acknowledgment)",
    "roi": "Authorization to release information",
}
NPP_URL = "https://site.psycharts.org/forms/handouts/privacy-notice.pdf"


def docs_for(client_id: str) -> list[dict[str, Any]]:
    if not client_id:
        return []
    with onboarding._con() as con:
        rows = con.execute(
            "SELECT id, form, received_at, pdf_path FROM intake_submissions WHERE client_id = ?"
            " AND pdf_status = 'saved' AND COALESCE(pdf_path, '') != '' ORDER BY received_at",
            (client_id,),
        ).fetchall()
    return [{"id": r["id"], "form": r["form"], "received_at": r["received_at"], "path": r["pdf_path"],
             "label": LABELS.get(r["form"], r["form"])} for r in rows]


def doc(client_id: str, sub_id: str) -> dict[str, Any] | None:
    return next((d for d in docs_for(client_id) if d["id"] == sub_id), None)


def last_sent(client_id: str) -> dict[str, Any] | None:
    with clients_db.connect() as con:
        billing._ensure(con)
        r = con.execute(
            "SELECT id, path, created_at, expires_at, hits, revoked_at FROM share_links WHERE billing_id = ?"
            " AND source = 'docs' ORDER BY created_at DESC LIMIT 1",
            (client_id,),
        ).fetchone()
    return {k: r[k] for k in r.keys()} if r else None


def pending(client_id: str) -> bool:
    with onboarding._con() as con:
        r = con.execute("SELECT 1 FROM intake_submissions WHERE client_id = ? AND pdf_status = 'pending' LIMIT 1",
                        (client_id,)).fetchone()
    return r is not None


def due(client_id: str, complete: bool) -> bool:
    """Send once intake is complete and every PDF has been filed; send again when a document signed later
    (e.g. release of info) is not in the last send. share_links.path holds the submission ids sent."""
    if not complete or pending(client_id):
        return False
    docs = docs_for(client_id)
    if not docs:
        return False
    last = last_sent(client_id)
    if not last:
        return True
    sent = set(filter(None, str(last.get("path") or "").split(",")))
    return any(d["id"] not in sent for d in docs)


def summary(client_id: str) -> dict[str, Any] | None:
    last = last_sent(client_id) if client_id else None
    if not last:
        return None
    exp = int(last.get("expires_at") or 0)
    return {
        "sent_at": last.get("created_at") or "",
        "expires_iso": datetime.fromtimestamp(exp, timezone.utc).isoformat() if exp else "",
        "opened": int(last.get("hits") or 0),
        "revoked": bool(last.get("revoked_at")),
    }
