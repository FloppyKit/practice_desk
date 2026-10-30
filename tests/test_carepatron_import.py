"""Carepatron importer. SYNTHETIC data only (fake names); Bedrock mocked."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from app import carepatron_import as ci

CSV_HDR = ["Id", "First name", "Last name", "Preferred name", "Email", "Phone number", "Date of birth", "Status", "DX Code", "Medications", "Ins Info", "Card Number", "Exp Date", "CVC"]
ROWS = [
    ["cp-001", "Testy", "McFakerson", "", "TESTY@example.test", "'+13175550101", "01/02/1980", "Active", "f411, F32.A; U071", "sertraline 50 mg\nhydroxyzine 25 mg prn", "FakeCo 123", "4111111111111111", "12/29", "cvcQ987"],
    ["cp-002", "Ada", "Placeholder", "Addie", "ada@example.test", "317.555.0102", "1990-03-04", "Active", "", "", "", "", "", ""],
    ["C0FFEE03-0000", "Bob", "Nobody", "", "", "317-555-0103", "5/6/1975", "Archived", "F90.0", "", "", "5555444433332222", "01/30", "cvcQ123"],
]


def _fixture(rows=ROWS):
    d = Path(tempfile.mkdtemp(prefix="cp-test-"))
    with open(d / "clients.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(CSV_HDR); w.writerows(rows)
    notes = d / "notes"; (notes / "cp-001").mkdir(parents=True)
    (notes / "cp-001" / "2026-01-05 Progress note.md").write_text("# Progress note\nDx F41.1. Sertraline 50 mg.\n")
    (notes / "Bob Nobody (c0ffee03)").mkdir()
    (notes / "Bob Nobody (c0ffee03)" / "Session Note - 2026-03-01 (2).md").write_text("x\n")
    (notes / "Ada Placeholder 2026-02-01.md").write_text("# Intake\nSynthetic text.\n")
    (notes / "Unknown Person 2026-02-02.md").write_text("orphan\n")
    with open(d / "appts.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["AppointmentId", "StartDate", "EndDate", "Title", "ClientId", "Client", "Status", "Service",
                    "Code", "Price", "DurationInMinutes", "Invoices", "DiagnosisCodes", "TimeZone", "Card Number"])
        w.writerow(["ap-1", "2026-01-05 10:00:00", "2026-01-05 10:30:00", "t", "CP-001", "", "Confirmed", "Follow-up",
                    "99214", "200", "30", "000001 - Paid", "", "America/Chicago", "4111111111111111"])
        w.writerow(["ap-1", "2026-01-05 10:00:00", "2026-01-05 10:30:00", "t", "CP-001", "", "Confirmed", "Add-on",
                    "90833", "100", "30", "000002 - Paid", "", "America/Chicago", ""])
        w.writerow(["ap-2", "2026-07-01 09:00:00", "", "t", "", "Ada Placeholder", "Cancelled", "Intake",
                    "", "", "60", "", "", "US/Central", ""])
        w.writerow(["ap-3", "2026-02-03 09:00:00", "", "t", "cp-999", "Zed Nobody", "Confirmed", "Intake",
                    "", "", "60", "", "", "America/New_York", ""])
        w.writerow(["blk-1", "2026-02-04 12:00:00", "", "Lunch", "", "", "", "", "", "", "60", "", "", "America/Chicago", ""])
    return d


def _run(d, apply=True, **kw):
    return ci.run(db=d / "db.sqlite", clients_csv=d / "clients.csv", notes_dir=d / "notes",
                  appts=d / "appts.csv", pdf_dir=None, out_dir=d / "out", apply=apply, **kw)


def _q(d, sql):
    con = sqlite3.connect(d / "db.sqlite"); r = con.execute(sql).fetchall(); con.close(); return r


def test_cp_normalizers():
    assert ci.norm_phone("'+13175550101") == "+13175550101"
    assert ci.norm_phone("+44 20 7946 0000") == "+442079460000"
    assert ci.norm_phone("555") == ""
    assert ci.norm_cp_id("AB-1") == "cont-ab-1" and ci.norm_cp_id("cont-x") == "cont-x"
    assert ci.norm_dob("01/02/1980") == "1980-01-02" and ci.norm_dob("1990-03-04T00:00:00") == "1990-03-04"


def test_cp_load_and_idempotent():
    d = _fixture()
    r1 = _run(d)
    assert r1.counts["clients"]["created"] == 3
    assert _q(d, "SELECT phone_e164, dob, email, source FROM clients WHERE carepatron_contact_id='cont-cp-001'") == [
        ("+13175550101", "1980-01-02", "testy@example.test", "carepatron")]
    assert r1.counts["notes"] == {"in": 4, "created": 3, "out": 3, "gap": 1}
    assert r1.counts["appointments"]["out"] == 2 and r1.counts["appointments"]["gap"] == 2
    before = _q(d, "SELECT * FROM clients ORDER BY id") + _q(d, "SELECT id FROM imported_notes") + _q(d, "SELECT id FROM imported_appointments")
    r2 = _run(d)
    assert r2.counts["clients"].get("created", 0) == 0 and r2.counts["clients"]["unchanged"] == 3
    assert r2.counts["notes"].get("created", 0) == 0 and r2.counts["appointments"].get("created", 0) == 0
    after = _q(d, "SELECT * FROM clients ORDER BY id") + _q(d, "SELECT id FROM imported_notes") + _q(d, "SELECT id FROM imported_appointments")
    assert before == after
    nid = hashlib.sha256("cont-cp-001\x1f2026-01-05\x1f# Progress note\nDx F41.1. Sertraline 50 mg.\n".encode()).hexdigest()[:32]
    assert _q(d, f"SELECT content FROM imported_notes WHERE id='{nid}'")[0][0].startswith("# Progress note")
    assert _q(d, "SELECT count(*) FROM signed_notes") == [(0,)] if _q(d, "SELECT name FROM sqlite_master WHERE name='signed_notes'") else True


def test_cp_dedupe_and_review():
    d = _fixture()
    con = sqlite3.connect(d / "db.sqlite"); ci.ensure_schema(con)
    con.execute("INSERT INTO clients (id, display_name, given_name, family_name, email, source) VALUES "
                "('e1','Testy M','Testy','McFakerson','testy@example.test','desk')")
    con.execute("INSERT INTO clients (id, display_name, given_name, family_name, phone_e164, dob, source) VALUES "
                "('p1','B N','Bob','Nobody','+13175550103','1975-05-06','desk')")
    con.execute("INSERT INTO clients (id, display_name, given_name, family_name, source) VALUES "
                "('f1','Adam Placeholder','Adam','Placeholder','desk')")
    con.commit(); con.close()
    r = _run(d)
    assert r.counts["clients"]["matched_email"] == 1 and r.counts["clients"]["matched_phone_dob"] == 1
    assert _q(d, "SELECT carepatron_contact_id, display_name FROM clients WHERE id='e1'") == [("cont-cp-001", "Testy M")]
    assert _q(d, "SELECT count(*) FROM clients") == [(3,)]  # fuzzy Ada not merged nor inserted
    rv = list(csv.DictReader(open(d / "out" / "review.csv")))
    assert [x["ref"] for x in rv] == ["cont-cp-002"] and "f1" in rv[0]["reason"]


def test_cp_dry_run_never_writes_live():
    d = _fixture()
    con = sqlite3.connect(d / "db.sqlite"); ci.ensure_schema(con); con.commit(); con.close()
    before = (d / "db.sqlite").read_bytes()
    r = _run(d, apply=False)
    assert r.counts["clients"]["created"] == 3
    assert (d / "db.sqlite").read_bytes() == before
    assert _q(d, "SELECT count(*) FROM clients") == [(0,)]
    assert "dry run" in (d / "out" / "reconciliation.md").read_text()


def test_cp_reconciliation_lists_gaps():
    d = _fixture(ROWS + [["", "No", "Id", "", "", "", "", "", "", "", "", "", "", ""]])
    _run(d)
    rec = json.loads((d / "out" / "reconciliation.json").read_text())
    assert rec["counts"]["clients"]["in"] == 4 and rec["counts"]["clients"]["out"] == 3
    refs = {(g["type"], g["ref"]) for g in rec["gaps"]}
    assert ("clients", "row4") in refs and ("notes", "(top-level)") in refs and ("appointments", "ap-3") in refs and ("appointments", "blk-1") in refs


def test_cp_extract_mocked_suggestions_only():
    d = _fixture()
    calls = []

    def fake(text):
        calls.append(text)
        return '{"dx_codes": ["F41.1", "bogus"], "medications": [{"name": "sertraline", "dose": "50 mg"}]}', 1000, 100
    r = _run(d, do_extract=True, call=fake)
    assert len(calls) == 3 and r.counts["extract"]["out"] == 3
    rows = list(csv.DictReader(open(d / "out" / "extract_suggestions.csv")))
    assert json.loads(rows[0]["dx_codes"]) == ["F41.1"] and rows[0]["status"] == "needs_review"
    assert _q(d, "SELECT count(*) FROM clients WHERE standing_dx IS NOT NULL") == [(0,)]
    assert ci.validate_extract("not json") is None and ci.validate_extract('{"dx_codes": "x", "medications": []}') is None


def test_cp_clinical_dx_and_meds():
    assert ci.parse_dx("f411, F32.A; U071 F41.1") == (["F41.1", "F32.A"], 4, 1, 1)
    assert ci.split_meds("a 1 mg\n- b 2 mg; c") == ["a 1 mg", "b 2 mg", "c"]
    d = _fixture()
    con = sqlite3.connect(d / "db.sqlite"); ci.ensure_schema(con)
    con.execute("INSERT INTO clients (id, display_name, given_name, family_name, carepatron_contact_id, standing_dx, source) "
                "VALUES ('k1','B N','Bob','Nobody','cont-c0ffee03-0000','[\"F99\"]','desk')")
    con.commit(); con.close()
    r = _run(d)
    assert r.counts["dx"]["would_fill"] == 1 and r.counts["dx"]["already_set"] == 1
    assert _q(d, "SELECT count(*) FROM clients WHERE standing_dx LIKE '%F41.1%'") == [(0,)]  # suggestions only
    sug = list(csv.DictReader(open(d / "out" / "clinical_suggestions.csv")))
    assert {x["field"] for x in sug} == {"standing_dx", "medications"}
    assert _q(d, "SELECT count(*) FROM imported_client_clinical WHERE field='ins_info'") == [(1,)]
    try:
        _run(d, apply=False, apply_clinical_fields=True); assert False
    except ValueError:
        pass
    r = _run(d, apply_clinical_fields=True)
    assert r.counts["dx"]["filled"] == 1
    assert _q(d, "SELECT standing_dx FROM clients WHERE carepatron_contact_id='cont-cp-001'") == [('["F41.1", "F32.A"]',)]
    assert _q(d, "SELECT standing_dx FROM clients WHERE id='k1'") == [('["F99"]',)]  # never overwritten
    r = _run(d, apply_clinical_fields=True)
    assert r.counts["dx"].get("filled", 0) == 0 and r.counts["clinical:medications"]["unchanged"] == 1


def test_cp_card_columns_never_stored():
    d = _fixture()
    _run(d)
    _run(d, apply=False, do_extract=True, call=lambda t: ('{"dx_codes": [], "medications": []}', 1, 1))
    secrets = ["4111111111111111", "5555444433332222", "12/29", "01/30", "cvcQ987", "cvcQ123", "Card Number", "CVC", "Exp Date"]
    blobs = [p.read_text() for p in (d / "out").iterdir()]
    con = sqlite3.connect(d / "db.sqlite")
    for (tbl,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for row in con.execute(f"SELECT * FROM {tbl}"):
            blobs.append(" ".join(str(v) for v in row))
    con.close()
    for b in blobs:
        for s in secrets:
            assert s not in b, f"card data leaked: {s}"


def test_cp_appointments_tz_dedupe_and_blocks():
    assert ci.to_utc("2026-01-05 10:00:00", "America/Chicago") == "2026-01-05T16:00:00+00:00"
    assert ci.to_utc("2026-07-01 09:00:00", "US/Central") == "2026-07-01T14:00:00+00:00"
    assert ci.APPT_TZ == "America/Chicago"
    d = _fixture()
    r = _run(d)
    c = r.counts["appointments"]
    assert c["in"] == 5 and c["out"] == 2 and c["deduped"] == 1 and c["gap"] == 2
    assert c["status_Confirmed"] == 1 and c["status_Cancelled"] == 1
    reasons = {(g["ref"], g["reason"]) for g in r.gaps if g["type"] == "appointments"}
    assert reasons == {("ap-3", "client id not found"), ("blk-1", "no client on event (calendar block)")}
    row = _q(d, "SELECT carepatron_appointment_id, tz, start_utc, lines_json, raw_json FROM imported_appointments WHERE carepatron_appointment_id='ap-1'")
    assert len(row) == 1 and row[0][1] == "America/Chicago" and row[0][2] == "2026-01-05T16:00:00+00:00"
    assert [x["code"] for x in json.loads(row[0][3])] == ["99214", "90833"]
    assert "4111" not in row[0][4] and "Card Number" not in row[0][4]
    assert _run(d).counts["appointments"].get("created", 0) == 0


def test_cp_appointments_practice_tz_and_cross_file_dedupe():
    d = _fixture()
    src = list(csv.reader(open(d / "appts.csv")))
    hdr, overlap = src[0], src[1]
    blank = dict.fromkeys(hdr, "")
    def row(**kw):
        r = dict(blank, **kw); return [r[h] for h in hdr]
    with open(d / "future.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(hdr); w.writerow(overlap)  # overlapping date range: same row again
        w.writerow(row(AppointmentId="ap-9", StartDate="2099-02-10 09:00:00", EndDate="2099-02-10 09:30:00",
                       ClientId="cp-001", Status="Confirmed", Service="Follow-up", TimeZone="America/New_York"))
        w.writerow(row(AppointmentId="ap-8", StartDate="2099-04-01 09:00:00", ClientId="cp-404",
                       Status="Confirmed", TimeZone="America/Chicago"))
    run = lambda: ci.run(db=d / "db.sqlite", clients_csv=d / "clients.csv", notes_dir=None,
                         appts=[d / "appts.csv", d / "future.csv"], pdf_dir=None, out_dir=d / "out", apply=True)
    c = run().counts["appointments"]
    assert c["in"] == 8 and c["in_file1"] == 5 and c["in_file2"] == 3
    assert c["deduped_across_files"] == 1 and c["out"] == 3 and c["future"] == 1 and c["future_month_2099-02"] == 1
    # NY label is reference only: 09:00 Central (CST) = 15:00 UTC
    assert _q(d, "SELECT tz, start_utc FROM imported_appointments WHERE carepatron_appointment_id='ap-9'") == [
        ("America/New_York", "2099-02-10T15:00:00+00:00")]
    assert len(json.loads(_q(d, "SELECT lines_json FROM imported_appointments WHERE carepatron_appointment_id='ap-1'")[0][0])) == 2
    gaps = json.loads((d / "out" / "reconciliation.json").read_text())["gaps"]
    assert {"type": "appointments", "ref": "ap-8", "reason": "client id not found (future)"} in gaps
    before = _q(d, "SELECT * FROM imported_appointments ORDER BY id")
    assert run().counts["appointments"].get("created", 0) == 0
    assert [r[:-1] for r in _q(d, "SELECT * FROM imported_appointments ORDER BY id")] == [r[:-1] for r in before]


def test_cp_new_client_status_mapped_existing_kept():
    d = _fixture()
    _run(d)
    assert _q(d, "SELECT status FROM clients WHERE carepatron_contact_id='cont-c0ffee03-0000'") == [("archived",)]
    con = sqlite3.connect(d / "db.sqlite")
    con.execute("UPDATE clients SET status='inactive', email='other@example.test' WHERE carepatron_contact_id='cont-cp-001'")
    con.commit(); con.close()
    r = _run(d)
    assert r.counts["clients"]["kept_existing_email"] == 1
    assert _q(d, "SELECT status, email FROM clients WHERE carepatron_contact_id='cont-cp-001'") == [("inactive", "other@example.test")]
