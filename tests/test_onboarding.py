"""New-client onboarding: hold at booking, intake flags, Chart/Consents PDF, confirm / remind / release,
fee add defaults to no charge. Fake calendar, fake Drive, fake sends. No network."""
from __future__ import annotations

import base64
import json
import os
import struct
import tempfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Request

CT = ZoneInfo("America/Chicago")


def _png() -> str:
    w, h = 12, 6
    rows = bytearray()
    for y in range(h):
        rows.append(1)  # Sub filter
        for x in range(w):
            rows += bytes((10, 20, 30, 255 if (x + y) % 2 else 0)) if x == 0 else bytes((0, 0, 0, 0))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(rows))) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode()


class FakeCal:
    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.patches: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def create(self, **kw):
        ev = {"id": kw["uid"], "summary": kw["summary"], "status": kw.get("status") or "confirmed",
              "extendedProperties": {"private": dict(kw.get("private_props") or {})}}
        self.events[kw["uid"]] = ev
        return ev

    def get(self, _cid, eid):
        if eid not in self.events:
            raise KeyError(eid)
        return self.events[eid]

    def patch(self, _cid, eid, **kw):
        self.patches.append((eid, kw))
        ev = self.events[eid]
        if kw.get("summary") is not None:
            ev["summary"] = kw["summary"]
        if kw.get("status") is not None:
            ev["status"] = kw["status"]
        return ev

    def delete(self, _cid, eid):
        self.deleted.append(eid)
        self.events.pop(eid, None)


@contextmanager
def _env():
    from app import clients_db, config, onboarding
    from app import main

    saved = {
        (clients_db, "DB_PATH"): clients_db.DB_PATH,
        (config, "CONFIG_PATH"): config.CONFIG_PATH,
        (main, "create_event"): main.create_event,
        (main, "get_event"): main.get_event,
        (main, "patch_event"): main.patch_event,
        (main, "delete_event"): main.delete_event,
        (main, "generate_slots"): main.generate_slots,
        (main, "_mint_intake_url"): main._mint_intake_url,
        (main, "_require_staff"): main._require_staff,
        (main, "_onboarding_fx"): main._onboarding_fx,
        (main, "_notify_post"): main._notify_post,
        (main, "_notify_get"): main._notify_get,
        (main, "_drive_mkdir"): main._drive_mkdir,
        (main, "_bill_send"): main._bill_send,
    }
    saved_env = {k: os.environ.get(k) for k in ("BOOKER_LOG_DIR", "CANCEL_SECRET_FILE")}
    cal = FakeCal()
    sends: list[tuple[str, str, str]] = []
    drive: dict[str, bytes] = {}
    folders = {"/", "/Clients"}
    try:
        with tempfile.TemporaryDirectory() as raw:
            sec = Path(raw) / "cancel.secret"
            sec.write_text("onboarding-test-secret")
            os.environ["BOOKER_LOG_DIR"] = raw
            os.environ["CANCEL_SECRET_FILE"] = str(sec)
            clients_db.DB_PATH = Path(raw) / "clients.sqlite"
            config.CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "event-types.example.yaml"
            main.create_event = cal.create
            main.get_event = cal.get
            main.patch_event = cal.patch
            main.delete_event = cal.delete
            main._require_staff = lambda *a, **k: None
            main._mint_intake_url = lambda email, name="", forms=None, client_id="": "https://site.example.invalid/intake/open?t=TOKEN"

            def fx():
                return onboarding.Effects(
                    get_event=cal.get, patch_event=cal.patch, delete_event=cal.delete,
                    send_email=lambda to, sub, body, **k: sends.append(("email", to, sub)) or {"ok": True},
                    send_sms=lambda to, body, **k: sends.append(("sms", to, body)) or {"ok": True},
                    staff_email=lambda sub, body: sends.append(("staff", "", sub)),
                    rebook_url="https://book.example.invalid/new-client",
                )

            main._onboarding_fx = fx

            def bill_send(*, kind, view, fields, sms_body, **k):
                sends.append(("email", str(view.get("email") or ""), kind))
                sends.append(("sms", str(view.get("phone") or ""), sms_body))
                return {"email": {"ok": True}, "sms": {"ok": True}}

            main._bill_send = bill_send

            def notify_post(path, payload):
                drive[payload["path"]] = base64.b64decode(payload["b64"])
                return 200, {"ok": True}

            def notify_get(path):
                from urllib.parse import unquote

                target = unquote(path.split("path=", 1)[1])
                if path.startswith("/drive/file"):
                    return (200, drive[target], "application/pdf") if target in drive else (404, b"", "")
                if target not in folders:
                    return 502, b'{"ok": false}', "application/json"
                pre = target.rstrip("/") + "/"
                items = [{"name": f[len(pre):], "type": "folder"} for f in folders
                         if f.startswith(pre) and "/" not in f[len(pre):]]
                items += [{"name": f[len(pre):], "type": "file"} for f in drive
                          if f.startswith(pre) and "/" not in f[len(pre):]]
                return 200, json.dumps({"ok": True, "items": items}).encode(), "application/json"

            def mkdir(path):
                assert path not in folders and path.rsplit("/", 1)[0] in folders, path
                folders.add(path)

            main._notify_post, main._notify_get, main._drive_mkdir = notify_post, notify_get, mkdir
            yield main, cal, sends, drive, folders, fx
    finally:
        for (mod, name), val in saved.items():
            setattr(mod, name, val)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _book(main, *, start: datetime, email="new.person@example.com", event_type="new-client",
          duration=90, voice=False, sms=True):
    main.generate_slots = lambda *a, **k: [{"start": start.isoformat()}]
    main._phone_slots = main._phone_slots
    req = main.BookRequest(event_type=event_type, start=start.isoformat(), duration_minutes=duration,
                           name="Nova Newperson", email=email, phone="913-555-0100", sms_consent=sms,
                           source="voice" if voice else "web")
    headers = [(b"x-desk-channel", b"voice")] if voice else []
    http = Request({"type": "http", "method": "POST", "path": "/api/book", "query_string": b"", "headers": headers})
    if voice:
        saved = main._phone_slots
        main._phone_slots = lambda *a, **k: [{"start": start.isoformat()}]
    bg = BackgroundTasks()
    try:
        out = main.api_book(req, bg, http)
    finally:
        if voice:
            main._phone_slots = saved
    notice = next((t.kwargs for t in bg.tasks if t.func is main._send_booking_notices), {})
    return out, notice


def _submit(main, form: str, email="new.person@example.com", **fields):
    base = {"fullName": "Nova Newperson", "email": email}
    if form != "personal":
        base["signature"] = _png()
    base.update(fields)
    bg = BackgroundTasks()
    meta = {"ip": "203.0.113.9", "userAgent": "UnitUA", "linkAuth": {"verified": True, "email": email}}
    out = main.api_internal_intake_submitted(main.InternalIntakeRequest(form=form, fields=base, meta=meta),
                                             Request({"type": "http", "headers": []}), bg)
    for t in bg.tasks:
        t.func(*t.args, **t.kwargs)
    return out


def _card(main, email="new.person@example.com"):
    req = main.InternalCardRequest(email=email, customer_id="cus_unit", payment_method_id="pm_unit",
                                   brand="visa", last4="4242", exp_month=12, exp_year=2030)
    return main.api_internal_client_card(req, Request({"type": "http", "headers": []}))


def test_deadline_policy() -> None:
    from app.onboarding import deadline_for, remind_at_for

    booked = datetime(2026, 10, 27, 16, 0, tzinfo=CT)
    far = booked + timedelta(days=10)
    assert deadline_for(booked, far) == booked + timedelta(hours=48)
    soon = booked + timedelta(hours=26)  # web minimum notice is 24h
    assert deadline_for(booked, soon) == soon - timedelta(hours=2)
    very_soon = booked + timedelta(hours=1)  # phone with short notice
    assert deadline_for(booked, very_soon) == booked + timedelta(minutes=30)
    assert remind_at_for(booked, booked + timedelta(hours=48)) == booked + timedelta(hours=24)


def test_new_client_web_booking_is_held_and_goes_to_intake() -> None:
    with _env() as (main, cal, _s, _d, _f, _fx):
        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        out, notice = _book(main, start=start)
        assert out["held"] is True and out["intake_url"].endswith("t=TOKEN")
        assert "is held for 48 hours, until" in out["message"]
        ev = cal.events[out["event_id"]]
        assert ev["status"] == "tentative" and ev["summary"].startswith("HELD (paperwork due)")
        assert ev["extendedProperties"]["private"]["hold"] == "held"
        assert notice["intake_url"].endswith("t=TOKEN") and notice["hold_until_label"]
        from app import clients_db, onboarding

        row = clients_db.get_by_email("new.person@example.com")
        assert row and row["source"] == onboarding.NEW_SOURCE
        assert onboarding.get_hold(out["event_id"])["status"] == "held"


def test_phone_booking_held_but_link_goes_by_text_and_email() -> None:
    with _env() as (main, cal, _s, _d, _f, _fx):
        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=5)
        out, notice = _book(main, start=start, voice=True)
        assert out["held"] is True and "intake_url" not in out
        assert notice["intake_url"].endswith("t=TOKEN")


def test_existing_client_and_other_types_unchanged() -> None:
    with _env() as (main, cal, _s, _d, _f, _fx):
        from app import clients_db

        clients_db.create(name="Old Client", email="old@example.com", source="carepatron")
        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        out, notice = _book(main, start=start, email="old@example.com")
        assert "held" not in out and cal.events[out["event_id"]]["status"] == "confirmed"
        assert not notice.get("hold_until_label")
        out2, _ = _book(main, start=start, email="brand.new@example.com", event_type="follow-up", duration=30)
        assert "held" not in out2


def test_forms_flip_flags_file_pdf_and_card_confirms_hold() -> None:
    with _env() as (main, cal, sends, drive, folders, _fx):
        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        out, _ = _book(main, start=start)
        eid = out["event_id"]
        r1 = _submit(main, "personal", phone="913-555-0100")
        assert r1["linked"] and r1["flags"]["personal"] and not r1["complete"]
        r2 = _submit(main, "consents", informedConsent=True)
        assert r2["flags"]["consents"] and not r2["complete"]
        assert "/Clients/Newperson, Nova/Chart/Consents" in folders
        pdfs = [p for p in drive if p.startswith("/Clients/Newperson, Nova/Chart/Consents/")]
        assert len(pdfs) == 2 and all(drive[p].startswith(b"%PDF") for p in pdfs)
        consent = next(drive[p] for p in pdfs if p.endswith("consents.pdf"))
        assert b"/Im1 Do" in consent and b"/SMask" in consent  # drawn signature embedded
        assert b"203.0.113.9" in consent and b"VERIFIED" in consent
        assert cal.events[eid]["status"] == "tentative"
        _submit(main, "card-auth", authorizeCharges=True)
        assert cal.events[eid]["status"] == "tentative"  # signature alone is not a card on file
        res = _card(main)
        assert res["confirmed_holds"] == [eid]
        assert cal.events[eid]["status"] == "confirmed"
        assert not cal.events[eid]["summary"].startswith("HELD")
        from app import onboarding

        assert onboarding.get_hold(eid)["status"] == "confirmed"
        assert any(k == "email" and "Confirmed" in sub for k, _to, sub in sends)
        # desk payloads carry the flags
        week = main._desk_week_payload  # noqa: F841 (needs calendar listing; covered by _attach_intake)
        visits = [{"event_id": eid, "event_type": "new-client"}]
        main._attach_intake(visits)
        assert visits[0]["intake"]["complete"] and visits[0]["hold"]["status"] == "confirmed"
        row = onboarding.find_client("new.person@example.com")
        summary = main._intake_summary({"id": row["id"], "email": row["email"]})
        assert summary["flags"] == {"personal": True, "consents": True, "card": True,
                                    "card_auth": True, "roi": False}


def test_reminder_then_release_with_short_deadline() -> None:
    with _env() as (main, cal, sends, _d, _f, fx):
        from app import onboarding

        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        out, _ = _book(main, start=start)
        eid = out["event_id"]
        main.api_desk_hold_times(eid, main.HoldTimesRequest(deadline_minutes=10, remind_minutes=-1),
                                 Request({"type": "http", "headers": []}))
        r = onboarding.sweep(fx())
        assert r["reminded"] == [eid] and r["released"] == []
        assert onboarding.sweep(fx())["reminded"] == []  # once only
        r = onboarding.sweep(fx(), now=datetime.now(timezone.utc) + timedelta(minutes=11))
        assert r["released"] == [eid] and cal.deleted == [eid]
        assert onboarding.get_hold(eid)["status"] == "released"
        kinds = [(k, sub) for k, _to, sub in sends]
        assert any(k == "email" and "released" in sub for k, sub in kinds)
        assert sum(1 for k, _to, b in sends if k == "sms") == 2  # reminder + release, consented phone
        assert onboarding.sweep(fx(), now=datetime.now(timezone.utc) + timedelta(hours=1))["released"] == []


def test_cancelled_hold_is_not_released_with_a_notice() -> None:
    with _env() as (main, cal, sends, _d, _f, fx):
        from app import onboarding

        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        out, _ = _book(main, start=start)
        cal.events.pop(out["event_id"])  # client cancelled via the cancel link
        onboarding.set_times(out["event_id"], deadline=datetime.now(timezone.utc) - timedelta(minutes=1),
                             remind_at=datetime.now(timezone.utc) - timedelta(minutes=2))
        r = onboarding.sweep(fx())
        assert r["gone"] == [out["event_id"]] and not sends


def test_add_fee_without_explicit_charge_does_not_charge() -> None:
    from tests.test_billing import _desk

    with _desk(card=True, live=True) as (main, fake, _drive):
        req = main.DeskFeeRequest(client_id="c-unit", amount="100", dos="2026-10-27")
        out = main.api_desk_fees_create(req, "")
        assert out.get("charged") is False
        assert "/payment_intents" not in fake.posts


def test_progress_for_cascade_resume() -> None:
    with _env() as (main, _cal, _s, _d, _f, _fx):
        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        _book(main, start=start)
        r = lambda: main.api_internal_intake_progress(Request({"type": "http", "headers": []}),  # noqa: E731
                                                     email="new.person@example.com")
        p = r()
        assert p["missing"] == ["card", "card_auth", "personal", "consents"]
        assert p["client"]["given_name"] == "Nova" and p["client"]["phone"].endswith("0100")
        assert p["hold"]["status"] == "held"
        _submit(main, "card-auth", authorizeCharges=True)
        assert r()["missing"] == ["card", "personal", "consents"]


def test_desk_new_client_booking_gets_link_but_no_hold() -> None:
    with _env() as (main, cal, _s, _d, _f, _fx):
        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        main.generate_slots = lambda *a, **k: []
        req = main.BookRequest(event_type="new-client", start=start.isoformat(), duration_minutes=90,
                               name="Desk Person", email="desk.person@example.com", source="desk")
        bg = BackgroundTasks()
        out = main.api_book(req, bg, Request({"type": "http", "headers": []}))
        notice = next(t.kwargs for t in bg.tasks if t.func is main._send_booking_notices)
        assert "held" not in out and cal.events[out["event_id"]]["status"] == "confirmed"
        assert notice["intake_url"].endswith("t=TOKEN") and not notice["hold_until_label"]
