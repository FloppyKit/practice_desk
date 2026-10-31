"""Refills v0: rules, match, last visit, PHI-free summary. Synthetic fixtures; no network."""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from app import clients_db, refills

T = date(2026, 10, 30)


def R(**kw):
    base = dict(controlled="none", age_years=30, last_visit=date(2026, 9, 1), next_visit=None, today=T)
    base.update(kw)
    return refills.recommend(**base)


def test_adult_within_window_with_visit_booked() -> None:
    r = R(next_visit=date(2026, 11, 20))
    assert r["recommendation"] == "refill_as_last_order" and r["max_supply_days"] == 90


def test_adult_within_window_no_visit_bridges() -> None:
    r = R()
    assert r["recommendation"] == "bridge_30_and_schedule" and "booking_text_needed" in r["flags"]
    assert r["max_supply_days"] == 30


def test_minor_window_is_3_months() -> None:
    assert R(age_years=15, last_visit=date(2026, 8, 15))["recommendation"] == "bridge_30_and_schedule"
    assert R(age_years=30, last_visit=date(2026, 6, 1))["recommendation"] == "bridge_30_and_schedule"


def test_minor_grace_3_to_4_months_bridges_then_visit() -> None:
    r = R(age_years=15, last_visit=date(2026, 7, 15))  # 107 days
    assert r["recommendation"] == "bridge_30_then_visit" and r["max_supply_days"] == 30
    assert "visit_needed_before_more" in r["flags"]
    assert R(age_years=15, last_visit=date(2026, 6, 1))["recommendation"] == "needs_visit"  # >4 months


def test_adult_no_grace() -> None:
    assert R(age_years=30, last_visit=date(2026, 4, 20))["recommendation"] == "needs_visit"  # 193 days


def test_past_window_with_booking_notes_bucket() -> None:
    r = R(last_visit=date(2025, 12, 1), next_visit=date(2026, 11, 3))
    assert r["recommendation"] == "needs_visit" and r["notes"] == ["visit booked within 7 days"]


def test_no_visit_ever_needs_visit() -> None:
    r = R(last_visit=None)
    assert r["recommendation"] == "needs_visit" and "no_completed_visit" in r["flags"]


def test_controlled_or_unknown() -> None:
    assert R(controlled="unknown", last_visit=date(2026, 9, 1))["recommendation"] == "controlled_matt_signs"
    assert R(controlled="C2", last_visit=date(2026, 6, 1))["recommendation"] == "needs_visit"
    assert R(controlled=None, last_visit=date(2026, 10, 1))["status"] == "controlled"


def test_age_on() -> None:
    assert refills.age_on("2008-10-31", T) == 17 and refills.age_on("2008-10-30", T) == 18


def test_prefilter() -> None:
    assert refills.prefilter("need a refill of my sertraline")
    assert not refills.prefilter("can we move my appointment to Tuesday")


def _db(tmp: str) -> None:
    clients_db.DB_PATH = Path(tmp) / "c.sqlite"
    with clients_db.connect() as con:
        for i, (n, ph, dob) in enumerate([("Synth Alpha", "+15550000001", "1990-01-02"),
                                          ("Synth Beta", "+15550000002", "2012-05-05"),
                                          ("Synth Beta", "+15550000003", "1980-01-01")]):
            con.execute("INSERT INTO clients (id, display_name, email, phone_e164, dob) VALUES (?,?,?,?,?)",
                        (f"c{i}", n, f"s{i}@example.invalid", ph, dob))
        con.execute("CREATE TABLE imported_appointments (client_id TEXT, start_utc TEXT, status TEXT)")
        con.executemany("INSERT INTO imported_appointments VALUES (?,?,?)", [
            ("c0", "2026-08-01T15:00:00+00:00", "Confirmed"),
            ("c0", "2026-09-15T15:00:00+00:00", "DidNotAttend"),
            ("c0", "2026-10-01T15:00:00+00:00", "Cancelled")])


def test_match_rules_and_last_visit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _db(tmp)
        with clients_db.connect() as con:
            m = lambda **k: refills.match_client(con, **dict(dict(phones=[], email="", name="", dob=""), **k))
            assert m(phones=["+15550000001"], name="Synth Alpha")[:2] == ("c0", "matched")  # no DOB needed
            assert m(email="s0@example.invalid", name="Synth Alpha")[1] == "matched"
            assert m(phones=["+15550000001"], name="Synth Alpha", dob="1990-01-02")[2] == "contact+name+dob"
            assert m(phones=[], name="Synth Alpha")[1] == "unmatched"  # name only never matches
            assert m(phones=["+15550000001"], name="Someone Else")[1] == "ambiguous_match"
            lv, src = refills.last_visit(con, "c0", now=datetime(2026, 10, 30, tzinfo=timezone.utc),
                                         calendar_past=[{"start_iso": "2026-09-20T10:00:00-05:00", "no_show": True}])
            assert (lv, src) == (date(2026, 8, 1), "imported_appointments")


def test_dob_conflict_is_flagged_not_ignored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _db(tmp)
        with clients_db.connect() as con:
            cid, st, _b, flags = refills.match_client(con, phones=["+15550000001"], email="",
                                                      name="Synth Alpha", dob="1990-01-03")
            assert (cid, st, flags) == ("c0", "matched", ["dob_mismatch"])


def test_shared_family_contact_is_ambiguous() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _db(tmp)
        with clients_db.connect() as con:
            con.execute("UPDATE clients SET phone_e164='+15550000001' WHERE id='c1'")  # parent + child share a phone
            cid, st, _b, _f = refills.match_client(con, phones=["+15550000001"], email="", name="Synth Alpha", dob="")
            assert (cid, st) == ("", "ambiguous_match")


def test_ingest_and_summary_has_no_phi() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _db(tmp)
        fake = lambda _t: {"is_refill": True, "name": "Synth Alpha", "dob": "1990-01-02",
                           "meds": ["sertraline"], "pharmacy": "Synthetic Pharmacy 1 Main St"}
        rid = refills.ingest(source="chat", source_ref="t1", text="need a refill of my sertraline",
                             phones=["+15550000001"], extract_fn=fake, visits_fn=lambda c: [],
                             now=datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
        assert rid
        assert refills.ingest(source="chat", source_ref="t2", text="hello there", extract_fn=fake) is None
        row = refills.list_requests()[0]
        assert row["status"] == "controlled" and row["recommendation"] == "controlled_matt_signs"
        assert row["last_visit_source"] == "imported_appointments" and row["controlled"] == "unknown"
        blob = json.dumps(refills.summary()).lower()
        for leak in ("synth", "alpha", "1990", "sertraline", "pharmacy", "555", "c0", "example"):
            assert leak not in blob, leak
        assert refills.summary()["by_status"]["controlled"] == 1
