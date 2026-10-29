"""Client copies of signed intake documents: sent once intake is complete, re-sent on a newer signature,
served via a 30-day revocable docs link scoped to that client. Fake Drive / sends. No network."""
from __future__ import annotations

from datetime import datetime, timedelta

from tests.test_onboarding import CT, _book, _card, _env, _submit


def _copies(sends):
    return [x for x in sends if x[0] == "email" and x[2] == "intake_copies"]


def test_copies_sent_once_complete_and_resent_after_roi() -> None:
    with _env() as (main, _cal, sends, drive, _folders, _fx):
        from app import billing, intake_copies, onboarding

        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        _book(main, start=start)
        _submit(main, "card-auth", authorizeCharges=True)
        _submit(main, "personal", phone="913-555-0100")
        assert not _copies(sends)  # not complete yet
        _submit(main, "consents", informedConsent=True)
        assert not _copies(sends)  # card still missing
        _card(main)
        assert len(_copies(sends)) == 1
        sms = [b for k, _t, b in sends if k == "sms" and "/docs/" in b]
        assert sms and "30 days" in sms[0]
        row = onboarding.find_client("new.person@example.com")
        cid = row["id"]
        first = intake_copies.last_sent(cid)
        _card(main)  # nothing new signed: no duplicate send
        assert len(_copies(sends)) == 1
        import time

        time.sleep(1.1)
        _submit(main, "roi", otherParty="Test Clinic", purpose="coordination", expires="1 year")
        assert len(_copies(sends)) == 2
        assert billing.active_share(cid, "docs")["id"] != first["id"]  # old link turned off
        summ = main._intake_summary({"id": cid, "email": row["email"]})
        assert summ["copies"]["sent_at"] and not summ["copies"]["revoked"]


def test_docs_page_lists_only_that_clients_pdfs_and_npp() -> None:
    with _env() as (main, _cal, sends, drive, _folders, _fx):
        from app import billing, intake_copies, onboarding

        start = datetime.now(CT).replace(microsecond=0, second=0, minute=0) + timedelta(days=7)
        _book(main, start=start)
        for f in ("card-auth", "personal", "consents"):
            _submit(main, f)
        _card(main)
        cid = onboarding.find_client("new.person@example.com")["id"]
        link = billing.mint_share(billing_id=cid, client_id=cid, source="docs", path="", name="x", kind="docs")
        page = main.public_intake_copies(link["token"])
        html = page.body.decode()
        assert page.status_code == 200 and "Card-on-file authorization" in html and "Consents" in html
        assert intake_copies.NPP_URL in html
        docs = intake_copies.docs_for(cid)
        pdf = main.public_intake_copy_file(link["token"], docs[0]["id"])
        assert pdf.status_code == 200 and pdf.body.startswith(b"%PDF")
        assert main.public_intake_copy_file(link["token"], "nope").status_code == 404
        # other kinds' tokens are rejected; revoked links stop working
        avs = billing.mint_share(billing_id=cid, client_id=cid, source="avs", path="", name="x", kind="avs")
        assert main.public_intake_copies(avs["token"]).status_code == 404
        billing.revoke_shares(cid, "docs")
        assert main.public_intake_copies(link["token"]).status_code == 410
