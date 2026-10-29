"""Patient portal, balance checkout, and verify routes. Fake Stripe, no network."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException, Request

ROOT = Path(__file__).resolve().parents[1]
_ENV_KEYS = ("BOOKER_LOG_DIR", "VERIFY_CODE_SECRET", "CANCEL_SECRET_FILE")


class FakeStripe:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str], str]] = []
        self.gets: list[str] = []
        self.sessions: dict[str, dict] = {}

    def post(self, path: str, form: dict[str, str], *, idem: str = "") -> dict:
        self.posts.append((path, dict(form), idem))
        if path == "/customers":
            return {"id": "cus_unit1"}
        if path.startswith("/customers/"):
            return {"id": path.rsplit("/", 1)[-1]}
        if path == "/checkout/sessions":
            sid = f"cs_unit_{len(self.sessions) + 1}"
            self.sessions[sid] = {
                "id": sid,
                "url": f"https://checkout.example.invalid/{sid}",
                "status": "open",
                "payment_status": "unpaid",
                "customer": form.get("customer", ""),
                "metadata": {
                    "client_id": form.get("metadata[client_id]", ""),
                    "fee_ids": form.get("metadata[fee_ids]", ""),
                },
            }
            return dict(self.sessions[sid])
        if path.startswith("/invoices/") and path.endswith("/void"):
            return {"status": "void"}
        raise AssertionError("unexpected Stripe POST " + path)

    def get(self, path: str) -> dict:
        self.gets.append(path)
        if path.startswith("/customers?email="):
            return {"data": []}
        if path.startswith("/checkout/sessions/"):
            sid = path[len("/checkout/sessions/"):].split("?", 1)[0]
            sess = dict(self.sessions[sid])
            if "expand[]=payment_intent" in path:
                sess["payment_intent"] = {"id": "pi_unit", "payment_method": "pm_unit"}
            return sess
        raise AssertionError("unexpected Stripe GET " + path)

    def pay(self, sid: str) -> None:
        self.sessions[sid].update(status="complete", payment_status="paid")


@contextmanager
def _portal():
    from app import clients_db, config, stripe_pay, verify_codes
    from app import main

    saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    saved = {
        (clients_db, "DB_PATH"): clients_db.DB_PATH,
        (config, "CONFIG_PATH"): config.CONFIG_PATH,
        (stripe_pay, "STRIPE_SECRET_KEY"): stripe_pay.STRIPE_SECRET_KEY,
        (stripe_pay, "STRIPE_WEBHOOK_SECRET"): stripe_pay.STRIPE_WEBHOOK_SECRET,
        (stripe_pay, "_post"): stripe_pay._post,
        (stripe_pay, "_get"): stripe_pay._get,
        (verify_codes, "_default_sender"): verify_codes._default_sender,
        (main, "_visits_for_client"): main._visits_for_client,
        (main, "delete_event"): main.delete_event,
        (main, "generate_slots"): main.generate_slots,
    }
    fake = FakeStripe()
    try:
        with tempfile.TemporaryDirectory() as raw:
            secret = Path(raw) / "cancel.secret"
            secret.write_text("portal-test-secret")
            os.environ["BOOKER_LOG_DIR"] = raw
            os.environ["VERIFY_CODE_SECRET"] = "portal-test"
            os.environ["CANCEL_SECRET_FILE"] = str(secret)
            clients_db.DB_PATH = Path(raw) / "clients.sqlite"
            config.CONFIG_PATH = ROOT / "config" / "event-types.example.yaml"
            stripe_pay.STRIPE_SECRET_KEY = "sk_unit_fake"
            stripe_pay.STRIPE_WEBHOOK_SECRET = "whsec_unit"
            stripe_pay._post = fake.post
            stripe_pay._get = fake.get
            clients_db.upsert(
                {
                    "id": "c-jane",
                    "display_name": "Jane Demo",
                    "email": "jane@example.com",
                    "phone_e164": "+15555550100",
                    "status": "active",
                    "drive_folder_url": "https://drive.proton.me/urls/JANE#chart",
                    "drive_folder_short_url": "https://go.psycharts.org/jane",
                    "billing_folder_url": "https://drive.proton.me/urls/JANE#bill",
                }
            )
            yield main, fake
    finally:
        for (mod, attr), val in saved.items():
            setattr(mod, attr, val)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _token(client_id: str = "c-jane") -> str:
    from app.tokens import mint_verify_token

    return mint_verify_token(client_id=client_id, email="jane@example.com", name="Jane", matched="email")


def _status(fn, *args) -> int:
    try:
        fn(*args)
    except HTTPException as e:
        return e.status_code
    raise AssertionError("expected HTTPException")


def test_portal_routes_reject_bad_or_unknown_token() -> None:
    with _portal() as (main, _fake):
        bad = main.PublicTokenRequest(verify_token="nope.nope")
        for fn in (
            main.api_public_upcoming,
            main.api_public_folders,
            main.api_public_balance,
            main.api_public_pay,
        ):
            assert _status(fn, bad) == 401
        ghost = main.PublicTokenRequest(verify_token=_token("c-missing"))
        assert _status(main.api_public_balance, ghost) == 404


def test_folders_prefer_drive_url() -> None:
    with _portal() as (main, _fake):
        out = main.api_public_folders(main.PublicTokenRequest(verify_token=_token()))
        assert out["notes_url"] == "https://drive.proton.me/urls/JANE#chart"
        assert out["billing_url"] == "https://drive.proton.me/urls/JANE#bill"
        assert "email" not in out


def test_balance_sums_open_fees_only() -> None:
    from app import fees

    with _portal() as (main, _fake):
        req = main.PublicTokenRequest(verify_token=_token())
        assert main.api_public_balance(req)["owed_cents"] == 0
        fees.create(client_id="c-jane", dos="2026-10-01", amount_cents=26500)
        fees.create(client_id="c-jane", dos="2026-10-08", amount_cents=10000, kind="no_show")
        fees.create(client_id="c-jane", dos="2026-09-01", amount_cents=5000, status="paid")
        out = main.api_public_balance(req)
        assert out == {"ok": True, "owed_cents": 36500, "owed_pretty": "$365.00"}


def test_pay_opens_checkout_then_reuses_it() -> None:
    from app import fees

    with _portal() as (main, fake):
        req = main.PublicTokenRequest(verify_token=_token())
        empty = main.api_public_pay(req)
        assert empty["url"] == "" and empty["owed_cents"] == 0
        assert not fake.posts

        a = fees.create(client_id="c-jane", dos="2026-10-01", amount_cents=26500)
        b = fees.create(client_id="c-jane", dos="2026-10-08", amount_cents=10000, kind="no_show")
        first = main.api_public_pay(req)
        assert first["url"] == "https://checkout.example.invalid/cs_unit_1"
        assert first["owed_cents"] == 36500
        sessions = [p for p in fake.posts if p[0] == "/checkout/sessions"]
        assert len(sessions) == 1
        _path, form, idem = sessions[0]
        assert form["customer"] == "cus_unit1"
        assert form["line_items[0][price_data][unit_amount]"] == "36500"
        assert form["payment_intent_data[setup_future_usage]"] == "off_session"
        assert set(form["metadata[fee_ids]"].split(",")) == {a["id"], b["id"]}
        assert form["success_url"].endswith("/pay/thanks?session_id={CHECKOUT_SESSION_ID}")
        assert idem.startswith("bal-c-jane-36500-")
        for fid in (a["id"], b["id"]):
            row = fees.get(fid)
            assert row["stripe_id"] == "cs_unit_1" and row["status"] == "owed"

        again = main.api_public_pay(req)
        assert again["url"] == first["url"] and again.get("reused") is True
        assert len([p for p in fake.posts if p[0] == "/checkout/sessions"]) == 1


def test_pay_off_without_stripe_key() -> None:
    from app import fees, stripe_pay

    with _portal() as (main, _fake):
        fees.create(client_id="c-jane", dos="2026-10-01", amount_cents=26500)
        stripe_pay.STRIPE_SECRET_KEY = ""
        assert _status(main.api_public_pay, main.PublicTokenRequest(verify_token=_token())) == 503


def test_finalize_checkout_marks_paid_and_saves_card() -> None:
    from app import clients_db, fees
    from app.stripe_pay import finalize_checkout

    with _portal() as (main, fake):
        fee = fees.create(client_id="c-jane", dos="2026-10-01", amount_cents=26500)
        url = main.api_public_pay(main.PublicTokenRequest(verify_token=_token()))["url"]
        sid = url.rsplit("/", 1)[-1]

        assert finalize_checkout(sid) == {"ok": True, "pending": True, "marked": 0}
        assert fees.get(fee["id"])["status"] == "owed"

        fake.pay(sid)
        done = finalize_checkout(sid)
        assert done["ok"] is True and done["marked"] >= 1 and done["saved_card"] is True
        assert fees.get(fee["id"])["status"] == "paid"
        row = clients_db.get_by_id("c-jane")
        assert row["stripe_customer_id"] == "cus_unit1"
        assert row["stripe_payment_method_id"] == "pm_unit"
        assert bool(row["card_on_file"])
        default_pm = [p for p in fake.posts if p[0] == "/customers/cus_unit1"]
        assert default_pm and default_pm[-1][1] == {"invoice_settings[default_payment_method]": "pm_unit"}

        try:
            finalize_checkout("in_not_a_session")
        except RuntimeError:
            pass
        else:
            raise AssertionError("non-checkout id accepted")


def test_pay_thanks_page() -> None:
    from app import fees

    with _portal() as (main, fake):
        assert "Payment not completed" in main.pay_thanks("").body.decode()
        fees.create(client_id="c-jane", dos="2026-10-01", amount_cents=26500)
        sid = main.api_public_pay(main.PublicTokenRequest(verify_token=_token()))["url"].rsplit("/", 1)[-1]
        assert "Payment still processing" in main.pay_thanks(sid).body.decode()
        fake.pay(sid)
        assert "Payment received" in main.pay_thanks(sid).body.decode()


def test_stripe_webhook_checkout_completed() -> None:
    from starlette.requests import Request

    from app import fees

    with _portal() as (main, fake):
        fee = fees.create(client_id="c-jane", dos="2026-10-01", amount_cents=26500)
        sid = main.api_public_pay(main.PublicTokenRequest(verify_token=_token()))["url"].rsplit("/", 1)[-1]
        fake.pay(sid)
        body = json.dumps({"type": "checkout.session.completed", "data": {"object": {"id": sid}}}).encode()
        ts = str(int(time.time()))
        sig = hmac.new(b"whsec_unit", ts.encode() + b"." + body, hashlib.sha256).hexdigest()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/stripe/webhook",
            "query_string": b"",
            "headers": [(b"stripe-signature", f"t={ts},v1={sig}".encode())],
        }
        out = asyncio.run(main.api_stripe_webhook(Request(scope, receive)))
        assert out["ok"] is True and out["type"] == "checkout.session.completed"
        assert fees.get(fee["id"])["status"] == "paid"


def test_upcoming_and_cancel_only_own_visits() -> None:
    with _portal() as (main, _fake):
        visits = [
            {
                "event_id": "ev-next",
                "calendar_id": "cal-1",
                "title": "Follow-up",
                "when_label": "Tue Oct 20 · 10:00 AM",
                "duration_minutes": 30,
                "start_iso": "2026-10-20T10:00:00-05:00",
                "email": "jane@example.com",
                "upcoming": True,
            },
            {
                "event_id": "ev-past",
                "calendar_id": "cal-1",
                "title": "Follow-up",
                "when_label": "Tue Sep 1 · 10:00 AM",
                "start_iso": "2026-09-01T10:00:00-05:00",
                "upcoming": False,
            },
        ]
        seen: list[dict] = []
        deleted: list[tuple[str, str]] = []

        def fake_visits(client, _tz):
            seen.append(client)
            return [dict(v) for v in visits]

        main._visits_for_client = fake_visits
        main.delete_event = lambda cal, eid: deleted.append((cal, eid))
        tok = _token()

        up = main.api_public_upcoming(main.PublicTokenRequest(verify_token=tok))
        assert seen[0]["id"] == "c-jane"
        assert [v["event_id"] for v in up["visits"]] == ["ev-next"]
        assert set(up["visits"][0]) == {"event_id", "title", "when_label", "duration_minutes", "start_iso"}

        other = main.PublicCancelVisitRequest(verify_token=tok, event_id="ev-someone-else")
        assert _status(main.api_public_cancel_visit, other) == 404
        assert deleted == []
        out = main.api_public_cancel_visit(main.PublicCancelVisitRequest(verify_token=tok, event_id="ev-next"))
        assert out == {"ok": True, "cancelled": "Tue Oct 20 · 10:00 AM"}
        assert deleted == [("cal-1", "ev-next")]


def test_verify_routes_recognize_then_code_then_token() -> None:
    from app import verify_codes

    with _portal() as (main, _fake):
        assert main.api_verify_patient(main.VerifyRequest())["reason"] == "need_email_or_phone"
        named = main.api_verify_patient(main.VerifyRequest(phone="(555) 555-0100"))
        assert named == {"ok": True, "level": "message"}  # never the name before a code
        assert main.api_verify_patient(main.VerifyRequest(email="nobody@example.com"))["ok"] is False
        assert main.api_verify_patient(main.VerifyRequest(full_name="Jane Demo", dob="1990-01-02"))["ok"] is False

        sent: list[tuple[str, str, str]] = []

        def sender(channel, destination, code):
            sent.append((channel, destination, code))
            return {"ok": True}

        verify_codes._default_sender = sender
        pending = main.api_verify_code_send(main.VerifyRequest(email="jane@example.com"))
        assert pending["delivery"] == "pending" and "name" not in pending
        assert sent and sent[0][:2] == ("email", "jane@example.com")

        wrong = main.api_verify_patient_code(main.VerifyCodeRequest(email="jane@example.com", code="000000"))
        if sent[0][2] != "000000":
            assert wrong == {"ok": False, "reason": "invalid_code"}
        good = main.api_verify_patient_code(main.VerifyCodeRequest(email="jane@example.com", code=sent[0][2]))
        assert good["ok"] is True and good["level"] == "sensitive" and good["name"] == "Jane Demo"
        out = main.api_public_balance(main.PublicTokenRequest(verify_token=good["verify_token"]))
        assert out["ok"] is True


def test_voice_follow_up_needs_contact_on_file() -> None:
    with _portal() as (main, _fake):
        main.generate_slots = lambda *a, **k: []

        def book(**kw):
            req = main.BookRequest(
                event_type="follow-up",
                start="2026-10-20T10:00:00-05:00",
                duration_minutes=30,
                name="Jane Demo",
                source="voice",
                **kw,
            )
            # api_book now takes the raw Request (phone line marks itself with x-desk-channel: voice)
            http = Request({"type": "http", "method": "POST", "path": "/api/book", "query_string": b"",
                            "headers": [(b"x-desk-channel", b"voice")]})
            return _status(main.api_book, req, BackgroundTasks(), http)

        assert book(email="stranger@example.com") == 401
        assert book(email="jane@example.com", verify_token="bad.token") == 401
        assert book(email="other@example.com", phone="555-555-0100") == 401
        assert book(email="jane@example.com") == 409
        assert book(email="other@example.com", verify_token=_token()) == 409


def test_slot_taken_is_a_clear_409() -> None:
    with _portal() as (main, _fake):
        main.generate_slots = lambda *a, **k: []
        req = main.BookRequest(event_type="follow-up", start="2026-10-20T10:00:00-05:00", duration_minutes=30,
                               name="Jane Demo", email="jane@example.com", source="web")
        http = Request({"type": "http", "method": "POST", "path": "/api/book", "query_string": b"", "headers": []})
        try:
            main.api_book(req, BackgroundTasks(), http)
        except HTTPException as e:
            assert e.status_code == 409 and "no longer available" in str(e.detail)
        else:
            raise AssertionError("expected 409")


def test_booker_page_keeps_slot_taken_message_after_refresh() -> None:
    """Root cause: the submit catch set the error, then loadSlots() cleared it. Now: refresh, then message."""
    import subprocess

    js = (ROOT / "static" / "js" / "booker.js").read_text()
    i = js.index("await loadSlots();\n        if (e && e.status === 409)")
    assert js.index("SLOT_TAKEN_MSG, \"err\")", i) > i
    assert 'const SLOT_TAKEN_MSG = "That time was just taken. Please pick another.";' in js
    fetch_fn = js[js.index("async function fetchJSON"): js.index("function el(id)")]
    harness = fetch_fn + """
globalThis.fetch = async () => ({ ok: false, status: 409, statusText: "Conflict",
  json: async () => ({ detail: "That slot is no longer available. Please pick another." }) });
fetchJSON("/api/book", {}).then(() => { console.log("no-throw"); process.exit(1); },
  (e) => { console.log(e.status); process.exit(e.status === 409 ? 0 : 1); });
"""
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr


def test_new_client_page_uses_hold_wording_only_for_new_client() -> None:
    html = (Path(__file__).resolve().parents[1] / "static" / "book.html").read_text()
    assert 'if (slug === "new-client")' in html
    assert "Next you'll add a card to confirm. Nothing is charged today." in html
    assert "Hold this time" in html
    # Default copy stays for every other type.
    assert "We’ll email a confirmation with a cancel link." in html
    assert ">Confirm booking</button>" in html


def test_booker_polish_contrast_and_single_length_label() -> None:
    css = (ROOT / "static" / "css" / "booker.css").read_text()
    html = (ROOT / "static" / "book.html").read_text()
    assert "--muted: #595959;" in css
    assert ".cal-day:disabled {\n  color: var(--muted);" in css
    assert "font-size: 0.875rem;\n  line-height: 1.4;\n  color: var(--muted, #666);" in css
    assert '"Visit length: " + durs[0] + " min"' in html
    assert 'duration-bar").hidden = true' not in html


def test_voice_booking_existing_client_unfinished_intake_gets_link_no_hold() -> None:
    from app import onboarding

    with _portal() as (main, _fake):
        start = "2026-10-20T10:00:00-05:00"
        saved = {k: getattr(main, k) for k in ("_phone_slots", "create_event", "_mint_intake_url")}
        saved_status = onboarding.status
        main._phone_slots = lambda *a, **k: [{"start": start}]
        main.generate_slots = lambda *a, **k: [{"start": start}]
        main.create_event = lambda **k: {"htmlLink": ""}
        main._mint_intake_url = lambda email, *a, **k: "https://site.example/intake#tok"
        try:
            def book(channel: bytes, email: str = "jane@example.com"):
                req = main.BookRequest(event_type="follow-up", start=start, duration_minutes=30, name="Jane Demo",
                                       email=email, phone="555-555-0100", sms_consent=True, source="voice",
                                       verify_token=_token())
                hdrs = [(b"x-desk-channel", channel)] if channel else []
                http = Request({"type": "http", "method": "POST", "path": "/api/book", "query_string": b"",
                                "headers": hdrs})
                bt = BackgroundTasks()
                out = main.api_book(req, bt, http)
                return out, (bt.tasks[0].kwargs.get("intake_url") if bt.tasks else None)

            out, url = book(b"voice")
            assert out.get("intake_link_sent") is True and not out.get("held")
            assert url == "https://site.example/intake#tok"
            out, url = book(b"")  # web: unchanged
            assert "intake_link_sent" not in out and not url
            onboarding.status = lambda **k: {"complete": True}
            out, url = book(b"voice")  # intake complete: unchanged
            assert "intake_link_sent" not in out and not url
        finally:
            onboarding.status = saved_status
            for k, v in saved.items():
                setattr(main, k, v)


def test_confirm_sms_carries_intake_link_when_given() -> None:
    from app.email_templates import confirm_sms

    s = confirm_sms(title="Follow-up", when_label="Tue Oct 20 · 10:00 AM", intake_url="https://x/intake")
    assert "https://x/intake" in s and "card on file" in s
    assert "intake" not in confirm_sms(title="Follow-up", when_label="Tue Oct 20 · 10:00 AM")
