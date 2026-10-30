"""Per-client intake: family card share, status by client id, Spruce STOP/START."""
from __future__ import annotations

import os
import tempfile


def _fresh_db():
    d = tempfile.mkdtemp()
    os.environ["CLIENTS_DB"] = os.path.join(d, "c.sqlite")
    import importlib

    from app import clients_db

    importlib.reload(clients_db)
    clients_db.DB_PATH = __import__("pathlib").Path(os.environ["CLIENTS_DB"])
    clients_db.connect()
    return clients_db


def test_family_card_shared_unless_own_and_stop_start() -> None:
    cdb = _fresh_db()
    from app import chat_threads, patients

    for cid, ph in (("a", "+15550000001"), ("b", "+15550000001"), ("c", "+15550000001"), ("d", "+15550000002")):
        cdb.upsert({"id": cid, "display_name": cid, "email": "fam@example.com", "phone_e164": ph})
    cdb.upsert({"id": "c", "card_on_file": True, "stripe_payment_method_id": "pm_own"})
    out = patients.record_card_on_file(client_id="a", email="fam@example.com", customer_id="cus_1",
                                       payment_method_id="pm_fam", brand="visa", last4="4242")
    assert out["client_id"] == "a" and out["shared_with"] == 1
    b, c, d = cdb.get_by_id("b"), cdb.get_by_id("c"), cdb.get_by_id("d")
    assert b["card_on_file"] and b["stripe_payment_method_id"] == "pm_fam" and b["card_shared_from"] == "a"
    assert c["stripe_payment_method_id"] == "pm_own" and not c.get("card_shared_from")  # own card kept
    assert not d["card_on_file"]  # different phone: not family
    item = {"direction": "inbound", "text": "STOP", "conversation": {"externalParticipants": [
        {"endpoint": {"channel": "phone", "rawValue": "+15550000002"}}]}}
    assert chat_threads.apply_sms_keyword(item) == "off"
    assert cdb.sms_consent_state(cdb.get_by_id("d")) == "no"
    item["text"] = "Start"
    assert chat_threads.apply_sms_keyword(item) == "on"
    assert cdb.sms_consent_state(cdb.get_by_id("d")) == "yes"
    item["direction"] = "outbound"
    item["text"] = "stop"
    assert chat_threads.apply_sms_keyword(item) == ""
