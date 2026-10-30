"""Portal uploads: content sniffing, Office macro checks, size/rate limits, dedupe, metadata-only rows."""
from __future__ import annotations

import io
import os
import tempfile
import zipfile


def _fresh_db():
    d = tempfile.mkdtemp()
    os.environ["CLIENTS_DB"] = os.path.join(d, "c.sqlite")
    import importlib
    import pathlib

    from app import clients_db

    importlib.reload(clients_db)
    clients_db.DB_PATH = pathlib.Path(os.environ["CLIENTS_DB"])
    clients_db.connect()
    from app import uploads

    importlib.reload(uploads)
    return uploads


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64
EXE = b"MZ\x90\x00" + b"\x00" * 64
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64

_DOCX_CT = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>')
_XLSX_CT = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet.main+xml"/></Types>')
_DOCM_CT = _DOCX_CT.replace("officedocument.wordprocessingml.document.main+xml", "ms-word.document.macroEnabled.main+xml")


def _zip(parts: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in parts.items():
            z.writestr(k, v)
    return buf.getvalue()


def docx(extra: dict | None = None, ct: str = _DOCX_CT) -> bytes:
    return _zip({"[Content_Types].xml": ct, "word/document.xml": "<w:document/>", **(extra or {})})


def xlsx() -> bytes:
    return _zip({"[Content_Types].xml": _XLSX_CT, "xl/workbook.xml": "<workbook/>"})


ROW = {"id": "synthetic-upload-1", "given_name": "Test", "family_name": "Patient", "name": "Test Patient"}


def _filer(store: list):
    def f(name: str, blob: bytes) -> str:
        store.append((name, len(blob)))
        return "/Clients/Test Patient/Chart/Uploads/" + name
    return f


def test_sniff_types() -> None:
    up = _fresh_db()
    assert up.sniff(PDF)[0] == "pdf" and up.sniff(PNG)[0] == "png"
    assert up.sniff(JPG)[0] == "jpeg" and up.sniff(HEIC)[0] == "heic"
    assert up.sniff(docx())[0] == "docx" and up.sniff(xlsx())[0] == "xlsx"
    assert up.sniff(EXE) is None and up.sniff(OLE) is None and up.sniff(b"hello") is None
    assert up.sniff(_zip({"a.txt": "x"})) is None  # plain zip is not Office


def test_office_macro_and_activex_rejected() -> None:
    up = _fresh_db()
    for bad in (docx({"word/vbaProject.bin": b"\x00vba"}), docx(ct=_DOCM_CT), docx({"word/activeX/activeX1.xml": "<a/>"})):
        try:
            up.handle(ROW, bad, file_into_chart=_filer([]))
            raise AssertionError("macro file accepted")
        except up.UploadError as e:
            assert e.code == "macro" and e.status == 415


def test_exe_renamed_and_size_rejected() -> None:
    up = _fresh_db()
    for blob, code in ((EXE, "bad_type"), (OLE, "bad_type"), (b"%PDF-" + b"0" * (16 * 1024 * 1024), "too_large"), (b"", "empty")):
        try:
            up.handle(ROW, blob, file_into_chart=_filer([]))
            raise AssertionError("accepted " + code)
        except up.UploadError as e:
            assert e.code == code, (e.code, code)
    assert up.list_for(ROW["id"]) == []  # nothing recorded for rejected files


def test_filed_dedupe_rate_and_safe_name() -> None:
    up = _fresh_db()
    store: list = []
    a = up.handle(ROW, PDF, file_into_chart=_filer(store))
    assert a["kind"] == "pdf" and not a["duplicate"] and a["name"].endswith(".pdf") and "upload-" in a["name"]
    assert " TP " in a["name"]  # initials only, never the original filename
    b = up.handle(ROW, PDF, file_into_chart=_filer(store))
    assert b["duplicate"] and b["chart_path"] == a["chart_path"] and len(store) == 1
    for blob in (PNG, docx(), xlsx()):
        assert not up.handle(ROW, blob, file_into_chart=_filer(store))["duplicate"]
    rows = up.list_for(ROW["id"])
    assert {r["status"] for r in rows} == {"filed", "duplicate"} and all("sha256" in r for r in rows)
    assert set(rows[0].keys()) == {"id", "client_id", "chart_path", "sha256", "size", "type", "status", "created_at"}
    for i in range(10):
        try:
            up.handle(ROW, PNG + bytes([i]) * 8, file_into_chart=_filer(store))
        except up.UploadError as e:
            assert e.code == "rate" and e.status == 429
            break
    else:
        raise AssertionError("no rate limit")
    assert up.delete_ids([r["id"] for r in up.list_for(ROW["id"])]) >= 5


def test_filing_failure_recorded_not_raised_raw() -> None:
    up = _fresh_db()

    def boom(name: str, blob: bytes) -> str:
        raise RuntimeError("drive down")

    try:
        up.handle(ROW, PNG, file_into_chart=boom)
        raise AssertionError("no error")
    except up.UploadError as e:
        assert e.code == "filing" and e.status == 502
    assert [r["status"] for r in up.list_for(ROW["id"])] == ["failed"]
