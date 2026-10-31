"""Note carry-forward + transcript update. Synthetic; no network."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from app import clients_db, note_carry


def test_carry_skips_visit_specific_and_prefers_signed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        clients_db.DB_PATH = Path(tmp) / "c.sqlite"
        with clients_db.connect() as con:
            con.execute("CREATE TABLE signed_notes (id TEXT, client_id TEXT, dos TEXT, template TEXT, fields_json TEXT,"
                        " avs_path TEXT, avs_name TEXT, chart_path TEXT, signed_at TEXT)")
            con.execute("CREATE TABLE imported_notes (id TEXT, client_id TEXT, dos TEXT, content TEXT)")
            con.execute("INSERT INTO imported_notes VALUES ('i1','c1','2026-01-01','Plan: old plan')")
        assert note_carry.carry("c1")["source"] == "imported"
        with clients_db.connect() as con:
            con.execute("INSERT INTO signed_notes VALUES ('n1','c1','2026-09-01','avs',?,'','','','2026-09-01T10:00:00')",
                        (json.dumps({"dos": "2026-09-01", "subjective": "x", "interval_update": "y",
                                     "medications": "synthetic med 1", "plan": "synthetic plan"}),))
        c = note_carry.carry("c1")
        assert c["source"] == "signed" and c["dos"] == "2026-09-01"
        assert c["fields"] == {"medications": "synthetic med 1", "plan": "synthetic plan"}
        assert note_carry.carry("nobody") is None


def test_update_returns_only_changed_known_sections() -> None:
    fake = lambda _s, _t: json.dumps({"changed": {"plan": "new plan", "medications": "same", "dos": "2020-01-01",
                                                  "made_up": "z"}})
    out = note_carry.update_from_transcript({"plan": "old plan", "medications": "same", "dos": "2026-10-30"},
                                            "synthetic transcript text long enough", call=fake)
    assert out == {"plan": "new plan"}
