"""Stripe off-session charge. No PAN. Description is date only."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()


def stripe_ready() -> bool:
    return bool(STRIPE_SECRET_KEY.startswith("sk_"))


def _post(path: str, form: dict[str, str], *, idem: str = "") -> dict[str, Any]:
    if not stripe_ready():
        raise RuntimeError("Stripe is not configured on the booker")
    data = urllib.parse.urlencode(form).encode()
    headers = {
        "Authorization": "Bearer " + STRIPE_SECRET_KEY,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if idem:
        headers["Idempotency-Key"] = idem[:80]
    req = urllib.request.Request(
        "https://api.stripe.com/v1" + path,
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(raw)
            msg = str(((err.get("error") or {}).get("message")) or raw[:160])
        except Exception:
            msg = raw[:160] or f"stripe {e.code}"
        raise RuntimeError(msg) from e


def _get(path: str) -> dict[str, Any]:
    if not stripe_ready():
        raise RuntimeError("Stripe is not configured on the booker")
    req = urllib.request.Request(
        "https://api.stripe.com/v1" + path,
        headers={"Authorization": "Bearer " + STRIPE_SECRET_KEY},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")[:160]
        raise RuntimeError(raw or f"stripe {e.code}") from e


def ensure_customer(*, email: str, existing_id: str = "", client_id: str = "") -> str:
    if existing_id and existing_id.startswith("cus_"):
        return existing_id
    em = (email or "").strip().lower()
    if not em:
        raise RuntimeError("need an email to invoice")
    found = _get("/customers?email=" + urllib.parse.quote(em) + "&limit=1")
    data = found.get("data") or []
    if data:
        return str(data[0].get("id") or "")
    created = _post(
        "/customers",
        {"email": em, "metadata[client_id]": (client_id or "")[:80]},
    )
    cid = str(created.get("id") or "")
    if not cid.startswith("cus_"):
        raise RuntimeError("could not create Stripe customer")
    return cid


def create_invoice(
    *,
    customer_id: str,
    lines: list[dict[str, Any]],
    client_id: str = "",
) -> dict[str, Any]:
    if not customer_id:
        raise RuntimeError("no Stripe customer")
    if not lines:
        raise RuntimeError("nothing to invoice")
    ids: list[str] = []
    total = 0
    for i, ln in enumerate(lines):
        cents = int(ln.get("amount_cents") or 0)
        if cents <= 0:
            continue
        dos = str(ln.get("dos") or "")[:10]
        kind = str(ln.get("kind") or "visit")
        if kind == "no_show":
            desc = f"No-show {dos}" if dos else "No-show"
        else:
            desc = f"Visit {dos}" if dos else "Visit"
        item = _post(
            "/invoiceitems",
            {
                "customer": customer_id,
                "amount": str(cents),
                "currency": "usd",
                "description": desc,
                "metadata[fee_id]": str(ln.get("id") or ""),
                "metadata[dos]": dos,
            },
            idem=f"ii-{(ln.get('id') or i)}",
        )
        ids.append(str(item.get("id") or ""))
        total += cents
    if not ids:
        raise RuntimeError("no invoice lines")
    inv = _post(
        "/invoices",
        {
            "customer": customer_id,
            "collection_method": "send_invoice",
            "days_until_due": "14",
            "auto_advance": "true",
            "metadata[client_id]": (client_id or "")[:80],
            "metadata[fee_ids]": ",".join(str(ln.get("id") or "") for ln in lines)[:500],
        },
    )
    iid = str(inv.get("id") or "")
    if not iid.startswith("in_"):
        raise RuntimeError("could not create invoice")
    final = _post(f"/invoices/{iid}/finalize", {})
    url = str(final.get("hosted_invoice_url") or inv.get("hosted_invoice_url") or "")
    return {
        "ok": True,
        "stripe_id": iid,
        "pay_url": url,
        "amount_cents": int(final.get("amount_due") or total),
        "status": str(final.get("status") or ""),
    }


def void_invoice(invoice_id: str) -> None:
    if not (invoice_id or "").startswith("in_"):
        return
    try:
        _post(f"/invoices/{invoice_id}/void", {})
    except RuntimeError:
        pass


def verify_webhook(payload: bytes, header: str) -> dict[str, Any]:
    if not STRIPE_WEBHOOK_SECRET:
        raise RuntimeError("Stripe webhook secret unset")
    parts = {}
    for bit in (header or "").split(","):
        if "=" in bit:
            k, v = bit.split("=", 1)
            parts.setdefault(k.strip(), []).append(v.strip())
    ts = (parts.get("t") or [""])[0]
    sigs = parts.get("v1") or []
    if not ts or not sigs:
        raise RuntimeError("bad stripe signature")
    if abs(int(time.time()) - int(ts)) > 300:
        raise RuntimeError("stale stripe signature")
    signed = ts.encode() + b"." + payload
    expect = hmac.new(STRIPE_WEBHOOK_SECRET.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expect, s) for s in sigs):
        raise RuntimeError("stripe signature mismatch")
    return json.loads(payload.decode() or "{}")


def charge_off_session(
    *,
    customer_id: str,
    payment_method_id: str,
    amount_cents: int,
    dos: str,
    fee_id: str,
) -> dict[str, Any]:
    if not customer_id or not payment_method_id:
        raise RuntimeError("no card on file")
    cents = int(amount_cents)
    if cents <= 0:
        raise RuntimeError("bad amount")
    day = (dos or "")[:10]
    data = _post(
        "/payment_intents",
        {
            "amount": str(cents),
            "currency": "usd",
            "customer": customer_id,
            "payment_method": payment_method_id,
            "off_session": "true",
            "confirm": "true",
            "description": f"Visit {day}" if day else "Visit",
            "metadata[fee_id]": fee_id,
            "metadata[dos]": day,
        },
        idem="fee-" + fee_id,
    )
    status = str(data.get("status") or "")
    sid = str(data.get("id") or "")
    if status == "succeeded":
        return {"ok": True, "status": "paid", "stripe_id": sid}
    if status == "requires_action":
        return {
            "ok": False,
            "status": "failed",
            "stripe_id": sid,
            "error": "Card needs authentication — send the pay link instead.",
        }
    return {
        "ok": False,
        "status": "failed",
        "stripe_id": sid,
        "error": status or "charge did not complete",
    }
