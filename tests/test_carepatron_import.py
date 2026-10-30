"""Carepatron importer. SYNTHETIC data only (fake names); Bedrock mocked."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from app import carepatron_import as ci

CSV_HDR = ["Id", "First Name", "Last Name", "Preferred Name", "Email", "Phone Number", "Date of Birth", "Status"]
ROWS = [
    ["cp-001", "Testy", "McFakerson", "", "TESTY@example.test", "(317) 555-0101", "01/02/1980", "Active"],
    ["cp-002", "Ada", "Placeholder", "Addie", "ada@example.test", "317.555.0102", "1990-03-04", "Active"],
    ["cp-003", "Bob", "Nobody", "", "", "317-555-0103", "5/6/1975", "Active"],
]


def _fixture(rows=ROWS):
    d = Path(tempfile.mkdtemp(prefix="cp-test-"))
    with open(d / "clients.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(CSV_HDR); w.writerows(rows)
    notes = d / "notes"; (notes / "cp-001").mkdir(parents=True)
    (notes / "cp-001" / "2026-01-05 Progress note.md").write_text("# Progress note\nDx F41.1. Sertraline 50 mg.\n")
    (notes / "Ada Placeholder 2026-02-01.md").write_text("# Intake\nSynthetic text.\n")
    (notes / "Unknown Person 2026-02-02.md").write_text("orphan\n")
    with open(d / "appts.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["Id", "Client Id", "Client", "Start", "End", "Service", "Status"])
        w.writerow(["ap-1", "cp-001", "", "2026-01-05T10:00", "2026-01-05T10:30", "Follow-up", "Done"])
        w.writerow(["ap-2", "", "Ada Placeholder", "2026-02-01T09:00", "", "Intake", "Done"])
        w.writerow(["ap-3", "cp-999", "", "2026-02-03T09:00", "", "Intake", "Done"])
    return d


def _run(d, apply=True, **kw):
    return ci.run(db=d / "db.sqlite", clients_csv=d / "clients.csv", notes_dir=d / "notes",
                  appts=d / "appts.csv", pdf_dir=None, out_dir=d / "out", apply=apply, **kw)


def _q(d, sql):
    con = sqlite3.connect(d / "db.sqlite"); r = con.execute(sql).fetchall(); con.close(); return r


def test_cp_normalizers():
    assert ci.norm_phone("(317) 555-0101") == "+13175550101"
    assert ci.norm_phone("+44 20 7946 0000") == "+442079460000"
    assert ci.norm_phone("555") == ""
    assert ci.norm_dob("01/02/1980") == "1980-01-02" and ci.norm_dob("1990-03-04T00:00:00") == "1990-03-04"


def test_cp_load_and_idempotent():
    d = _fixture()
    r1 = _run(d)
    assert r1.counts["clients"]["created"] == 3
    assert _q(d, "SELECT phone_e164, dob, email, source FROM clients WHERE carepatron_contact_id='cp-001'") == [
        ("+13175550101", "1980-01-02", "testy@example.test", "carepatron")]
    assert r1.counts["notes"] == {"in": 3, "created": 2, "out": 2, "gap": 1}
    assert r1.counts["appointments"]["out"] == 2 and r1.counts["appointments"]["gap"] == 1
    before = _q(d, "SELECT * FROM clients ORDER BY id") + _q(d, "SELECT id FROM imported_notes") + _q(d, "SELECT id FROM imported_appointments")
    r2 = _run(d)
    assert r2.counts["clients"].get("created", 0) == 0 and r2.counts["clients"]["unchanged"] == 3
    assert r2.counts["notes"].get("created", 0) == 0 and r2.counts["appointments"].get("created", 0) == 0
    after = _q(d, "SELECT * FROM clients ORDER BY id") + _q(d, "SELECT id FROM imported_notes") + _q(d, "SELECT id FROM imported_appointments")
    assert before == after
    nid = hashlib.sha256("cp-001\x1f2026-01-05\x1f# Progress note\nDx F41.1. Sertraline 50 mg.\n".encode()).hexdigest()[:32]
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
    assert _q(d, "SELECT carepatron_contact_id, display_name FROM clients WHERE id='e1'") == [("cp-001", "Testy M")]
    assert _q(d, "SELECT count(*) FROM clients") == [(3,)]  # fuzzy Ada not merged nor inserted
    rv = list(csv.DictReader(open(d / "out" / "review.csv")))
    assert [x["ref"] for x in rv] == ["cp-002"] and "f1" in rv[0]["reason"]


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
    d = _fixture(ROWS + [["", "No", "Id", "", "", "", "", ""]])
    _run(d)
    rec = json.loads((d / "out" / "reconciliation.json").read_text())
    assert rec["counts"]["clients"]["in"] == 4 and rec["counts"]["clients"]["out"] == 3
    refs = {(g["type"], g["ref"]) for g in rec["gaps"]}
    assert ("clients", "row4") in refs and ("notes", "Unknown Person 2026-02-02.md") in refs and ("appointments", "ap-3") in refs


def test_cp_extract_mocked_suggestions_only():
    d = _fixture()
    calls = []

    def fake(text):
        calls.append(text)
        return '{"dx_codes": ["F41.1", "bogus"], "medications": [{"name": "sertraline", "dose": "50 mg"}]}', 1000, 100
    r = _run(d, do_extract=True, call=fake)
    assert len(calls) == 2 and r.counts["extract"]["out"] == 2
    rows = list(csv.DictReader(open(d / "out" / "extract_suggestions.csv")))
    assert json.loads(rows[0]["dx_codes"]) == ["F41.1"] and rows[0]["status"] == "needs_review"
    assert _q(d, "SELECT count(*) FROM clients WHERE standing_dx IS NOT NULL") == [(0,)]
    assert ci.validate_extract("not json") is None and ci.validate_extract('{"dx_codes": "x", "medications": []}') is None
