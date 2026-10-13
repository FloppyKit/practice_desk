"""Scribe draft queue — synthetic strings only."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app import scribe_drafts


def test_put_list_get_ack():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SCRIBE_DRAFTS_DB"] = str(Path(tmp) / "scribe_drafts.sqlite")
        row = scribe_drafts.put(
            note="Patient slept better. Continue sertraline 50mg.",
            transcript="um patient slept better continue sertraline",
            title="Visit note",
            template="blank",
            name="Jane Demo",
            duration_minutes=20,
        )
        assert row["id"]
        pending = scribe_drafts.list_pending()
        assert len(pending) == 1
        assert pending[0]["has_body"] is True
        assert "note" not in pending[0]
        assert "transcript" not in pending[0]
        full = scribe_drafts.get(row["id"])
        assert full and "sertraline" in full["note"]
        assert scribe_drafts.ack(row["id"]) is True
        assert scribe_drafts.list_pending() == []
        assert scribe_drafts.ack(row["id"]) is False


def test_put_requires_body():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SCRIBE_DRAFTS_DB"] = str(Path(tmp) / "scribe_drafts.sqlite")
        try:
            scribe_drafts.put(note="", transcript="")
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
