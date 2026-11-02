"""Calendar reminders: synthetic only, no network, nothing sent."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from app import cal_reminders as cr

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=cr.TZ)


def _row(t, start, **kw):
    r = {"ev": cr._h("e1"), "event_id": "e1", "start": start.isoformat(), "match": "matched", "title": "Visit",
         "when_label": "x", "duration": 30, "event_type": "", "name": "Synth", "email": "s@example.invalid",
         "phone": "+15550000009", "consent": True, "type": t, "channel": "email" if t.startswith("email") else "sms",
         "send_at": (start - cr.WINDOWS[t][0]).isoformat(), "skip": ""}
    r.update(kw)
    r["due"] = not r["skip"] and datetime.fromisoformat(r["send_at"]) <= NOW
    return r


def test_dry_run_by_default_and_no_pii_in_log(monkeypatch=None):
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BOOKER_LOG_DIR"] = tmp
        os.environ.pop("REMINDERS_SEND", None)
        sent = []
        cr.deliver = lambda r: sent.append(r) or {"ok": True}
        start = NOW + timedelta(minutes=20)
        rows = [_row("sms_30m", start), _row("email_24h", NOW + timedelta(hours=30))]
        st = cr.tick(NOW, rows=rows)
        st2 = cr.tick(NOW, rows=rows)
        assert not st["sending"] and sent == [] and st["due"] == 1
        txt = (Path(tmp) / "reminders" / "dryrun-20261102.jsonl").read_text()
        assert len(txt.splitlines()) == 2  # deduped across ticks
        for leak in ("Synth", "example.invalid", "5550000009", "e1\""):
            assert leak not in txt


def test_enabled_sends_once_and_moved_visit_gets_new_key():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BOOKER_LOG_DIR"] = tmp
        (Path(tmp) / "reminders").mkdir()
        (Path(tmp) / "reminders" / "ENABLED").write_text("")
        sent = []
        cr.deliver = lambda r: sent.append(r["type"]) or {"ok": True, "request_id": "x"}
        start = NOW + timedelta(minutes=20)
        cr.tick(NOW, rows=[_row("sms_30m", start)])
        cr.tick(NOW, rows=[_row("sms_30m", start)])
        assert sent == ["sms_30m"]
        cr.tick(NOW, rows=[_row("sms_30m", start + timedelta(minutes=5))])  # rescheduled
        assert sent == ["sms_30m", "sms_30m"]
        cr.tick(NOW, rows=[_row("sms_30m", start, skip="no sms consent")])
        assert len(sent) == 2


def test_bare_name():
    assert cr._bare("Follow-up · Jane Q (30 min)") == "Jane Q"
    assert cr._bare("[Telehealth] Follow up: Jane Q") == "Jane Q"
    assert cr._candidates("Jane Q - 30 Min Follow Up") == ["Jane Q"]
    assert cr._candidates("Jane Q - 60 Min New Patient Visit")[0] == "Jane Q"


def test_late_skip_and_opt_out_rules():
    assert cr.LATE_SKIP == timedelta(hours=2) and cr.ASSUME_SMS_CONSENT
