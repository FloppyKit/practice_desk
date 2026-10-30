"""Read-only Carepatron import views. SYNTHETIC data only."""
from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException


@contextmanager
def _db(with_tables: bool):
    from app import carepatron_import as ci, clients_db, main
    saved = (clients_db.DB_PATH, main.REMIND_SECRET)
    with tempfile.TemporaryDirectory() as raw:
        clients_db.DB_PATH = Path(raw) / "clients.sqlite"
        main.REMIND_SECRET = "unit-staff"
        con = sqlite3.connect(clients_db.DB_PATH)
        clients_db._init(con)
        con.execute("INSERT INTO clients (id, display_name, source) VALUES ('c1','Testy McFakerson','carepatron')")
        if with_tables:
            ci.ensure_schema(con)
            con.execute("INSERT INTO imported_notes VALUES ('n1','c1','cont-x','2026-01-05','f/Progress - 2026-01-05.md',"
                        "'# Progress note\n<script>alert(1)</script>','t')")
            con.execute("INSERT INTO imported_notes VALUES ('n2','c1','cont-x','2026-03-01','f/b.md','# Later note','t')")
            con.execute("INSERT INTO imported_appointments (id,client_id,carepatron_contact_id,carepatron_appointment_id,start_at,tz,"
                        "start_utc,service,status,duration_min,lines_json) VALUES "
                        "('a1','c1','cont-x','ap1','2026-01-05 10:00:00','America/Chicago','2026-01-05T16:00:00+00:00','Follow-up','Confirmed',30,"
                        "'[{\"service\":\"Follow-up\",\"code\":\"99214\",\"price\":\"200\",\"invoice\":\"\",\"dx\":\"\"}]'),"
                        "('a2','c1','cont-x','ap2','2099-02-10 09:00:00','America/Chicago','2099-02-10T15:00:00+00:00','Follow-up','Confirmed',30,'[]')")
            con.execute("INSERT INTO imported_client_clinical VALUES ('k1','c1','supplements','Fish oil','s','t'),"
                        "('k2','c1','medications','sertraline 50 mg','s','t')")
        con.commit(); con.close()
        try:
            yield main
        finally:
            clients_db.DB_PATH, main.REMIND_SECRET = saved


def test_imported_client_view_present():
    with _db(True) as main:
        d = main.api_desk_client_imported(secret="unit-staff", id="c1")
        assert d["available"] and [n["dos"] for n in d["notes"]] == ["2026-03-01", "2026-01-05"]
        assert d["notes"][1]["title"] == "Progress note"
        assert [c["field"] for c in d["clinical"]] == ["medications", "supplements"]
        up, past = d["appointments"]["upcoming"], d["appointments"]["past"]
        assert len(up) == 1 and up[0]["when_label"] == "Tue Feb 10, 2099 9:00 AM CT" and up[0]["read_only"]
        assert past[0]["lines"][0]["code"] == "99214" and past[0]["when_label"].endswith("10:00 AM CT")
        w = main.api_desk_imported_week(secret="unit-staff", start="2099-02-09", days=7)
        assert w["available"] and [i["client_name"] for i in w["items"]] == ["Testy McFakerson"]
        assert main.api_desk_imported_week(secret="unit-staff", start="2099-03-09", days=7)["items"] == []


def test_imported_views_hidden_without_tables():
    with _db(False) as main:
        assert main.api_desk_client_imported(secret="unit-staff", id="c1") == {"ok": True, "available": False}
        assert main.api_desk_imported_week(secret="unit-staff", start="2099-02-09")["available"] is False


def test_imported_views_require_staff():
    with _db(True) as main:
        for fn, kw in ((main.api_desk_client_imported, {"id": "c1"}), (main.api_desk_imported_week, {"start": "2099-02-09"})):
            for bad in ("", "wrong"):
                try:
                    fn(secret=bad, **kw); raise AssertionError("no auth")
                except HTTPException as e:
                    assert e.status_code == 401


def test_imported_routes_staff_only_and_ui_escapes():
    from app import main
    paths = {r.path for r in main.app.routes if "imported" in getattr(r, "path", "")}
    assert paths == {"/api/desk/client/imported", "/api/desk/imported/week"}
    html = (Path(__file__).resolve().parents[1] / "static" / "desk.html").read_text()
    seg = html[html.index("async function loadImported("):html.index("function renderHome(")]
    assert "esc(n.body)" in seg and "esc(x.value)" in seg
    low = seg.lower()
    for word in ("method:", "post", "/api/desk/bill", "remind", "charge", "sms", "/api/book"):
        assert word not in low, word
