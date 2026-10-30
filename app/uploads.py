"""Patient portal uploads: one file per send, sniffed by content, filed to the client's Proton Drive chart.

Only metadata is stored here (never file bytes). Allowed: PDF, JPEG, PNG, HEIC, DOCX, XLSX (no macros/ActiveX, not encrypted) up to 15 MB; 10 per client per day.
A duplicate (same sha256 for the same client, already filed) is treated as already filed.
"""
from __future__ import annotations

import hashlib
import io
import zipfile
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import clients_db

MAX_BYTES = 15 * 1024 * 1024
EMAIL_ATTACH_MAX = 8 * 1024 * 1024
DAILY_LIMIT = 10

_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}


class UploadError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def sniff(blob: bytes) -> tuple[str, str, str] | None:
    """(kind, ext, mime) from the first bytes, or None if not an allowed type."""
    if blob[:5] == b"%PDF-":
        return "pdf", "pdf", "application/pdf"
    if blob[:3] == b"\xff\xd8\xff":
        return "jpeg", "jpg", "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "png", "image/png"
    if len(blob) >= 12 and blob[4:8] == b"ftyp" and blob[8:12] in _HEIC_BRANDS:
        return "heic", "heic", "image/heic"
    if blob[:4] == b"PK\x03\x04":
        return _ooxml(blob)
    # Legacy OLE (.doc/.xls) and encrypted Office files (which are OLE containers) are not accepted.
    return None


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ZIP_MAX_ENTRIES = 2000
_ZIP_MAX_UNCOMPRESSED = 150 * 1024 * 1024


class MacroError(ValueError):
    pass


def _ooxml(blob: bytes) -> tuple[str, str, str] | None:
    """Word/Excel by zip contents (not extension). Raises MacroError for macro/ActiveX/encrypted parts."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        infos = zf.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return None
    if len(infos) > _ZIP_MAX_ENTRIES or sum(i.file_size for i in infos) > _ZIP_MAX_UNCOMPRESSED:
        return None
    names = [i.filename for i in infos]
    low = [n.lower() for n in names]
    if any(i.flag_bits & 0x1 for i in infos):
        raise MacroError("encrypted")
    if any(n.endswith("vbaproject.bin") or "activex" in n or n.endswith(".bin") and "vba" in n for n in low):
        raise MacroError("macro")
    if "[content_types].xml" not in low:
        return None
    try:
        ct = zf.read(names[low.index("[content_types].xml")]).decode("utf-8", "replace").lower()
    except Exception:
        return None
    if "macroenabled" in ct or "vbaproject" in ct or "activex" in ct:
        raise MacroError("macro")
    if "word/document.xml" in low and "wordprocessingml.document.main+xml" in ct:
        return "docx", "docx", _DOCX_MIME
    if "xl/workbook.xml" in low and "spreadsheetml.sheet.main+xml" in ct:
        return "xlsx", "xlsx", _XLSX_MIME
    return None


def _init(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
          id TEXT PRIMARY KEY,
          client_id TEXT NOT NULL,
          chart_path TEXT,
          sha256 TEXT NOT NULL,
          size INTEGER NOT NULL,
          type TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS uploads_client ON uploads(client_id, created_at)")
    con.execute("CREATE INDEX IF NOT EXISTS uploads_sha ON uploads(client_id, sha256)")


def _con() -> sqlite3.Connection:
    con = clients_db.connect()
    _init(con)
    return con


def count_today(client_id: str, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=1)).isoformat()
    with _con() as con:
        r = con.execute(
            "SELECT COUNT(*) FROM uploads WHERE client_id=? AND created_at>=? AND status IN ('filed','duplicate')",
            (client_id, since),
        ).fetchone()
    return int(r[0])


def find_filed(client_id: str, sha256: str) -> dict[str, Any] | None:
    with _con() as con:
        r = con.execute(
            "SELECT * FROM uploads WHERE client_id=? AND sha256=? AND status='filed' ORDER BY created_at LIMIT 1",
            (client_id, sha256),
        ).fetchone()
    return dict(r) if r else None


def record(client_id: str, *, chart_path: str, sha256: str, size: int, kind: str, status: str) -> str:
    uid = secrets.token_hex(8)
    with _con() as con:
        con.execute(
            "INSERT INTO uploads (id, client_id, chart_path, sha256, size, type, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (uid, client_id, chart_path, sha256, size, kind, status, datetime.now(timezone.utc).isoformat()),
        )
    return uid


def list_for(client_id: str) -> list[dict[str, Any]]:
    with _con() as con:
        return [dict(r) for r in con.execute("SELECT * FROM uploads WHERE client_id=? ORDER BY created_at", (client_id,))]


def delete_ids(ids: list[str]) -> int:
    if not ids:
        return 0
    with _con() as con:
        cur = con.execute(f"DELETE FROM uploads WHERE id IN ({','.join('?' * len(ids))})", list(ids))
        return cur.rowcount


def safe_name(row: dict[str, Any], ext: str, now: datetime | None = None) -> str:
    """Generated name: date, initials, random tag. Never the patient's filename."""
    day = (now or datetime.now(timezone.utc)).date().isoformat()
    given = str(row.get("given_name") or "").strip()
    family = str(row.get("family_name") or "").strip()
    ini = (given[:1] + family[:1]).upper() if given and family else ""
    tag = secrets.token_hex(3)
    return f"{day} {ini} upload-{tag}.{ext}" if ini else f"{day} upload-{tag}.{ext}"


def log(client_id: str, size: int, kind: str, outcome: str) -> None:
    print("portal_upload", client_id, size, kind or "-", outcome, flush=True)


def handle(
    row: dict[str, Any],
    blob: bytes,
    *,
    file_into_chart: Callable[[str, bytes], str],
) -> dict[str, Any]:
    """Validate and file one upload. file_into_chart(name, blob) -> chart path (raises on failure)."""
    cid = str(row.get("id") or "")
    size = len(blob)
    if size == 0:
        log(cid, size, "", "rejected_empty")
        raise UploadError("empty", "That file is empty.")
    if size > MAX_BYTES:
        log(cid, size, "", "rejected_size")
        raise UploadError("too_large", "Files can be up to 15 MB.", 413)
    try:
        found = sniff(blob)
    except MacroError:
        log(cid, size, "office", "rejected_macro")
        raise UploadError("macro", "Word or Excel files with macros, ActiveX, or a password can't be sent. Please save a plain copy or a PDF.", 415)
    if not found:
        log(cid, size, "", "rejected_type")
        raise UploadError("bad_type", "Only PDF, JPG, PNG, HEIC, Word (.docx), or Excel (.xlsx) files can be sent.", 415)
    kind, ext, mime = found
    digest = hashlib.sha256(blob).hexdigest()
    dup = find_filed(cid, digest)
    if dup:
        record(cid, chart_path=dup.get("chart_path") or "", sha256=digest, size=size, kind=kind, status="duplicate")
        log(cid, size, kind, "duplicate")
        return {"ok": True, "duplicate": True, "kind": kind, "mime": mime, "size": size,
                "chart_path": dup.get("chart_path") or "", "sha256": digest}
    if count_today(cid) >= DAILY_LIMIT:
        log(cid, size, kind, "rejected_rate")
        raise UploadError("rate", "You can send up to 10 files a day. Please try again tomorrow.", 429)
    name = safe_name(row, ext)
    try:
        path = file_into_chart(name, blob)
    except Exception:
        record(cid, chart_path="", sha256=digest, size=size, kind=kind, status="failed")
        log(cid, size, kind, "failed_filing")
        raise UploadError("filing", "The file could not be saved right now. Please try again later.", 502)
    uid = record(cid, chart_path=path, sha256=digest, size=size, kind=kind, status="filed")
    log(cid, size, kind, "filed")
    return {"ok": True, "duplicate": False, "id": uid, "kind": kind, "mime": mime, "size": size,
            "chart_path": path, "name": name, "sha256": digest}
