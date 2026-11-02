import os, tempfile
def test_book_token_survives_mangling(monkeypatch):
    d = tempfile.mkdtemp(); f = os.path.join(d, "s"); open(f, "w").write("x" * 64)
    monkeypatch.setenv("CANCEL_SECRET_FILE", f)
    from app.tokens import mint_book_token, verify_book_token
    t = mint_book_token(client_id="c1")
    for bad in (t, t + ".", t + ")", t + "%2E", " " + t + "\n", t[:60] + "\n" + t[60:]):
        assert verify_book_token(bad)["id"] == "c1"
    import pytest
    with pytest.raises(ValueError):
        verify_book_token(t[:-2] + "AA")
