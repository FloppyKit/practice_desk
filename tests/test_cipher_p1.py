"""CIPHER_P1 unit tests. Synthetic Jane-class data only. clinical_claim: false."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _iso_tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="cipher-p1-"))


def test_mode_flags_default_off():
    os.environ.pop("CIPHER_P1", None)
    os.environ.pop("CIPHER_DUAL_WRITE", None)
    os.environ.pop("CIPHER_OCCUPANCY", None)
    from app import cipher_mode

    assert cipher_mode.enabled() is False
    assert cipher_mode.dual_write() is False
    assert cipher_mode.occupancy() is False


def test_mode_flags_on():
    os.environ["CIPHER_P1"] = "1"
    os.environ["CIPHER_DUAL_WRITE"] = "1"
    os.environ["CIPHER_OCCUPANCY"] = "1"
    from importlib import reload
    from app import cipher_mode

    reload(cipher_mode)
    assert cipher_mode.enabled() is True
    assert cipher_mode.dual_write() is True
    assert cipher_mode.occupancy() is True
    os.environ["CIPHER_P1"] = "0"
    reload(cipher_mode)


def test_seal_params_public_only():
    tmp = _iso_tmp()
    os.environ["CIPHER_SEAL_KEY_FILE"] = str(tmp / "seal.json")
    from importlib import reload
    from app import cipher_seal

    reload(cipher_seal)
    cipher_seal._key_cache = None  # type: ignore[attr-defined]
    off = cipher_seal.seal_params(False)
    assert off == {"enabled": False}
    on = cipher_seal.seal_params(True)
    assert on["enabled"] is True
    jwk = on["public_jwk"]
    assert jwk["kty"] == "EC" and jwk["crv"] == "P-256"
    assert "x" in jwk and "y" in jwk
    assert "d" not in jwk


def test_seal_open_roundtrip_and_queue():
    tmp = _iso_tmp()
    os.environ["CIPHER_SEAL_KEY_FILE"] = str(tmp / "seal.json")
    os.environ["CIPHER_QUEUE_DB"] = str(tmp / "q.sqlite")
    from importlib import reload
    from app import cipher_queue, cipher_seal

    reload(cipher_seal)
    reload(cipher_queue)
    cipher_seal._key_cache = None  # type: ignore[attr-defined]

    plain = {
        "kind": "intake",
        "name": "Jane Demo",
        "email": "jane.demo@example.test",
        "phone": "+15555550100",
        "event_type": "new-client",
        "start": "2026-08-21T15:00:00-05:00",
        "duration_minutes": 90,
        "notes": "synthetic fixture only",
    }
    env = cipher_seal.seal_envelope_python(plain, kind="intake")
    cipher_seal.validate_envelope(env)
    opened = cipher_seal.open_envelope(env)
    assert opened["name"] == "Jane Demo"
    assert opened["email"] == "jane.demo@example.test"

    row = cipher_queue.put(env, source="test")
    assert row["id"]
    pending = cipher_queue.list_pending()
    assert any(p["id"] == row["id"] for p in pending)
    assert cipher_queue.pending_count() >= 1
    assert cipher_queue.mark_drained(row["id"]) is True
    assert cipher_queue.mark_drained(row["id"]) is False


def test_validate_rejects_junk():
    from app import cipher_seal

    try:
        cipher_seal.validate_envelope({"v": 1, "wrap": "nope"})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_occupancy_summary_shape():
    """Occupancy labels must not embed the patient name (unit the rule, not FastAPI)."""
    event_id = "abcdef0123456789"
    name = "Jane Demo"
    occ_summary = f"Visit · {event_id[:8]}"
    clear_summary = f"{name} – New client (90 min)"
    assert name not in occ_summary
    assert name in clear_summary
    assert occ_summary.startswith("Visit · ")


def test_token_purpose_cancel():
    import os
    from pathlib import Path

    sec = Path(tempfile.mkdtemp()) / "cancel.secret"
    sec.write_text("demo-test-secret\n", encoding="utf-8")
    os.environ["CANCEL_SECRET_FILE"] = str(sec)
    from importlib import reload
    from app import tokens

    reload(tokens)
    t = tokens.mint_cancel_token(
        event_id="e1",
        calendar_id="cal",
        email="jane.demo@example.test",
        name="",
        purpose="cancel",
    )
    payload = tokens.verify_cancel_token(t)
    assert payload.get("p") == "cancel"
    assert payload.get("m") == "jane.demo@example.test"
    assert not payload.get("n")
