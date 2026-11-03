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

    invoice_status = "open"

    def get(self, path: str) -> dict:
        if path.startswith("/invoices/"):
            return {"id": path.rsplit("/", 1)[1], "status": self.invoice_status, "amount_due": 26500,
                    "hosted_invoice_url": "https://invoice.stripe.com/i/unit"}
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


def test_branded_pay_link_redirects_closes_and_revokes() -> None:
    with _desk(letterhead=True, live=True) as (main, fake, drive):
        out = main.api_desk_bill_confirm(_req(main, dry_run=False, send_email=False, share_superbill=True), "")
        url = out["payment"]["pay_url"]
        assert url.startswith(main.PUBLIC_BASE + "/pay/") and out["billing"]["pay_url"] == url
        assert out["billing"]["pay_link"] and out["billing"]["share"]
        token = url.rsplit("/", 1)[1]
        r = main.public_pay_link(token)
        assert r.status_code == 302 and r.headers["location"] == "https://invoice.stripe.com/i/unit"
        assert r.headers["cache-control"] == "no-store" and r.headers["x-robots-tag"] == "noindex"
        assert main.public_pay_link(token[:-2] + "xx").status_code == 404
        assert main.public_pay_link("garbage").status_code == 404
        sb_token = out["share"]["url"].rsplit("/", 1)[1]
        assert main.public_pay_link(sb_token).status_code == 404  # superbill tokens are not pay tokens
        assert main.public_superbill_share(token).status_code == 404  # and vice versa
        fake.invoice_status = "void"
        closed = main.public_pay_link(token)
        assert closed.status_code == 200 and b"closed" in closed.body and b"Unit Practice" in closed.body
        fake.invoice_status = "paid"
        assert b"paid" in main.public_pay_link(token).body
        fake.invoice_status = "open"
        bid = out["billing"]["id"]
        main.api_desk_bill_share_revoke(main.BillShareRequest(billing_id=bid, pay_link=True), "")
        assert main.public_pay_link(token).status_code == 410
        assert main.public_superbill_share(sb_token).status_code == 200  # superbill link untouched
        days = (out["billing"]["pay_link"]["expires_iso"])
        from datetime import datetime, timezone

        assert 29.9 < (datetime.fromisoformat(days) - datetime.now(timezone.utc)).total_seconds() / 86400 <= 30.0


def test_pay_thanks_route_still_wins() -> None:
    from app import main

    paths = [getattr(r, "path", "") for r in main.app.routes]
    assert paths.index("/pay/thanks") < paths.index("/pay/{token}")


def test_letterhead_licenses_stay_on_one_line() -> None:
    from app.superbill_pdf import superbill_pdf

    lic = ["KS TW-00062", "IL 036.132941", "MO 2025054356"]
    pdf = superbill_pdf({"letterhead": {"name": "Dr Matt Brown", "address": "332 South Michigan Avenue STE 121 #5386",
                                        "licenses": lic}, "lines": []})
    one = "Licenses: " + " \u00b7 ".join(lic)
    assert ("(" + one + ")").encode("latin-1") in pdf


# ---- Superbill v2: pick visits / fees / imported visits ----------------------------------------


def _seed_v2():
    import uuid as _u

    from app import clients_db, fees

    with clients_db.connect() as con:
        fees._ensure(con)
        con.execute(
            "CREATE TABLE IF NOT EXISTS imported_appointments (id TEXT PRIMARY KEY, client_id TEXT, "
            "carepatron_contact_id TEXT, carepatron_appointment_id TEXT, start_at TEXT, end_at TEXT, tz TEXT, "
            "start_utc TEXT, end_utc TEXT, service TEXT, status TEXT, duration_min INTEGER, lines_json TEXT, "
            "raw_json TEXT, imported_at TEXT)")
        paid_lines = json.dumps([{"service": "Office Visit", "code": "99213 - 95", "price": "220", "invoice": "000274 - Paid"}])
        imp = [
            ("imp-old", "2025-01-07 09:00:00", "2025-01-07T15:00:00+00:00", "60 min Office Visit", "Confirmed", 60, paid_lines),
            ("imp-dup", "2026-10-20 10:00:00", "2026-10-20T15:00:00+00:00", "30 Min Visit", "Confirmed", 30, "[]"),
            ("imp-cx", "2025-02-01 09:00:00", "2025-02-01T15:00:00+00:00", "30 Min Visit", "Cancelled", 30, "[]"),
            ("imp-np", "2024-12-01 09:00:00", "2024-12-01T15:00:00+00:00", "Initial Consultation", "Confirmed", 90, "[]"),
        ]
        for i in imp:
            con.execute("INSERT INTO imported_appointments (id, client_id, start_at, start_utc, service, status, "
                        "duration_min, lines_json) VALUES (?,?,?,?,?,?,?,?)", (i[0], "c-unit", *i[1:]))
        for fid, kind, status, ev in (("fee-ns", "no_show", "owed", ""), ("fee-w", "other", "waived", ""),
                                      ("fee-v1", "visit", "paid", "ev-1")):
            con.execute("INSERT INTO fees (id, client_id, dos, kind, amount_cents, status, event_id) VALUES (?,?,?,?,?,?,?)",
                        (fid, "c-unit", "2026-09-01", kind, 10000, status, ev))
        con.commit()
    return [
        {"event_id": "ev-1", "start_iso": "2026-10-20T10:00:00-05:00", "event_type": "follow-up-30",
         "duration_minutes": 30, "upcoming": False},
        {"event_id": "ev-2", "start_iso": "2026-10-27T10:00:00-05:00", "event_type": "follow-up-60",
         "duration_minutes": 60, "upcoming": False},
        {"event_id": "ev-up", "start_iso": "2026-12-01T10:00:00-06:00", "event_type": "follow-up-30",
         "duration_minutes": 30, "upcoming": True},
        {"event_id": "ev-intro", "start_iso": "2026-10-01T10:00:00-05:00", "event_type": "intro-call",
         "duration_minutes": 15, "upcoming": False},
    ]


def _v2_post(main, keys, lines, **kw):
    req = main.DeskSuperbillRequest(client_id="c-unit", dos="2026-10-20", diagnoses=[{"code": "F41.1", "label": "GAD"}],
                                    lines=lines, items=keys, **kw)
    return main.api_desk_superbill(req)


def test_superbill_v2_list_split_imported_dedupe_and_fees() -> None:
    from app import superbills

    with _desk(letterhead=True) as (main, _fake, _drive):
        cal = _seed_v2()
        out = superbills.build_list("c-unit", cal, now_utc="2026-11-03T15:00:00+00:00")
        keys = [i["key"] for i in out["unlinked"]]
        assert out["linked"] == []
        assert "ev:ev-1" in keys and "ev:ev-2" in keys
        assert "ev:ev-up" not in keys and "ev:ev-intro" not in keys      # upcoming and intro calls are not billable rows
        assert "imp:imp-dup" not in keys                                 # same slot as calendar ev-1
        assert "imp:imp-cx" not in keys                                  # cancelled
        assert "imp:imp-old" in keys and "imp:imp-np" in keys
        assert "fee:fee-ns" in keys and "fee:fee-w" not in keys and "fee:fee-v1" not in keys
        by = {i["key"]: i for i in out["unlinked"]}
        assert [ln["code"] for ln in by["ev:ev-1"]["lines"]] == ["99213", "90833"]
        assert by["ev:ev-1"]["paid"] and by["ev:ev-1"]["lines"][0]["paid"] == "220.00"   # paid via fee ledger
        assert not by["ev:ev-2"]["paid"] and by["ev:ev-2"]["lines"][1]["code"] == "90838"
        assert by["imp:imp-old"]["paid"] and by["imp:imp-old"]["visit_code"] == "fu60"
        assert by["imp:imp-np"]["visit_code"] == "np" and by["imp:imp-np"]["fee_cents"] == 60000
        assert by["fee:fee-ns"]["optional"] and by["fee:fee-ns"]["lines"][0]["cost"] == "100.00"
        assert all(ln["dos"] == "2025-01-07" for ln in by["imp:imp-old"]["lines"])
        ranged = superbills.build_list("c-unit", cal, start="2026-10-01", end="2026-10-31",
                                       now_utc="2026-11-03T15:00:00+00:00")
        assert {i["key"] for i in ranged["unlinked"]} == {"ev:ev-1", "ev:ev-2"}


def test_superbill_v2_save_links_readd_and_unique_names() -> None:
    from app import superbills

    with _desk(letterhead=True) as (main, _fake, drive):
        cal = _seed_v2()
        ln1 = {"item": "ev:ev-1", "dos": "2026-10-20", "service": "Level 3", "code": "99213", "cost": "220.00", "paid": "220.00"}
        ln2 = {"item": "imp:imp-old", "dos": "2025-01-07", "service": "Level 3", "code": "99213", "cost": "220.00"}
        a = _v2_post(main, ["ev:ev-1", "imp:imp-old"], [ln1, ln2],
                     custom_fields=[{"label": "Referral", "value": "Self"}])
        assert a["ok"] and a["linked"] == 2 and a["name"].startswith("Superbill #")
        assert "1-7-2025 to 10-20-2026" in a["name"]
        out = superbills.build_list("c-unit", cal, now_utc="2026-11-03T15:00:00+00:00")
        linked = {i["key"]: i for i in out["linked"]}
        assert set(linked) == {"ev:ev-1", "imp:imp-old"} and linked["ev:ev-1"]["statements"] == [a["statement"]]
        assert "ev:ev-1" not in {i["key"] for i in out["unlinked"]}
        # Re-add an already-linked visit to a new superbill: new file, old one untouched.
        old_pdf = drive[a["path"]]
        b = _v2_post(main, ["ev:ev-1"], [ln1])
        assert b["path"] != a["path"] and b["statement"] != a["statement"]
        assert drive[a["path"]] == old_pdf
        out = superbills.build_list("c-unit", cal, now_utc="2026-11-03T15:00:00+00:00")
        assert {i["key"]: i for i in out["linked"]}["ev:ev-1"]["statements"] == [a["statement"], b["statement"]]
        assert superbills.count("c-unit") == 2
        assert b"Notes" in old_pdf and b"Referral: Self" in old_pdf
        # Bad keys are ignored, not stored.
        c = _v2_post(main, ["../etc", "ev:ok-1"], [dict(ln1, item="ev:ok-1")])
        assert c["linked"] == 1


def test_superbill_v2_filename_and_pages() -> None:
    from app.superbill_pdf import superbill_pdf, superbill_v2_filename

    assert superbill_v2_filename("001800", ["2026-10-20"]) == "Superbill #001800 10-20-2026.pdf"
    assert superbill_v2_filename("001801", ["2026-10-27", "2026-01-02"]) == "Superbill #001801 1-2-2026 to 10-27-2026.pdf"
    assert superbill_v2_filename("001800", ["2026-10-20"]) != superbill_v2_filename("001801", ["2026-10-20"])
    lines = [{"dos": "2026-10-20", "service": "Level 3 office visit", "code": "99213", "cost": "220", "paid": "0"}] * 60
    pdf = superbill_pdf({"statement": "1", "lines": lines, "custom_fields": [{"label": "Note", "value": "x"}]})
    assert b"/Count 1 " not in pdf and b"continued" in pdf and b"Note: x" in pdf
    one = superbill_pdf({"statement": "1", "lines": lines[:2]})
    assert b"/Count 1" in one and b"Notes" not in one


def test_superbill_v2_sheet_wiring() -> None:
    html = (ROOT / "static" / "desk.html").read_text(encoding="utf-8")
    for needle in ('id="sb-visits"', 'id="sb-linked-wrap"', "<details", 'id="sb-field-add"', "sb-ldos",
                   "/api/desk/superbill/visits", "custom_fields", "also on #"):
        assert needle in html, needle
