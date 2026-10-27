"""AVS: letterhead PDF, signed_notes record, prefill, 30-day avs share links, no-xAI draft.
Fake Drive, fake sends. No network."""
from __future__ import annotations

import base64
import json
from contextlib import contextmanager

from tests.test_billing import _desk

SCRIBE_NOTE = """## SOAP (draft · ~4 min)
**Subjective**
CHART-ONLY subjective text.

**Objective**
—

**Assessment**
- Anxiety: stable

**Plan**
- Follow-up: In 6 weeks

## After Visit Summary
Dr. Brown’s notes from today’s visit

Meet again:
In 6 weeks

Medications:
• Lexapro 10mg every morning

Supplements:
• Start NAC 600mg - 2 caps twice a day with food
  N-acetyl Cysteine (NAC) works as an anti-inflammatory and antioxidant in the brain.

Other recommendations:
• Keep your phone out of the bedroom."""


class _Resp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@contextmanager
def _avs_desk():
    with _desk(letterhead=True) as (main, fake, drive):
        calls: list[str] = []
        sends: list[dict] = []
        saved = (main.urlopen, main._bill_send)

        def urlopen(req, timeout=0):
            url = req.full_url
            calls.append(url)
            assert "x.ai" not in url, "xAI must never be called"
            if url.endswith("/drive/save"):
                p = json.loads(req.data.decode())
                drive[p["path"]] = base64.b64decode(p["b64"])
                return _Resp(b'{"ok": true}')
            if url == main.SCRIBE_NOTE_URL:
                return _Resp(json.dumps({"note": SCRIBE_NOTE, "note_engine": "bedrock-amazon.nova-pro-v1"}).encode())
            raise AssertionError("unexpected url " + url)

        def bill_send(**kw):
            sends.append(kw)
            return {"email": {"ok": True, "sent": False}, "sms": {"ok": True, "sent": False}}

        from urllib.parse import unquote

        folders = {"/", "/Clients", "/Clients/Patient, Unit", "/Clients/Patient, Unit/Chart",
                   "/Clients/Patient, Unit/Chart/Visit Notes"}
        mkdirs: list[str] = []

        def notify_get(path):
            target = unquote(path.split("path=", 1)[1])
            if not path.startswith("/drive/list"):
                return 200, drive.get(target, b""), "application/pdf"
            if target not in folders:
                return 502, b'{"ok": false}', "application/json"
            pre = target.rstrip("/") + "/"
            items = [{"name": f[len(pre):], "type": "folder"} for f in folders if f.startswith(pre) and "/" not in f[len(pre):]]
            items += [{"name": f[len(pre):], "type": "file"} for f in drive if f.startswith(pre) and "/" not in f[len(pre):]]
            return 200, json.dumps({"ok": True, "items": items}).encode(), "application/json"

        def mkdir(path):
            assert path not in folders, "mkdir of an existing folder: " + path
            assert path.rsplit("/", 1)[0] in folders, "parent missing: " + path
            folders.add(path)
            mkdirs.append(path)

        main.urlopen, main._bill_send = urlopen, bill_send
        main._notify_get, main._drive_mkdir = notify_get, mkdir
        main._test_folders, main._test_mkdirs = folders, mkdirs
        try:
            yield main, drive, calls, sends
        finally:
            main.urlopen, main._bill_send = saved


def _fields():
    from app.note_parse import parse_structured

    return parse_structured(SCRIBE_NOTE)


def _sign(main):
    from fastapi import BackgroundTasks

    f = _fields()
    req = main.DeskNoteSignRequest(client_id="c-unit", dos="2026-10-26", template="avs", name="Unit Patient",
                                   dob="1990-01-01", duration="30", values={}, **f)
    return main.api_desk_note_sign(req, BackgroundTasks())


def test_parse_structured_scribe_note() -> None:
    f = _fields()
    assert f["return_visit"] == "In 6 weeks"
    assert f["medications"] == "• Lexapro 10mg every morning"
    assert f["supplements"].splitlines()[1].startswith("  N-acetyl")
    assert f["objective"] == "" and f["subjective"].startswith("CHART-ONLY")


def test_sign_letterhead_pdf_patient_copy_and_record() -> None:
    with _avs_desk() as (main, drive, calls, sends):
        out = _sign(main)
        assert out["shareable"] and out["note_id"] and out["chart_path"].endswith("avs chart.pdf")
        avs = drive[out["path"]]
        assert avs.startswith(b"%PDF") and b"KS-UNIT" in avs and b"IL-UNIT" in avs and b"MO-UNIT" in avs
        assert b"Meet again" in avs and b"CHART-ONLY" not in avs  # S/O/A never on the patient AVS
        assert avs.index(b"Meet again") < avs.index(b"Medications")
        assert b"CHART-ONLY" in drive[out["chart_path"]]
        assert b"\x95 Lexapro" in avs  # bullet in WinAnsi, not "?"
        from app import signed_notes

        rec = signed_notes.get(out["note_id"])
        assert rec["avs_path"] == out["path"] and rec["fields"]["medications"].startswith("• Lexapro")
        assert not any("x.ai" in c for c in calls)


def test_prefill_from_last_signed_avs() -> None:
    with _avs_desk() as (main, drive, calls, sends):
        assert main.api_desk_note_last_avs(client_id="c-unit")["note"] is None
        out = _sign(main)
        got = main.api_desk_note_last_avs(client_id="c-unit")
        assert got["note"]["id"] == out["note_id"] and got["share"] is None
        assert set(got["prefill"]) == {"return_visit", "medications", "supplements", "recommendations"}
        assert "subjective" not in got["prefill"]


def test_avs_share_open_rotate_revoke() -> None:
    from app import billing

    with _avs_desk() as (main, drive, calls, sends):
        out = _sign(main)
        sh = main.api_desk_note_avs_share(main.AvsShareRequest(note_id=out["note_id"], to_email="t@example.invalid"))
        assert sh["url"].startswith(main.PUBLIC_BASE + "/avs/")
        assert sends[-1]["kind"] == "avs" and sends[-1]["view"]["email"] == "t@example.invalid"
        assert "30 days" in sends[-1]["sms_body"]
        tok = sh["url"].rsplit("/", 1)[1]
        r = main.public_avs_share(tok)
        assert r.status_code == 200 and r.body.startswith(b"%PDF") and r.headers["cache-control"] == "no-store"
        assert billing.active_share(out["note_id"], "avs")["hits"] == 1
        # an avs token is not a superbill token and vice versa
        assert main.public_superbill_share(tok).status_code == 404
        # 30-day max
        exp = billing.verify_share(tok, kind="avs")["expires_at"]
        import time
        assert exp - time.time() <= billing.SHARE_TTL_SECONDS + 5
        # re-send rotates: old link off
        sh2 = main.api_desk_note_avs_share(main.AvsShareRequest(note_id=out["note_id"], send_email=False, send_sms=False))
        assert sh2["replaced"] == 1 and main.public_avs_share(tok).status_code == 410
        tok2 = sh2["url"].rsplit("/", 1)[1]
        assert main.public_avs_share(tok2).status_code == 200
        assert main.api_desk_note_avs_revoke(main.AvsRevokeRequest(note_id=out["note_id"]))["revoked"] == 1
        assert main.public_avs_share(tok2).status_code == 410
        assert main.public_avs_share(tok2[:-2] + "xx").status_code == 404


def test_draft_never_calls_xai() -> None:
    with _avs_desk() as (main, drive, calls, sends):
        got = main.api_desk_note_draft(main.DeskNoteDraftRequest(transcript=SCRIBE_NOTE))
        assert got["engine"] == "scribe-structured" and got["fields"]["return_visit"] == "In 6 weeks"
        assert calls == []  # structured scribe note: no model call at all
        got = main.api_desk_note_draft(main.DeskNoteDraftRequest(transcript="doctor: how is sleep? patient: better on lexapro."))
        assert got["engine"].startswith("bedrock") and calls == [main.SCRIBE_NOTE_URL]


def _set_chart(path: str) -> None:
    from app import clients_db

    row = clients_db.get_by_id("c-unit")
    row["drive_folder_path"] = path
    clients_db.upsert(row)


def test_sign_creates_missing_client_tree() -> None:
    with _avs_desk() as (main, drive, calls, sends):
        _set_chart("/Clients/New, Person/Chart")
        out = _sign(main)
        assert main._test_mkdirs == [
            "/Clients/New, Person", "/Clients/New, Person/Billing", "/Clients/New, Person/Chart",
            "/Clients/New, Person/Chart/Consents", "/Clients/New, Person/Chart/Labs",
            "/Clients/New, Person/Chart/Letters", "/Clients/New, Person/Chart/Orders",
            "/Clients/New, Person/Chart/Visit Notes"]
        assert out["path"].startswith("/Clients/New, Person/Chart/Visit Notes/2026-10-26 ")


def test_sign_existing_chart_adds_nothing_and_matches_case() -> None:
    with _avs_desk() as (main, drive, calls, sends):
        main._test_folders.discard("/Clients/Patient, Unit/Chart/Visit Notes")
        main._test_folders.add("/Clients/Patient, Unit/Chart/visit NOTES")
        out = _sign(main)
        assert main._test_mkdirs == []  # existing chart: no Consents etc. added, no duplicate visit folder
        assert out["path"].startswith("/Clients/Patient, Unit/Chart/visit NOTES/")


def test_sign_existing_chart_without_visit_notes_gets_only_that() -> None:
    with _avs_desk() as (main, drive, calls, sends):
        main._test_folders.discard("/Clients/Patient, Unit/Chart/Visit Notes")
        _sign(main)
        assert main._test_mkdirs == ["/Clients/Patient, Unit/Chart/Visit Notes"]


def test_same_day_resign_never_overwrites() -> None:
    with _avs_desk() as (main, drive, calls, sends):
        a, b, c = _sign(main), _sign(main), _sign(main)
        assert a["path"].endswith(" avs.pdf") and b["path"].endswith(" avs-2.pdf") and c["path"].endswith(" avs-3.pdf")
        assert b["chart_path"].endswith(" avs-2 chart.pdf")
        assert len({a["path"], b["path"], c["path"], a["chart_path"], b["chart_path"], c["chart_path"]}) == 6


def test_sign_never_creates_archive_or_when_drive_down() -> None:
    from fastapi import HTTPException

    with _avs_desk() as (main, drive, calls, sends):
        _set_chart("/Client files/Gone, Person/Chart")
        try:
            _sign(main)
            raise AssertionError("expected 400")
        except HTTPException as e:
            assert e.status_code == 400
        _set_chart("/Clients/New, Person/Chart")
        main._test_folders.clear()
        try:
            _sign(main)
            raise AssertionError("expected 502")
        except HTTPException as e:
            assert e.status_code == 502
        assert main._test_mkdirs == []
