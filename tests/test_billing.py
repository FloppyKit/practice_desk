"""One-click billing: codes, mapping, letterhead gate, bill-once, no charge without confirm, share links.
Fake Stripe and fake Drive. No network, no sends."""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
_ENV = ("BOOKER_LOG_DIR", "CANCEL_SECRET_FILE", "BILLING_LIVE", "BILLING_SMS", "BILLING_CODES")


class FakeStripe:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, path: str, form: dict, *, idem: str = "") -> dict:
        self.posts.append(path)
        if path == "/payment_intents":
            return {"id": "pi_unit", "status": "succeeded"}
        if path == "/invoiceitems":
            return {"id": "ii_unit"}
        if path == "/invoices":
            return {"id": "in_unit"}
        if path.endswith("/finalize"):
            return {"id": "in_unit", "hosted_invoice_url": "https://invoice.example.invalid/x", "amount_due": 26500}
        raise AssertionError("unexpected Stripe POST " + path)

    def get(self, path: str) -> dict:
        return {"data": [{"id": "cus_unit"}]}


@contextmanager
def _desk(*, letterhead: bool = False, card: bool = False, live: bool = False):
    from app import clients_db, practice, stripe_pay
    from app import main

    saved_env = {k: os.environ.get(k) for k in _ENV}
    saved = {
        (clients_db, "DB_PATH"): clients_db.DB_PATH,
        (practice, "ROOT"): practice.ROOT,
        (stripe_pay, "STRIPE_SECRET_KEY"): stripe_pay.STRIPE_SECRET_KEY,
        (stripe_pay, "_post"): stripe_pay._post,
        (stripe_pay, "_get"): stripe_pay._get,
        (main, "_notify_post"): main._notify_post,
        (main, "_drive_mkdir"): main._drive_mkdir,
        (main, "_notify_get"): main._notify_get,
        (main, "_require_staff"): main._require_staff,
    }
    fake = FakeStripe()
    drive: dict[str, bytes] = {}
    try:
        with tempfile.TemporaryDirectory() as raw:
            sec = Path(raw) / "cancel.secret"
            sec.write_text("billing-test-secret")
            os.environ["BOOKER_LOG_DIR"] = raw
            os.environ["CANCEL_SECRET_FILE"] = str(sec)
            os.environ["BILLING_CODES"] = str(ROOT / "config" / "billing-codes.yaml")
            os.environ.pop("BILLING_SMS", None)
            if live:
                os.environ["BILLING_LIVE"] = "true"
            else:
                os.environ.pop("BILLING_LIVE", None)
            clients_db.DB_PATH = Path(raw) / "clients.sqlite"
            practice.ROOT = Path(raw) / "practice"
            fields = {"display_name": "Unit Practice", "legal_name": "Unit Practice LLC", "npi": "1234567890",
                      "address": "332 South Michigan Avenue STE 121 #5386"}
            if letterhead:
                fields.update(ein="00-0000000", licenses=[
                    {"state": "KS", "number": "KS-UNIT"}, {"state": "MO", "number": "MO-UNIT"},
                    {"state": "IL", "number": "IL-UNIT"}])
            practice.save(fields)
            stripe_pay.STRIPE_SECRET_KEY = "sk_unit_fake"
            stripe_pay._post = fake.post
            stripe_pay._get = fake.get

            def notify_post(path, payload):
                import base64
                drive[payload["path"]] = base64.b64decode(payload["b64"])
                return 200, {"ok": True}

            main._notify_post = notify_post
            main._drive_mkdir = lambda path: None
            main._notify_get = lambda path: (200, drive.get(path.split("path=", 1)[1].replace("%20", " "), b""), "application/pdf")
            main._require_staff = lambda *a, **k: None
            row = {"id": "c-unit", "display_name": "Unit Patient", "email": "unit@example.invalid",
                   "dob": "1990-01-01", "status": "active",
                   "given_name": "Unit", "family_name": "Patient",
                   "drive_folder_path": "/Clients/Patient, Unit/Chart",
                   "billing_folder_path": "/Clients/Patient, Unit/Billing"}
            if card:
                row.update(stripe_customer_id="cus_unit", stripe_payment_method_id="pm_unit", card_on_file=1)
            clients_db.upsert(row)
            yield main, fake, drive
    finally:
        for (mod, attr), val in saved.items():
            setattr(mod, attr, val)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _req(main, **kw):
    base = dict(client_id="c-unit", event_id="ev-1", dos="2026-10-20", visit_code="fu30",
                confirm_code="fu30", diagnoses=[{"code": "F41.1", "label": "GAD"}], payment="link")
    base.update(kw)
    return main.BillConfirmRequest(**base)


def _status(fn, *a) -> int:
    try:
        fn(*a)
    except HTTPException as e:
        return e.status_code
    raise AssertionError("expected HTTPException")


def test_cpt_fees_add_up_and_presets_match() -> None:
    from app.billing import load_codes
    from app.practice import service_set

    codes = load_codes(ROOT / "config" / "billing-codes.yaml")["visit_types"]
    assert {k: v["price_cents"] for k, v in codes.items()} == {"fu30": 26500, "fu60": 42000, "np": 60000}
    assert [v["invoice_code"] for v in codes.values()] == ["FU30", "FU60", "NP"]
    for v in codes.values():
        assert sum(x["fee_cents"] for x in v["lines"]) == v["price_cents"]
        assert all(x["modifier"] == "95" for x in v["lines"])
    presets = {p["id"]: p for p in service_set("psychiatrist")}
    for key, preset in (("fu30", "fu30"), ("fu60", "fu60"), ("np", "init90")):
        got = [(x["code"], int(float(x["cost"]) * 100)) for x in presets[preset]["lines"]]
        assert got == [(x["code"], x["fee_cents"]) for x in codes[key]["lines"]], key


def test_bad_fee_split_is_rejected() -> None:
    from app.billing import load_codes

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write('pos: "10"\nvisit_types:\n  x: {price: 100, cpt: [{code: "99213", mod: "95", fee: 90}]}\n')
    try:
        load_codes(Path(f.name))
    except ValueError:
        return
    finally:
        os.unlink(f.name)
    raise AssertionError("expected ValueError")


def test_visit_mapping() -> None:
    from app.billing import guess_code

    assert guess_code("new-client", 90) == "np"
    assert guess_code("follow-up", 30) == "fu30"
    assert guess_code("follow-up", 60) == "fu60"
    assert guess_code("", 30) == "fu30"


def test_confirm_required_and_refusal_without_letterhead() -> None:
    with _desk(letterhead=False) as (main, fake, drive):
        assert _status(main.api_desk_bill_confirm, _req(main, confirm_code=""), "") == 400
        out = main.api_desk_bill_confirm(_req(main), "")
        assert out["dry_run"] and out["invoice"]["invoice_code"] == "FU30"
        assert out["invoice"]["amount_cents"] == 26500
        sb = out["superbill"]
        assert sb["status"] == "refused"
        assert "tax ID (EIN)" in sb["missing"] and "KS license number" in sb["missing"]
        assert "practice address" not in sb["missing"]
        assert fake.posts == [] and drive == {}


def test_bill_once_and_superbill_contents() -> None:
    with _desk(letterhead=True) as (main, fake, drive):
        out = main.api_desk_bill_confirm(_req(main, visit_code="fu60", confirm_code="fu60"), "")
        assert out["created"] and out["superbill"]["status"] == "saved"
        again = main.api_desk_bill_confirm(_req(main, visit_code="fu60", confirm_code="fu60"), "")
        assert again["created"] is False
        from app import billing

        pdf = (billing.dryrun_dir() / out["superbill"]["path"]).read_bytes()
        for needle in (b"99213 - 95", b"90838 - 95", b"POS", b"(10)", b"NPI: 1234567890",
                       b"Tax ID \\(EIN\\): 00-0000000", b"332 South Michigan Avenue STE 121 #5386"):
            assert needle in pdf, needle
        assert b"SPECIMEN" not in pdf
        assert fake.posts == [] and drive == {}


def test_no_charge_without_card_choice_and_live_link_path() -> None:
    with _desk(letterhead=True, card=True, live=True) as (main, fake, drive):
        assert _status(main.api_desk_bill_confirm, _req(main, payment="card", dry_run=False), "") == 400
        assert "/payment_intents" not in fake.posts
        out = main.api_desk_bill_confirm(_req(main, dry_run=False, send_email=False), "")
        assert out["payment"]["charged"] is False and "/payment_intents" not in fake.posts
        assert out["payment"]["pay_url"].startswith("https://")
        assert list(drive) == ["/Clients/Patient, Unit/Billing/Superbill 10-20-2026.pdf"]
        out2 = main.api_desk_bill_confirm(
            _req(main, event_id="ev-2", payment="card", confirm_charge=True, dry_run=False, send_email=False), "")
        assert out2["payment"]["charged"] is True and fake.posts.count("/payment_intents") == 1


def test_dry_run_forced_when_not_live() -> None:
    with _desk(letterhead=True, card=True, live=False) as (main, fake, drive):
        out = main.api_desk_bill_confirm(_req(main, payment="card", confirm_charge=True, dry_run=False), "")
        assert out["dry_run"] and out["forced_dry_run"] and fake.posts == []


def test_share_link_works_expires_rotates_and_revokes() -> None:
    with _desk(letterhead=False) as (main, fake, drive):
        out = main.api_desk_bill_confirm(_req(main, specimen=True, share_ttl_seconds=1), "")
        assert out["superbill"]["specimen"] is True
        token = out["share"]["url"].rsplit("/", 1)[1]
        r = main.public_superbill_share(token)
        assert r.status_code == 200 and r.body.startswith(b"%PDF")
        assert main.public_superbill_share(token[:-2] + "xx").status_code == 404
        time.sleep(2.1)
        assert main.public_superbill_share(token).status_code == 410
        bid = out["billing"]["id"]
        rot = main.api_desk_bill_share(main.BillShareRequest(billing_id=bid, send_email=False), "")
        t2 = rot["url"].rsplit("/", 1)[1]
        assert main.public_superbill_share(t2).status_code == 200
        exp = rot["expires_iso"]
        from datetime import datetime, timezone

        days = (datetime.fromisoformat(exp) - datetime.now(timezone.utc)).total_seconds() / 86400
        assert 29.9 < days <= 30.0
        main.api_desk_bill_share_revoke(main.BillShareRequest(billing_id=bid), "")
        assert main.public_superbill_share(t2).status_code == 410


def test_standing_dx_defaults_preview() -> None:
    with _desk() as (main, fake, drive):
        main.api_desk_bill_confirm(_req(main, save_dx=True, diagnoses=[{"code": "f42.2", "label": "OCD"}]), "")
        prev = main.api_desk_bill_preview("", "c-unit", "ev-9", "2026-10-21", "follow-up", 60)
        assert prev["standing_dx"] == [{"code": "F42.2", "label": "OCD"}]
        assert prev["guess"] == "fu60" and prev["sms"] is False


def test_billing_sms_uses_spruce_only_and_dry_run_sends_nothing() -> None:
    from app import spruce_api

    saved = {k: os.environ.get(k) for k in ("SPRUCE_API_TOKEN", "SPRUCE_PHONE_ENDPOINT_ID", "ENABLE_SMS")}
    sent: list[tuple] = []
    orig = spruce_api.send_spruce_sms
    try:
        os.environ.update(SPRUCE_API_TOKEN="unit-token", SPRUCE_PHONE_ENDPOINT_ID="unit-ep", ENABLE_SMS="false")
        spruce_api.send_spruce_sms = lambda *a, **k: sent.append(a) or {"ok": True, "sent": True}
        with _desk(letterhead=True) as (main, fake, drive):
            from app import clients_db

            clients_db.upsert({"id": "c-unit", "phone_e164": "+15555550123"})
            os.environ["BILLING_SMS"] = "true"
            assert main.api_desk_bill_preview("", "c-unit", "ev-s", "2026-10-20", "follow-up", 30)["sms"] is True
            out = main.api_desk_bill_confirm(_req(main, event_id="ev-s", send_sms=True), "")
            sms = out["payment"]["notify"]["sms"]
            assert sms["dry_run"] and sms["via"] == "spruce_api" and "$265.00" in sms["text"]
            assert out["share"]["sms"]["via"] == "spruce_api" and sent == []
    finally:
        spruce_api.send_spruce_sms = orig
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_specimen_can_be_rebuilt_as_real_superbill() -> None:
    with _desk(letterhead=True) as (main, fake, drive):
        from app import practice

        prac_store = practice.ROOT / "practice.json"
        data = json.loads(prac_store.read_text())
        data["ein"] = ""
        prac_store.write_text(json.dumps(data))
        out = main.api_desk_bill_confirm(_req(main, specimen=True), "")
        assert out["superbill"]["specimen"] is True
        practice.save({"ein": "00-0000000"})
        again = main.api_desk_bill_superbill(main.BillSuperbillRequest(billing_id=out["billing"]["id"], send_email=False), "")
        assert again["superbill"]["status"] == "saved" and again["superbill"]["specimen"] is False
        assert drive == {} and fake.posts == []
