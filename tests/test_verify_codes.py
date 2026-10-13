"""Account codes. No name/DOB match. No chart fields in the public start response."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app import clients_db
from app.patients import verify_patient
from app.verify_codes import confirm_code, start_verify


def test_name_and_dob_do_not_verify() -> None:
    result = verify_patient(
        full_name="Jane Demo",
        first_name="Jane",
        last_name="Demo",
        dob="1990-01-02",
    )
    assert result == {"ok": False, "reason": "need_email_or_phone"}
    assert "verify_token" not in result
    assert "client_id" not in result
    assert "email" not in result


def test_code_to_email_on_file_then_token() -> None:
    sent: list[tuple[str, str, str]] = []

    def sender(channel, destination, code):
        sent.append((channel, destination, code))
        return {"ok": True}

    old_db = clients_db.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as raw:
            secret = Path(raw) / "cancel.secret"
            secret.write_text("cancel-test-secret")
            os.environ["BOOKER_LOG_DIR"] = raw
            os.environ["VERIFY_CODE_SECRET"] = "test-secret"
            os.environ["CANCEL_SECRET_FILE"] = str(secret)
            clients_db.DB_PATH = Path(raw) / "clients.sqlite"
            clients_db.upsert(
                {
                    "id": "c-test",
                    "display_name": "Jane Demo",
                    "email": "jane@example.com",
                    "phone_e164": "+15555550100",
                    "dob": "1990-01-02",
                    "status": "active",
                }
            )
            pending = start_verify(email="jane@example.com", sender=sender)
            assert pending == {
                "ok": True,
                "delivery": "pending",
                "message": "If that email or mobile is on the account, a code is on the way.",
            }
            assert "client_id" not in pending
            assert "name" not in pending
            assert sent and sent[0][0] == "email" and sent[0][1] == "jane@example.com"
            bad = confirm_code(email="jane@example.com", code="000000")
            assert bad == {"ok": False, "reason": "invalid_code"}
            good = confirm_code(email="jane@example.com", code=sent[0][2])
            assert good["ok"] is True
            assert good.get("verify_token")
            assert "email" not in good
            assert "name" not in good
            assert "client_id" not in good
            missed = start_verify(email="nobody@example.com", sender=sender)
            assert missed == pending
            assert len(sent) == 1
    finally:
        clients_db.DB_PATH = old_db
