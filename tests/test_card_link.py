"""Card-change link: on-file contact only, consent gates SMS, 10-minute limit, masked hint, no PHI."""
from __future__ import annotations

import os
import tempfile


def _mod():
    os.environ["BOOKER_LOG_DIR"] = tempfile.mkdtemp()
    import importlib

    from app import card_link

    return importlib.reload(card_link)


ROW = {"id": "c-demo", "email": "psycharts@mail.grokbot.com", "phone_e164": "+19133918760", "sms_consent": 1,
       "display_name": "Demo Tester Test", "standing_dx": "F42"}
URL = "https://drmattbrown.com/pay"


class Rec:
    def __init__(self):
        self.email, self.sms = [], []

    def e(self, to, subj, body):
        self.email.append((to, subj, body)); return {"ok": True}

    def s(self, to, body):
        self.sms.append((to, body)); return {"ok": True}


def test_sent_email_and_sms_to_on_file():
    m, r = _mod(), Rec()
    out = m.send_card_link(ROW, pay_url=URL, send_email=r.e, send_sms=r.s, now=1000)
    assert out == {"status": "sent", "hint": "p*********@mail.grokbot.com and the mobile ending 8760"}, out
    assert [x[0] for x in r.email] == [ROW["email"]] and [x[0] for x in r.sms] == [ROW["phone_e164"]]
    assert URL in r.email[0][2] and URL in r.sms[0][1]


def test_rate_limited_within_10_minutes_then_ok():
    m, r = _mod(), Rec()
    m.send_card_link(ROW, pay_url=URL, send_email=r.e, send_sms=r.s, now=1000)
    out = m.send_card_link(ROW, pay_url=URL, send_email=r.e, send_sms=r.s, now=1000 + 599)
    assert out["status"] == "rate_limited" and len(r.email) == 1 and len(r.sms) == 1
    out = m.send_card_link(ROW, pay_url=URL, send_email=r.e, send_sms=r.s, now=1000 + 601)
    assert out["status"] == "sent" and len(r.email) == 2


def test_no_consent_is_email_only():
    m, r = _mod(), Rec()
    out = m.send_card_link({**ROW, "sms_consent": 0}, pay_url=URL, send_email=r.e, send_sms=r.s, now=1)
    assert out == {"status": "sent", "hint": "p*********@mail.grokbot.com"} and not r.sms and len(r.email) == 1


def test_no_spruce_sender_is_email_only():
    m, r = _mod(), Rec()
    out = m.send_card_link(ROW, pay_url=URL, send_email=r.e, send_sms=None, now=1)
    assert out["status"] == "sent" and not r.sms


def test_body_has_no_phi_and_response_has_no_address():
    m, r = _mod(), Rec()
    out = m.send_card_link(ROW, pay_url=URL, send_email=r.e, send_sms=r.s, now=1)
    blob = " ".join([r.email[0][1], r.email[0][2], r.sms[0][1]])
    for bad in ("Demo", "Tester", "F42", "visit", "appointment", "diagnos", "med"):
        assert bad.lower() not in blob.lower(), bad
    assert "update your card on file" in blob.lower() and "Nothing is charged when you save." in blob
    assert ROW["email"] not in str(out) and "8760" in out["hint"] and "+1913" not in str(out)


def test_send_failure_not_reported_sent():
    m = _mod()
    out = m.send_card_link(ROW, pay_url=URL, send_email=lambda *a: {"ok": False}, send_sms=lambda *a: {"ok": False}, now=1)
    assert out == {"status": "failed", "hint": ""}


def test_endpoint_auth_and_token(monkeypatch=None):
    """401 without the staff secret, 401 with a bad login token; senders mocked; response has only status+hint."""
    import importlib

    os.environ["BOOKER_LOG_DIR"] = tempfile.mkdtemp()
    from fastapi.testclient import TestClient

    from app import main, card_link, notify, spruce_api

    importlib.reload(card_link)
    sent = []
    notify.send_email = lambda to, s, b, **k: (sent.append(("email", to)), {"ok": True})[1]
    spruce_api.send_spruce_sms = lambda to, b, **k: (sent.append(("sms", to)), {"ok": True})[1]
    spruce_api.spruce_api_enabled = lambda: True
    main._verified_client = lambda t: (ROW if t == "good" else (_ for _ in ()).throw(main.HTTPException(401, "invalid")))
    secret = main.REMIND_SECRET or "test-secret"
    main.REMIND_SECRET = secret
    c = TestClient(main.app)
    assert c.post("/api/internal/card-link", json={"verify_token": "good"}).status_code == 401
    assert c.post("/api/internal/card-link", json={"verify_token": "good"}, headers={"X-Staff-Secret": "wrong"}).status_code == 401
    assert c.post("/api/internal/card-link", json={"verify_token": "bad"}, headers={"X-Staff-Secret": secret}).status_code == 401
    assert not sent
    r = c.post("/api/internal/card-link", json={"verify_token": "good", "email": "attacker@example.com"},
               headers={"X-Staff-Secret": secret})
    assert r.status_code == 200 and set(r.json()) == {"status", "hint"} and r.json()["status"] == "sent", r.text
    assert sent == [("email", ROW["email"]), ("sms", ROW["phone_e164"])], sent
    r2 = c.post("/api/internal/card-link", json={"verify_token": "good"}, headers={"X-Staff-Secret": secret})
    assert r2.json()["status"] == "rate_limited" and len(sent) == 2
