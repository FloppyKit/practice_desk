"""Booking links (/b/<token>, no expiry, revocable) + shared Shlink helper. Synthetic only; no network."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app import clients_db, desk_agent, shortlinks, tokens

ROOT = Path(__file__).resolve().parents[1]


def _env(tmp: str) -> None:
    sec = Path(tmp) / "cancel.secret"
    sec.write_text("unit-test-secret")
    os.environ["CANCEL_SECRET_FILE"] = str(sec)
    clients_db.DB_PATH = Path(tmp) / "c.sqlite"
    with clients_db.connect() as con:
        con.execute("INSERT INTO clients (id, display_name, email, phone_e164) VALUES "
                    "('syn1', 'Synth Booker', 'synth@example.invalid', '+15550000009')")


def test_token_valid_tampered_revoked_and_route() -> None:
    from app import main

    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        url = main._book_link("syn1")["url"]
        assert url.startswith("https://book.drmattbrown.com/b/")
        tok = url.rsplit("/", 1)[1]
        for leak in ("Synth", "synth@", "5550000009"):
            assert leak not in url
        p = tokens.verify_book_token(tok)
        assert p["id"] == "syn1" and "x" not in p  # no expiry
        r = main.page_book_token(tok)
        assert r.status_code == 303 and r.headers["location"] == "/follow-up#bt=" + tok
        assert r.headers["cache-control"] == "no-store" and "noindex" in r.headers["x-robots-tag"]
        d = json.loads(main.api_book_prefill(main.BookPrefillRequest(token=tok)).body)
        assert d["ok"] and d["name"] == "Synth Booker" and d["email"] == "synth@example.invalid"
        body, sig = tok.split(".")
        bad = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + sig
        assert main.page_book_token(bad).status_code == 410
        assert main.page_book_token(body + "." + sig[::-1]).status_code == 410
        assert main.page_book_token("garbage").status_code == 410
        clients_db.bump_book_link_version("syn1")
        assert main.page_book_token(tok).status_code == 410  # revoked
        assert json.loads(main.api_book_prefill(main.BookPrefillRequest(token=tok)).body)["ok"] is False
        new = main._book_link("syn1")["url"]
        assert new != url and main.page_book_token(new.rsplit("/", 1)[1]).status_code == 303
        assert main._book_link("syn1")["url"] == new  # stable until revoked


def test_cancel_and_patient_tokens_reject_book_tokens() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        tok = tokens.mint_book_token(client_id="syn1")
        for fn in (tokens.verify_cancel_token, tokens.verify_patient_token):
            try:
                fn(tok)
                raise AssertionError("cross-use accepted")
            except ValueError:
                pass


def test_b_route_cannot_collide() -> None:
    from app import main
    from app.event_store import _SLUG

    paths = [getattr(r, "path", "") for r in main.app.routes]
    assert paths.count("/b/{token}") == 1 and not [p for p in paths if p.startswith("/b/") and p != "/b/{token}"]
    assert paths.index("/b/{token}") < paths.index("/{slug}")
    assert not _SLUG.match("b")  # a service slug can never be "b"


def test_desk_tool_and_email_dry_run() -> None:
    from app import main

    with tempfile.TemporaryDirectory() as tmp:
        _env(tmp)
        out = main._desk_book_link("syn1", send_email=True, dry_run=True)
        assert out["ok"] and out["url"].startswith("https://book.drmattbrown.com/b/")
        assert out["email"] == {"ok": True, "dry_run": True, "sent": False}
        assert main._desk_book_link("nope")["ok"] is False
        seen = []
        res = desk_agent._run_tool("booking_link", {"client_id": "syn1"}, None, None, None,
                                   lambda a: seen.append(a) or main._desk_book_link(a["client_id"], dry_run=True))
        assert res["url"] == out["url"] and seen == [{"client_id": "syn1"}]
        assert any(t["function"]["name"] == "booking_link" for t in desk_agent.TOOLS)


def test_copy_booking_link_buttons_present() -> None:
    tile = (ROOT / "static/js/client-tile.js").read_text()
    desk = (ROOT / "static/desk.html").read_text()
    for src in (tile, desk):
        assert 'id="hm-booklink">Copy booking link' in src and "/api/desk/book-link" in src
        assert src.index('id="hm-pay"') < src.index('id="hm-booklink"')
    assert "replaceState" in (ROOT / "static/js/booker.js").read_text()


def test_cloak_url_generic_title_and_valid_until() -> None:
    sent = {}

    class R:
        def __init__(self, b): self.b = b
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.b

    def opener(req, timeout=0):
        sent.update(json.loads(req.data))
        return R(json.dumps({"shortUrl": "https://go.example.invalid/abc"}).encode())

    os.environ["SHLINK_API_KEY"] = "unit"
    try:
        url, ok = shortlinks.cloak_url("https://pay.example.invalid/x", "pay", valid_until=1800000000, opener=opener)
    finally:
        os.environ.pop("SHLINK_API_KEY")
    assert ok and url == "https://go.example.invalid/abc"
    assert sent["title"] == "Payment link" and sent["validUntil"].startswith("2027-01-15") and not sent["crawlable"]
    assert shortlinks.cloak_url("http://insecure", "pay")[1] is False
