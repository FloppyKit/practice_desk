"""Carepatron export -> Practice Desk importer. Dry run by default.

Deterministic load of clients, verbatim notes and appointments; optional
Bedrock (Nova Lite, us-east-1) pass that writes suggestions to a review file
only. Never logs field values; reports carry IDs and counts only.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from app import clients_db

# Our field -> candidate Carepatron CSV headers (case/space-insensitive).
# GUESSED from the help docs; adjust once the real export arrives.
CLIENT_COLUMNS: dict[str, list[str]] = {
    "carepatron_contact_id": ["Id", "Client Id", "Contact Id", "ID"],
    "given_name": ["First Name", "First name", "Given Name"],
    "family_name": ["Last Name", "Last name", "Family Name"],
    "preferred_name": ["Preferred Name", "Nickname"],
    "email": ["Email", "Email Address", "Primary Email"],
    "phone": ["Phone", "Phone Number", "Mobile", "Primary Phone"],
    "dob": ["Date of Birth", "Birth Date", "DOB", "Birthdate"],
    "status": ["Status"],
}
APPT_COLUMNS: dict[str, list[str]] = {
    "appointment_id": ["Id", "Appointment Id", "ID"],
    "contact_id": ["Client Id", "Contact Id", "Attendee Id"],
    "client_name": ["Client", "Client Name", "Attendee", "Attendees"],
    "start": ["Start", "Start Time", "Start Date", "Date"],
    "end": ["End", "End Time", "End Date"],
    "service": ["Service", "Service Name", "Title"],
    "status": ["Status"],
}

NOVA_MODEL = "amazon.nova-lite-v1:0"
NOVA_REGION = "us-east-1"
NOVA_PRICE_IN, NOVA_PRICE_OUT = 0.06 / 1e6, 0.24 / 1e6  # USD per token, on-demand

_now = lambda: datetime.now(timezone.utc).replace(microsecond=0).isoformat()  # noqa: E731
_key = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())  # noqa: E731


def _pick(row: dict[str, str], cols: dict[str, list[str]]) -> dict[str, str]:
    norm = {_key(k): (v or "").strip() for k, v in row.items() if k}
    out = {}
    for field, names in cols.items():
        out[field] = next((norm[_key(n)] for n in names if norm.get(_key(n))), "")
    return out


def norm_phone(raw: str) -> str:
    d = re.sub(r"\D", "", raw or "")
    if (raw or "").strip().startswith("+") and 8 <= len(d) <= 15:
        return "+" + d
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d[0] == "1":
        return "+" + d
    return ""


def norm_dob(raw: str) -> str:
    s = (raw or "").strip().split("T")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y", "%d %B %Y", "%B %d, %Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def ensure_schema(con: sqlite3.Connection) -> None:
    clients_db._init(con)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS imported_notes (
          id TEXT PRIMARY KEY, client_id TEXT, carepatron_contact_id TEXT,
          dos TEXT, source_file TEXT, content TEXT NOT NULL, imported_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_imported_notes_client ON imported_notes(client_id, dos);
        CREATE TABLE IF NOT EXISTS imported_appointments (
          id TEXT PRIMARY KEY, client_id TEXT, carepatron_contact_id TEXT,
          start_at TEXT, end_at TEXT, service TEXT, status TEXT, raw_json TEXT, imported_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_imported_appts_client ON imported_appointments(client_id, start_at);
        """
    )


class Report:
    def __init__(self) -> None:
        self.counts: dict[str, dict[str, int]] = {}
        self.gaps: list[dict[str, str]] = []
        self.review: list[dict[str, str]] = []

    def inc(self, kind: str, what: str, n: int = 1) -> None:
        self.counts.setdefault(kind, {}).setdefault(what, 0)
        self.counts[kind][what] += n

    def gap(self, kind: str, ref: str, reason: str) -> None:
        self.gaps.append({"type": kind, "ref": ref, "reason": reason})
        self.inc(kind, "gap")


# ---------- clients ----------

def _find(con, rec) -> tuple[sqlite3.Row | None, str]:
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    if rec["carepatron_contact_id"]:
        r = q("SELECT * FROM clients WHERE carepatron_contact_id=?", rec["carepatron_contact_id"])
        if r:
            return r[0], "cp_id"
    if rec["email"]:
        r = q("SELECT * FROM clients WHERE lower(email)=?", rec["email"])
        if len(r) == 1:
            return r[0], "email"
    if rec["phone_e164"] and rec["dob"]:
        r = q("SELECT * FROM clients WHERE phone_e164=? AND dob=?", rec["phone_e164"], rec["dob"])
        if len(r) == 1:
            return r[0], "phone_dob"
    return None, ""


def load_clients(con, csv_path: Path, rep: Report) -> None:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for i, raw in enumerate(rows, 1):
        rep.inc("clients", "in")
        p = _pick(raw, CLIENT_COLUMNS)
        ref = p["carepatron_contact_id"] or f"row{i}"
        rec = {
            "carepatron_contact_id": p["carepatron_contact_id"],
            "given_name": p["given_name"], "family_name": p["family_name"],
            "preferred_name": p["preferred_name"],
            "email": p["email"].lower(), "phone_e164": norm_phone(p["phone"]),
            "dob": norm_dob(p["dob"]),
        }
        if not rec["carepatron_contact_id"]:
            rep.gap("clients", ref, "missing contact id"); continue
        if p["phone"] and not rec["phone_e164"]:
            rep.inc("clients", "phone_unparsed")
        if p["dob"] and not rec["dob"]:
            rep.inc("clients", "dob_unparsed")
        rec["display_name"] = " ".join(x for x in (rec["given_name"], rec["family_name"]) if x)
        row, how = _find(con, rec)
        if row is not None and row["carepatron_contact_id"] and row["carepatron_contact_id"] != rec["carepatron_contact_id"]:
            rep.review.append({"type": "client", "ref": ref, "reason": f"{how} matches client {row['id']} linked to another Carepatron id"})
            rep.gap("clients", ref, "held for review"); continue
        if row is None:
            fam, giv = _key(rec["family_name"]), _key(rec["given_name"])
            if fam and giv:
                sim = [r["id"] for r in con.execute("SELECT id,given_name,family_name,dob FROM clients")
                       if _key(r["family_name"]) == fam and (_key(r["given_name"])[:3] == giv[:3])
                       and (not rec["dob"] or not r["dob"] or r["dob"] == rec["dob"])]
                if sim:
                    rep.review.append({"type": "client", "ref": ref, "reason": "name similar to existing client(s) " + ",".join(sim)})
                    rep.gap("clients", ref, "held for review"); continue
            now = _now()
            con.execute(
                "INSERT INTO clients (id,display_name,preferred_name,given_name,family_name,email,phone_e164,dob,"
                "status,source,carepatron_contact_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex[:16], rec["display_name"], rec["preferred_name"] or None, rec["given_name"],
                 rec["family_name"], rec["email"] or None, rec["phone_e164"] or None, rec["dob"] or None,
                 "active", "carepatron", rec["carepatron_contact_id"], now, now))
            rep.inc("clients", "created"); rep.inc("clients", "out"); continue
        # Matched: fill blanks only, never overwrite existing values.
        sets = {k: v for k, v in rec.items() if v and not row[k]}
        if sets:
            sets["updated_at"] = _now()
            con.execute(f"UPDATE clients SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), row["id"]))
            rep.inc("clients", "updated")
        else:
            rep.inc("clients", "unchanged")
        rep.inc("clients", "matched_" + how); rep.inc("clients", "out")


def _client_index(con) -> tuple[dict[str, str], dict[str, str]]:
    by_cp, by_name = {}, {}
    names: dict[str, list[str]] = {}
    for r in con.execute("SELECT id, carepatron_contact_id, display_name FROM clients"):
        if r["carepatron_contact_id"]:
            by_cp[r["carepatron_contact_id"]] = r["id"]
        names.setdefault(_key(r["display_name"]), []).append(r["carepatron_contact_id"] or "")
    by_name = {k: v[0] for k, v in names.items() if k and len(v) == 1 and v[0]}
    return by_cp, by_name


# ---------- notes ----------
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def note_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*.md") if p.is_file())


def _note_owner(path: Path, folder: Path, by_cp, by_name) -> str:
    """Carepatron contact id for a note: any path part equal to a known id, else a unique client name."""
    parts = list(path.relative_to(folder).parts)
    parts[-1] = path.stem
    for part in parts:
        for tok in [part, *re.split(r"[ _\-]{1,}", part)]:
            if tok in by_cp:
                return tok
    for part in parts:
        k = _key(_DATE.sub("", part))
        for name, cp in by_name.items():
            if name and k.startswith(name):
                return cp
    return ""


def load_notes(con, folder: Path, rep: Report) -> None:
    by_cp, by_name = _client_index(con)
    for p in note_files(folder):
        rep.inc("notes", "in")
        rel = str(p.relative_to(folder))
        content = p.read_text(encoding="utf-8", errors="replace")
        cp = _note_owner(p, folder, by_cp, by_name)
        if not cp:
            rep.gap("notes", rel, "no matching client"); continue
        m = _DATE.search(p.stem) or _DATE.search(content[:400])
        dos = m.group(1) if m else ""
        nid = hashlib.sha256(f"{cp}\x1f{dos}\x1f{content}".encode()).hexdigest()[:32]
        cur = con.execute(
            "INSERT OR IGNORE INTO imported_notes (id,client_id,carepatron_contact_id,dos,source_file,content,imported_at)"
            " VALUES (?,?,?,?,?,?,?)", (nid, by_cp[cp], cp, dos, rel, content, _now()))
        rep.inc("notes", "created" if cur.rowcount else "unchanged"); rep.inc("notes", "out")
        if not dos:
            rep.inc("notes", "no_dos")


# ---------- appointments ----------

def _appt_rows(src: Path) -> Iterable[tuple[str, dict[str, str]]]:
    if src.suffix.lower() == ".zip":
        with zipfile.ZipFile(src) as z:
            for n in sorted(z.namelist()):
                if n.lower().endswith(".csv"):
                    text = z.read(n).decode("utf-8-sig", errors="replace")
                    for i, r in enumerate(csv.DictReader(io.StringIO(text)), 1):
                        yield f"{n}#{i}", r
    else:
        with open(src, newline="", encoding="utf-8-sig") as f:
            for i, r in enumerate(csv.DictReader(f), 1):
                yield f"{src.name}#{i}", r


def load_appointments(con, src: Path, rep: Report) -> None:
    by_cp, by_name = _client_index(con)
    for ref, raw in _appt_rows(src):
        rep.inc("appointments", "in")
        p = _pick(raw, APPT_COLUMNS)
        cp = p["contact_id"] if p["contact_id"] in by_cp else by_name.get(_key(p["client_name"]), "")
        rid = p["appointment_id"] or ref
        if not cp:
            rep.gap("appointments", rid, "no matching client"); continue
        aid = hashlib.sha256(f"{cp}\x1f{p['appointment_id']}\x1f{p['start']}\x1f{p['service']}".encode()).hexdigest()[:32]
        cur = con.execute(
            "INSERT OR IGNORE INTO imported_appointments (id,client_id,carepatron_contact_id,start_at,end_at,service,status,raw_json,imported_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (aid, by_cp[cp], cp, p["start"], p["end"], p["service"], p["status"], json.dumps(raw, sort_keys=True), _now()))
        rep.inc("appointments", "created" if cur.rowcount else "unchanged"); rep.inc("appointments", "out")


def count_pdfs(folder: Path, rep: Report) -> list[Path]:
    pdfs = sorted(folder.rglob("*.pdf"))
    rep.inc("pdfs", "in", len(pdfs))
    return pdfs


# ---------- Bedrock extraction (suggestions only) ----------
EXTRACT_SCHEMA = {"dx_codes": list, "medications": list}
_ICD = re.compile(r"^[A-TV-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
PROMPT = ("Extract from this psychiatric note. Reply with ONLY JSON: "
          '{"dx_codes": [ICD-10 codes explicitly stated], "medications": [{"name": str, "dose": str}]}. '
          "Do not infer anything not written.\n\nNOTE:\n")


def bedrock_token() -> str:
    tok = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "")
    if tok:
        return tok
    envf = Path.home() / ".config/practice-desk/bedrock-new.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?AWS_BEARER_TOKEN\s*=\s*['\"]?([^'\"\s]+)", line)
            if m:
                return m.group(1)
    return ""


def nova_call(text: str) -> tuple[str, int, int]:
    url = f"https://bedrock-runtime.{NOVA_REGION}.amazonaws.com/model/{NOVA_MODEL}/converse"
    body = {"messages": [{"role": "user", "content": [{"text": PROMPT + text[:20000]}]}],
            "inferenceConfig": {"maxTokens": 600, "temperature": 0}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + bedrock_token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.load(r)
    u = out.get("usage", {})
    return out["output"]["message"]["content"][0]["text"], int(u.get("inputTokens", 0)), int(u.get("outputTokens", 0))


def validate_extract(raw: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        d = json.loads(m.group(0)) if m else None
    except ValueError:
        return None
    if not isinstance(d, dict) or any(not isinstance(d.get(k), t) for k, t in EXTRACT_SCHEMA.items()):
        return None
    dx = [c.strip().upper() for c in d["dx_codes"] if isinstance(c, str) and _ICD.match(c.strip().upper())]
    meds = [{"name": str(x.get("name", ""))[:80], "dose": str(x.get("dose", ""))[:40]}
            for x in d["medications"] if isinstance(x, dict) and x.get("name")]
    return {"dx_codes": dx, "medications": meds}


def extract(con, out_dir: Path, rep: Report, call: Callable[[str], tuple[str, int, int]] = nova_call) -> float:
    rows = con.execute("SELECT id, client_id, dos, content FROM imported_notes ORDER BY client_id, dos").fetchall()
    tin = tout = 0
    with open(out_dir / "extract_suggestions.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["note_id", "client_id", "dos", "status", "dx_codes", "medications"])
        for r in rows:
            rep.inc("extract", "in")
            try:
                text, i, o = call(r["content"])
                tin += i; tout += o
                d = validate_extract(text)
            except Exception as e:  # noqa: BLE001
                d = None; rep.gap("extract", r["id"], "call failed: " + type(e).__name__); continue
            if d is None:
                rep.gap("extract", r["id"], "invalid JSON"); continue
            w.writerow([r["id"], r["client_id"], r["dos"], "needs_review",
                        json.dumps(d["dx_codes"]), json.dumps(d["medications"])])
            rep.inc("extract", "out")
    return tin * NOVA_PRICE_IN + tout * NOVA_PRICE_OUT


# ---------- run ----------

def write_reports(rep: Report, out_dir: Path, mode: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "review.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["type", "ref", "reason"]); w.writeheader(); w.writerows(rep.review)
    lines = [f"# Carepatron import reconciliation ({mode})", "", "| type | in | out | gaps | detail |", "|---|---|---|---|---|"]
    for k, c in rep.counts.items():
        det = ", ".join(f"{a}={b}" for a, b in sorted(c.items()) if a not in ("in", "out", "gap"))
        lines.append(f"| {k} | {c.get('in', 0)} | {c.get('out', 0)} | {c.get('gap', 0)} | {det} |")
    lines += ["", "## Gaps", ""] + [f"- {g['type']} `{g['ref']}`: {g['reason']}" for g in rep.gaps]
    (out_dir / "reconciliation.md").write_text("\n".join(lines) + "\n")
    (out_dir / "reconciliation.json").write_text(json.dumps({"mode": mode, "counts": rep.counts, "gaps": rep.gaps}, indent=1))


def run(*, db: Path, clients_csv: Path | None, notes_dir: Path | None, appts: Path | None,
        pdf_dir: Path | None, out_dir: Path, apply: bool = False, do_extract: bool = False,
        call: Callable | None = None) -> Report:
    rep = Report()
    if apply:
        target = db
    else:
        tmp = Path(tempfile.mkdtemp(prefix="cp-dryrun-"))
        target = tmp / "dryrun.sqlite"
        if db.exists():
            src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            dst = sqlite3.connect(str(target)); src.backup(dst); dst.close(); src.close()
    con = sqlite3.connect(str(target), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        ensure_schema(con)
        if clients_csv: load_clients(con, clients_csv, rep)
        if notes_dir: load_notes(con, notes_dir, rep)
        if appts: load_appointments(con, appts, rep)
        if pdf_dir: count_pdfs(pdf_dir, rep)
        con.commit()
        out_dir.mkdir(parents=True, exist_ok=True)
        if do_extract:
            rep.cost_usd = extract(con, out_dir, rep, call or nova_call)  # type: ignore[attr-defined]
    finally:
        con.close()
        if not apply:
            shutil.rmtree(target.parent, ignore_errors=True)
    write_reports(rep, out_dir, "APPLY" if apply else "dry run (copy, discarded)")
    return rep
