"""Spruce-Mac SMS queue. No network. No Twilio."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.notify import send_sms
from app.sms_bridge import ack, claim, enqueue, sms_settled


def _use_tmp(tmp: Path) -> None:
    os.environ["BOOKER_LOG_DIR"] = str(tmp)
    os.environ["SMS_BRIDGE"] = "spruce_mac"
    os.environ.pop("SMS_BRIDGE_SECRET", None)


def test_enqueue_and_duplicate() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        _use_tmp(Path(raw))
        first = enqueue(
            to="+15555550100",
            body="hello",
            kind="book_link",
            source="voice",
            idempotency_key="voice:1:book_link",
        )
        assert first["queued"] is True
        again = enqueue(
            to="+15555550100",
            body="hello",
            kind="book_link",
            source="voice",
            idempotency_key="voice:1:book_link",
        )
        assert again["already"] is True
        assert again["queued"] is True
        pending = list((Path(raw) / "sms-bridge" / "pending").glob("*.json"))
        assert len(pending) == 1


def test_claim_skips_future_and_expires() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        _use_tmp(Path(raw))
        later = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        enqueue(
            to="+15555550101",
            body="later",
            kind="join",
            idempotency_key="later",
            not_after=later,
        )
        # push not_before into the future by rewriting the file
        path = next((Path(raw) / "sms-bridge" / "pending").glob("*.json"))
        import json

        row = json.loads(path.read_text())
        row["not_before"] = later
        path.write_text(json.dumps(row))
        assert claim() is None

        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        enqueue(
            to="+15555550102",
            body="stale",
            kind="remind_30m",
            idempotency_key="stale",
            not_after=past,
        )
        assert claim() is None
        expired = list((Path(raw) / "sms-bridge" / "expired").glob("*.json"))
        assert len(expired) == 1


def test_ack_sent_settles() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        _use_tmp(Path(raw))
        queued = enqueue(
            to="+15555550103",
            body="go",
            kind="join",
            idempotency_key="go",
        )
        job = claim()
        assert job and job["to"] == "+15555550103"
        assert sms_settled(queued) is False
        done = ack(job["id"], "sent")
        assert done["settled"] is True
        again = enqueue(
            to="+15555550103",
            body="go",
            kind="join",
            idempotency_key="go",
        )
        assert again["sent"] is True
        assert sms_settled(again) is True


def test_uncertain_does_not_retry() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        _use_tmp(Path(raw))
        enqueue(to="+15555550104", body="maybe", kind="join", idempotency_key="maybe")
        job = claim()
        assert job
        ack(job["id"], "uncertain")
        assert claim() is None
        again = enqueue(
            to="+15555550104",
            body="maybe",
            kind="join",
            idempotency_key="maybe",
        )
        assert again["settled"] is True
        assert again.get("queued") is not True


def test_send_sms_queues_without_twilio() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        _use_tmp(Path(raw))
        result = send_sms(
            "5555550105",
            "Dr. Matt Brown: Book a visit at https://book.psycharts.org",
            kind="book_link",
            source="voice",
            idempotency_key="unit:book",
            template="book_link",
        )
        assert result["queued"] is True
        assert result["channel"] == "sms_bridge"
        os.environ["SMS_BRIDGE"] = ""
        skipped = send_sms("5555550105", "nope")
        assert skipped["skipped"] is True
