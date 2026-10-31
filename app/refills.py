"""Refill requests v0: intake, patient match, last visit, rules. Read-only; never acts.

Types (sketch):
  Facts      = controlled ('unknown'|'none'|'C2'..'C5'), age_years, last_visit, next_visit, today
  Rec        = {recommendation, status, flags[], notes[], max_supply_days}
  Request    = refill_requests row  (PHI stays in the booker SQLite)
  Event      = refill_events row     (ids + actions only)
Modules: rules (pure) -> recommend();  intake -> prefilter(), extract() (Bedrock, BAA AWS);
  match -> match_client() (contact + name + DOB; never name only);  visits -> last_visit();
  store -> ingest(), list_requests(), summary() (PHI-free).
MDToolbox: not wired. Med/pharmacy/controlled stay 'pending MDToolbox'.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from . import clients_db

VISITS_FN: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None  # set by main (desk calendar)

# ---------------------------------------------------------------- config (Matt's rules)
DEFAULTS: dict[str, Any] = {
    "minor_age": 18,
    "minor_window_days": 92,      # ~3 months since last completed visit
    "adult_window_days": 183,     # ~6 months
    "controlled_window_days": 92,
    "max_supply_days": 90,        # one 90-day fill or 3x30
    "bridge_days": 30,
    # Matt 2026-10-30 11:28 PM: minors get a 1-month grace (3-4 months -> bridge 30 then visit).
    # Adults: no grace (over 6 months -> needs_visit, no refill).
    "minor_grace_days": 31,
    "adult_grace_days": 0,
    "completed_statuses": ["Attended", "Confirmed"],  # imported history; past start only
}


def config() -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    try:
        from .config import load_config

        cfg.update((load_config() or {}).get("refills") or {})
    except Exception:
        pass
    return cfg


# ---------------------------------------------------------------- rules (pure)
def _bucket_days(days: int) -> str:
    if days <= 7:
        return "within 7 days"
    if days <= 30:
        return "in 8-30 days"
    return "in over 30 days"


def recommend(
    *,
    controlled: str | None,
    age_years: int | None,
    last_visit: date | None,
    next_visit: date | None,
    today: date,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recommendation only. Never stages, saves, or sends."""
    c = dict(DEFAULTS, **(cfg or {}))
    since = (today - last_visit).days if last_visit else None
    flags: list[str] = []
    notes: list[str] = []
    booked_note = ("visit booked " + _bucket_days((next_visit - today).days)) if next_visit else ""
    ctl = (controlled or "unknown").strip()
    if ctl != "none":  # controlled or unknown
        flags.append("controlled_unknown" if ctl == "unknown" else "controlled")
        ok = since is not None and since <= c["controlled_window_days"]
        if not ok and booked_note:
            notes.append(booked_note)
        return {"recommendation": "controlled_matt_signs" if ok else "needs_visit",
                "status": "controlled" if ok else "overdue", "flags": flags, "notes": notes,
                "max_supply_days": None}
    if age_years is None:
        return {"recommendation": "needs_dob", "status": "needs_dob", "flags": ["no_age"],
                "notes": notes, "max_supply_days": None}
    window = c["minor_window_days"] if age_years < c["minor_age"] else c["adult_window_days"]
    within = since is not None and since <= window
    if within and next_visit:
        return {"recommendation": "refill_as_last_order", "status": "pass", "flags": flags,
                "notes": ["fill details pending MDToolbox"], "max_supply_days": c["max_supply_days"]}
    if within:
        return {"recommendation": "bridge_30_and_schedule", "status": "pass",
                "flags": flags + ["booking_text_needed"], "notes": notes, "max_supply_days": c["bridge_days"]}
    grace = c["minor_grace_days"] if age_years < c["minor_age"] else c["adult_grace_days"]
    if since is not None and since <= window + grace:
        if booked_note:
            notes.append(booked_note)
        return {"recommendation": "bridge_30_then_visit", "status": "pass",
                "flags": flags + ["visit_needed_before_more"] + ([] if next_visit else ["booking_text_needed"]),
                "notes": notes, "max_supply_days": c["bridge_days"]}
    if booked_note:
        notes.append(booked_note)
    if since is None:
        flags.append("no_completed_visit")
    return {"recommendation": "needs_visit", "status": "overdue", "flags": flags, "notes": notes,
            "max_supply_days": None}


def age_on(dob: str, on: date) -> int | None:
    try:
        d = date.fromisoformat(str(dob)[:10])
    except ValueError:
        return None
    return on.year - d.year - ((on.month, on.day) < (d.month, d.day))


# ---------------------------------------------------------------- intake
_KW = re.compile(
    r"\b(refill|refills|re-fill|rx|prescription|prescriptions|pharmacy|meds?|medication|"
    r"run(?:ning)? out|ran out|out of my)\b", re.I)


def prefilter(text: str) -> bool:
    return bool(_KW.search(text or ""))


_SYSTEM = (
    "Extract a medication refill request from one patient message. Reply with JSON only: "
    '{"is_refill": true|false, "name": "", "dob": "YYYY-MM-DD or empty", "meds": ["..."], '
    '"pharmacy": ""}. Use empty values when not stated. Do not guess.'
)


def extract(text: str, *, call: Callable[[str, str], str] | None = None) -> dict[str, Any]:
    """Bedrock (AWS, under BAA) via the desk_agent setup. Only prefiltered text gets here."""
    raw = (call or _bedrock_text)(_SYSTEM, text[:1500])
    m = re.search(r"\{.*\}", raw or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        data = {}
    meds = data.get("meds") if isinstance(data.get("meds"), list) else []
    return {
        "is_refill": bool(data.get("is_refill")),
        "name": str(data.get("name") or "")[:80],
        "dob": str(data.get("dob") or "")[:10],
        "meds": [str(x)[:80] for x in meds][:5],
        "pharmacy": str(data.get("pharmacy") or "")[:160],
    }


def _bedrock_text(system: str, text: str) -> str:
    from .desk_agent import BEDROCK_TOKEN, BEDROCK_URL

    if not BEDROCK_TOKEN:
        raise RuntimeError("bedrock token unset")
    body = json.dumps({
        "system": [{"text": system}],
        "messages": [{"role": "user", "content": [{"text": text}]}],
        "inferenceConfig": {"temperature": 0, "maxTokens": 300},
    }).encode()
    req = urllib.request.Request(BEDROCK_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + BEDROCK_TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read().decode() or "{}")
    blocks = (((out.get("output") or {}).get("message") or {}).get("content") or [])
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))


# ---------------------------------------------------------------- match
def _norm_name(s: str) -> str:
    return " ".join(re.sub(r"[^a-z ]", " ", (s or "").lower()).split())


def _digits(s: str) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())[-10:]


def match_client(con: sqlite3.Connection, *, phones: list[str], email: str, name: str, dob: str
                 ) -> tuple[str, str, str, list[str]]:
    """-> (client_id, status, basis, flags). Name + contact (email/phone) to exactly one client.
    DOB optional (Matt 11:35 PM); a given DOB that conflicts is flagged dob_mismatch. Never name only."""
    cands: dict[str, sqlite3.Row] = {}
    for ph in phones:
        d = _digits(ph)
        if len(d) == 10:
            for r in con.execute("SELECT * FROM clients WHERE substr(replace(phone_e164,'+',''),-10)=?", (d,)):
                cands[r["id"]] = r
    if email:
        for r in con.execute("SELECT * FROM clients WHERE lower(email)=?", (email.strip().lower(),)):
            cands[r["id"]] = r
    if not cands:
        return "", "unmatched", "no contact match", []
    if len(cands) > 1:
        return "", "ambiguous_match", f"contact shared by {len(cands)} clients", []
    want = _norm_name(name)
    r = next(iter(cands.values()))
    full = {_norm_name(r["display_name"]), _norm_name(f"{r['given_name'] or ''} {r['family_name'] or ''}"),
            _norm_name(f"{r['preferred_name'] or ''} {r['family_name'] or ''}")}
    if not want or want not in full:
        return "", "ambiguous_match", "name missing or differs from the contact's client", []
    flags = []
    if dob and r["dob"] and str(r["dob"])[:10] != dob[:10]:
        flags.append("dob_mismatch")
    return r["id"], "matched", "contact+name" + ("+dob" if dob and "dob_mismatch" not in flags and r["dob"] else ""), flags


# ---------------------------------------------------------------- visits
def last_visit(con: sqlite3.Connection, client_id: str, *, now: datetime,
               calendar_past: list[dict[str, Any]] | None = None,
               cfg: dict[str, Any] | None = None) -> tuple[date | None, str]:
    """Latest completed visit and its source. No-shows/cancellations excluded."""
    c = dict(DEFAULTS, **(cfg or {}))
    best: tuple[date | None, str] = (None, "")

    def take(d: date | None, src: str) -> None:
        nonlocal best
        if d and (best[0] is None or d > best[0]):
            best = (d, src)
    qs = ",".join("?" * len(c["completed_statuses"]))
    try:
        row = con.execute(
        f"SELECT max(start_utc) FROM imported_appointments WHERE client_id=? AND status IN ({qs}) AND start_utc < ?",
        (client_id, *c["completed_statuses"], now.astimezone(timezone.utc).isoformat())).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and row[0]:
        take(date.fromisoformat(str(row[0])[:10]), "imported_appointments")
    for v in calendar_past or []:
        s = str(v.get("start_iso") or "")[:10]
        if s and not v.get("no_show"):
            take(date.fromisoformat(s), "desk_calendar")
    try:
        row = con.execute("SELECT max(dos) FROM signed_notes WHERE client_id=?", (client_id,)).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row and row[0]:
        take(date.fromisoformat(str(row[0])[:10]), "signed_notes")
    return best


# ---------------------------------------------------------------- store
def _init(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS refill_requests (
      id TEXT PRIMARY KEY, source TEXT, source_ref TEXT, received_at TEXT,
      client_id TEXT, match_basis TEXT, status TEXT, recommendation TEXT,
      flags_json TEXT, notes_json TEXT, max_supply_days INTEGER,
      med_text TEXT, controlled TEXT DEFAULT 'unknown', pharmacy_requested TEXT,
      pharmacy_on_file TEXT, last_visit_at TEXT, last_visit_source TEXT, next_visit_at TEXT,
      patient_age INTEGER, updated_at TEXT);
    CREATE INDEX IF NOT EXISTS refill_requests_received ON refill_requests(received_at);
    CREATE TABLE IF NOT EXISTS refill_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, at TEXT, actor TEXT, action TEXT, detail_json TEXT);
    """)


def connect() -> sqlite3.Connection:
    con = clients_db.connect()
    _init(con)
    return con


def _event(con: sqlite3.Connection, rid: str, actor: str, action: str, detail: dict[str, Any]) -> None:
    con.execute("INSERT INTO refill_events (request_id, at, actor, action, detail_json) VALUES (?,?,?,?,?)",
                (rid, datetime.now(timezone.utc).isoformat(), actor, action, json.dumps(detail)))


def ingest(*, source: str, source_ref: str, text: str, phones: list[str] | None = None, email: str = "",
           name: str = "", extract_fn: Callable[[str], dict[str, Any]] | None = None,
           visits_fn: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
           now: datetime | None = None, spruce_conversation_id: str = "") -> str | None:
    """New message -> maybe a 'new' refill request. Returns request id or None. Never acts."""
    if not prefilter(text):
        return None
    ex = (extract_fn or extract)(text)
    if not ex.get("is_refill"):
        return None
    visits_fn = visits_fn or VISITS_FN
    if not phones and spruce_conversation_id:
        try:
            from urllib.parse import quote

            from .chat_threads import _phones_in
            from .spruce_api import spruce_request

            _st, full = spruce_request("GET", f"/conversations/{quote(spruce_conversation_id, safe='')}")
            phones = _phones_in((full or {}).get("externalParticipants") or [], [])
        except Exception:
            phones = []
    now = now or datetime.now(timezone.utc)
    today = now.astimezone().date()
    rid = uuid.uuid4().hex[:16]
    with connect() as con:
        cid, status, basis, mflags = match_client(con, phones=phones or [], email=email,
                                                  name=ex.get("name") or name, dob=ex.get("dob") or "")
        age = lv = nv = None
        src = ""
        rec: dict[str, Any] = {"recommendation": "", "flags": [], "notes": [], "max_supply_days": None}
        if status == "matched":
            row = con.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
            age = age_on(row["dob"], today)
            cal = []
            if visits_fn:
                try:
                    cal = visits_fn(dict(row))
                except Exception:
                    cal = []
            lv, src = last_visit(con, cid, now=now, calendar_past=[v for v in cal if not v.get("upcoming")])
            fut = sorted(str(v.get("start_iso") or "")[:10] for v in cal if v.get("upcoming"))
            nv = date.fromisoformat(fut[0]) if fut else None
            rec = recommend(controlled="unknown", age_years=age, last_visit=lv, next_visit=nv, today=today,
                            cfg=config())
            status = rec["status"]
            rec["flags"] = mflags + rec["flags"]
        con.execute(
            "INSERT INTO refill_requests (id, source, source_ref, received_at, client_id, match_basis, status,"
            " recommendation, flags_json, notes_json, max_supply_days, med_text, controlled, pharmacy_requested,"
            " pharmacy_on_file, last_visit_at, last_visit_source, next_visit_at, patient_age, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, source, source_ref, now.isoformat(), cid, basis, status, rec["recommendation"],
             json.dumps(rec["flags"]), json.dumps(rec["notes"]), rec["max_supply_days"],
             ", ".join(ex.get("meds") or []), "unknown", ex.get("pharmacy") or "", "",
             lv.isoformat() if lv else "", src, nv.isoformat() if nv else "", age, now.isoformat()))
        _event(con, rid, "pa", "ingest", {"status": status, "recommendation": rec["recommendation"]})
    return rid


def ingest_bg(**kw: Any) -> None:
    """Fire-and-forget from webhook/chat paths; errors never reach the patient path."""
    def run() -> None:
        try:
            ingest(**kw)
        except Exception as e:  # ids only in logs
            print("refills_ingest_fail", type(e).__name__, flush=True)
    if prefilter(kw.get("text") or ""):
        threading.Thread(target=run, name="refill-ingest", daemon=True).start()


def list_requests(limit: int = 100) -> list[dict[str, Any]]:
    with connect() as con:
        rows = con.execute(
            "SELECT r.*, c.display_name AS client_name FROM refill_requests r "
            "LEFT JOIN clients c ON c.id = r.client_id ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["flags"] = json.loads(d.pop("flags_json") or "[]")
        d["notes"] = json.loads(d.pop("notes_json") or "[]")
        out.append(d)
    return out


# needs_dob kept readable for older rows; v0.1 uses ambiguous_match for matching and needs_dob only
# when the matched record has no DOB (age unknown for the minor/adult rule).
SUMMARY_STATUSES = ("new", "matched", "pass", "overdue", "controlled", "ambiguous_match", "needs_dob", "unmatched")
AGE_BUCKETS = ("lt_1d", "1_3d", "3_7d", "gt_7d")


def summary(now: float | None = None) -> dict[str, Any]:
    """PHI-free: counts by status and age bucket only."""
    now = now or time.time()
    by_status = {s: 0 for s in SUMMARY_STATUSES}
    by_age = {b: 0 for b in AGE_BUCKETS}
    with connect() as con:
        for st, at in con.execute("SELECT status, received_at FROM refill_requests"):
            if st in by_status:
                by_status[st] += 1
            try:
                age_d = (now - datetime.fromisoformat(at).timestamp()) / 86400
            except Exception:
                continue
            b = "lt_1d" if age_d < 1 else "1_3d" if age_d < 3 else "3_7d" if age_d < 7 else "gt_7d"
            by_age[b] += 1
    return {"ok": True, "total": sum(by_age.values()), "by_status": by_status, "by_age": by_age}
