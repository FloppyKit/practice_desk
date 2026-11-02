"""Visit reminders for every timed visit on Matt's calendar (not only booker-booked).

Types: 24h email, 24h text, 30-min text. Texts via Spruce only, consent required.
Safety: sends ONLY if REMINDERS_SEND=1 env or BOOKER_LOG_DIR/reminders/ENABLED exists. Otherwise
each tick writes a dry-run ledger (reminders/dryrun-YYYYMMDD.jsonl): hashed event id, type, times,
channel, skip reason. Never names, emails or phones in logs.
Idempotent: ledger key = sha(event_id)|type|start_iso, so re-runs never double-send and a moved
visit gets reminders for its new time only; cancelled/removed events are simply never seen.

CLI: python -m app.cal_reminders tick | report [days] | test (burner only, forced)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
TYPES = ("email_24h", "sms_24h", "sms_30m")
# type -> (lead before start, latest useful moment before start)
WINDOWS = {
    "email_24h": (timedelta(hours=24), timedelta(hours=3)),
    "sms_24h": (timedelta(hours=24), timedelta(hours=3)),
    "sms_30m": (timedelta(minutes=30), timedelta(minutes=5)),
}
# Matt 2026-11-02 (t234u): every current client consented to texts via Spruce. With this flag on,
# missing consent counts as yes; an explicit opt-out (sms_consent=0, e.g. inbound STOP) still blocks.
ASSUME_SMS_CONSENT = os.environ.get("REMINDERS_ASSUME_SMS_CONSENT", "1") == "1"
# A 24h reminder more than this late is skipped (would say "tomorrow" for a same-day visit).
LATE_SKIP = timedelta(hours=2)
TEST_EMAIL = "psycharts@mail.grokbot.com"
TEST_PHONE = "+19133918760"
NON_VISIT = re.compile(r"\b(block(ed)?|hold|busy|lunch|personal|off|ooo|out of office|vacation|admin|"
                       r"meeting|break|gym|pto|holiday|travel|do not book)\b", re.I)


def _dir() -> Path:
    d = Path(os.environ.get("BOOKER_LOG_DIR", "/data/logs")) / "reminders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def send_enabled() -> bool:
    return os.environ.get("REMINDERS_SEND", "") == "1" or (_dir() / "ENABLED").exists()


def _h(event_id: str) -> str:
    return hashlib.sha256(("rem:" + str(event_id)).encode()).hexdigest()[:16]


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(str(_dir() / "ledger.sqlite"))
    con.execute("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ev TEXT, type TEXT, start TEXT, "
                "status TEXT, detail TEXT, at TEXT)")
    return con


def _bare(title: str) -> str:
    c = _candidates(title)
    return c[0] if c else ""


def _candidates(title: str) -> list[str]:
    """Name candidates from a calendar title: each ' - ' / '·' / ':' segment, brackets and
    '30 min'-style durations removed. Calendar titles look like 'First Last - 30 Min Follow Up'."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", str(title or ""))
    segs = re.split(r"\s+-\s+|·|:|\|", t)
    out = []
    for seg in segs:
        seg = re.sub(r"\b\d+\s*(min(ute)?s?|hr|hour)?\b", " ", seg, flags=re.I)
        seg = re.sub(r"\s+", " ", seg).strip()
        if len(seg) >= 3 and not NON_VISIT.fullmatch(seg) and not re.fullmatch(
                r"(new|follow[- ]?up|visit|intake|telehealth|video|session|appointment|med(ication)? (check|management)|"
                r"psychiatric|evaluation|eval|consult(ation)?|\s)+", seg, re.I):
            out.append(seg)
    return out


def match_client(name: str, title: str) -> tuple[dict[str, Any] | None, str]:
    """Same rules as the desk Copy button: raw then bare name; exact name breaks ties."""
    from . import clients_db

    tried = [q for q in dict.fromkeys([str(name or "").strip(), *_candidates(name), *_candidates(title)]) if q]
    ambiguous = False
    for q in tried:
        hits = clients_db.search(q, limit=8)
        if len(hits) > 1:
            exact = [h for h in hits if str(h.get("display_name") or h.get("name") or "").strip().lower() == q.lower()]
            if len(exact) == 1:
                hits = exact
            else:
                ambiguous = True
                continue
        if len(hits) == 1:
            row = clients_db.get_by_id(str(hits[0]["id"]))
            if row:
                return row, "matched"
    return None, ("ambiguous" if ambiguous else "unmatched")


def _visits(now: datetime, days: float) -> list[dict[str, Any]]:
    from .cal import list_events
    from .main import _desk_cal_ids, _event_mail_fields

    out = []
    t0, t1 = now - timedelta(minutes=10), now + timedelta(days=days)
    for cid in _desk_cal_ids():
        for ev in list_events(cid, t0, t1):
            if ev.get("status") == "cancelled":
                continue
            out.append((ev, _event_mail_fields(ev, cid, TZ)))
    return out


def plan(now: datetime | None = None, days: float = 2.0) -> list[dict[str, Any]]:
    """Every (visit, type) with its decision. Holds contact data in memory only."""
    now = now or datetime.now(TZ)
    rows: list[dict[str, Any]] = []
    for ev, f in _visits(now, days):
        if not (ev.get("start") or {}).get("dateTime") or not f.get("start_iso"):
            continue  # all-day: blocks
        start = datetime.fromisoformat(f["start_iso"])
        if start <= now:
            continue
        title = str(f.get("title") or "")
        name = str(f.get("name") or "")
        if not name and NON_VISIT.search(title):
            continue  # block / hold / personal
        if (ev.get("transparency") == "transparent") and not name:
            continue
        client, how = (None, "booker") if f.get("email") or f.get("phone") else match_client(name, title)
        email = str(f.get("email") or (client or {}).get("email") or "")
        phone = str(f.get("phone") or (client or {}).get("phone_e164") or "")
        raw_c = (client or {}).get("sms_consent")
        opted_out = raw_c in (0, "0")
        consent = not opted_out and (bool(f.get("sms_consent")) or raw_c in (1, "1", True) or ASSUME_SMS_CONSENT)
        base = {"ev": _h(f["event_id"]), "event_id": f["event_id"], "start": start.isoformat(), "match": how,
                "title": title, "when_label": f.get("when_label") or "", "duration": f.get("duration_minutes"),
                "event_type": f.get("event_type") or "", "name": (client or {}).get("display_name") or name,
                "email": email, "phone": phone, "consent": consent}
        for t in TYPES:
            lead, latest = WINDOWS[t]
            send_at = start - lead
            r = dict(base, type=t, channel="email" if t.startswith("email") else "sms",
                     send_at=send_at.isoformat(), skip="")
            if how in ("unmatched", "ambiguous"):
                r["skip"] = how
            elif t.startswith("email") and not email:
                r["skip"] = "no email"
            elif t.startswith("sms") and not phone:
                r["skip"] = "no phone"
            elif t.startswith("sms") and opted_out:
                r["skip"] = "sms opt-out"
            elif t.startswith("sms") and not consent:
                r["skip"] = "no sms consent"
            elif t.endswith("24h") and now > send_at + LATE_SKIP:
                r["skip"] = "late_skip"
            elif now > start - latest:
                r["skip"] = "past send window"
            r["due"] = not r["skip"] and send_at <= now
            rows.append(r)
    return rows


def _body(r: dict[str, Any]) -> tuple[str, str]:
    from .email_templates import reminder_sms

    kind = "24h" if r["type"] == "sms_24h" else "30m"
    return "sms", reminder_sms(title=r["title"] if r["event_type"] else "Visit", when_label=r["when_label"],
                               kind=kind, duration_minutes=r["duration"])


def deliver(r: dict[str, Any]) -> dict[str, Any]:
    from .main import PUBLIC_BASE
    from .notify import patient_reminder_email, send_sms

    if r["type"] == "email_24h":
        return patient_reminder_email(
            name=r["name"] or "there", email=r["email"], title=r["title"] if r["event_type"] else "Visit",
            when_label=r["when_label"], kind="24h", rebook_url=PUBLIC_BASE, duration_minutes=r["duration"],
            start_iso=r["start"], location_line="Video visit (telehealth) · https://meet.drmattbrown.com")
    from .spruce_api import send_spruce_sms, spruce_api_enabled

    if not spruce_api_enabled():
        return {"ok": False, "detail": "spruce not configured"}
    _, body = _body(r)
    return send_spruce_sms(r["phone"], body, idempotency_key="rem:" + r["ev"] + ":" + r["type"] + ":" + r["start"])


def _public(r: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"ev": r["ev"], "type": r["type"], "channel": r["channel"], "send_at": r["send_at"],
            "start": r["start"], "match": r["match"], "skip": r["skip"], "due": r["due"], **extra}


def tick(now: datetime | None = None, *, rows: list[dict[str, Any]] | None = None,
         force_send: bool = False) -> dict[str, Any]:
    now = now or datetime.now(TZ)
    sending = force_send or send_enabled()
    rows = plan(now) if rows is None else rows
    con = _db()
    stats = {"sending": sending, "rows": len(rows), "due": 0, "sent": 0, "failed": 0, "already": 0}
    dry = _dir() / f"dryrun-{now:%Y%m%d}.jsonl"
    seen: set[str] = set()
    if dry.exists():
        for line in dry.read_text().splitlines():
            try:
                d = json.loads(line)
                seen.add(f"{d['ev']}|{d['type']}|{d['start']}|{d['skip']}|{d.get('would_send')}")
            except Exception:
                continue

    def _dry(r: dict[str, Any], would: bool) -> None:
        k = f"{r['ev']}|{r['type']}|{r['start']}|{r['skip']}|{would}"
        if k in seen:
            return
        seen.add(k)
        with dry.open("a") as fh:
            fh.write(json.dumps(_public(r, at=now.isoformat(), would_send=would)) + "\n")
    for r in rows:
        key = f"{r['ev']}|{r['type']}|{r['start']}"
        if not r["due"]:
            if not sending:
                _dry(r, False)
            continue
        stats["due"] += 1
        if con.execute("SELECT 1 FROM sent WHERE key=? AND status='sent'", (key,)).fetchone():
            stats["already"] += 1
            continue
        if not sending:
            _dry(r, True)
            continue
        res = deliver(r)
        ok = bool(res.get("ok"))
        detail = str(res.get("request_id") or res.get("detail") or "")[:80]
        con.execute("INSERT OR REPLACE INTO sent VALUES (?,?,?,?,?,?,?)",
                    (key, r["ev"], r["type"], r["start"], "sent" if ok else "failed", detail, now.isoformat()))
        con.commit()
        stats["sent" if ok else "failed"] += 1
        with (_dir() / "sent.jsonl").open("a") as fh:
            fh.write(json.dumps(_public(r, at=now.isoformat(), ok=ok, detail=detail if not ok else "")) + "\n")
    return stats


def catchup_24h_email(day: str, expect: int, now: datetime | None = None) -> dict[str, Any]:
    """One-time (Matt t234u): 24h emails for matched visits on `day` whose 24h time already passed
    (late_skip). Refuses unless the count == expect. Recorded in the ledger under the normal key."""
    now = now or datetime.now(TZ)
    allrows = [r for r in plan(now, 2.0) if r["start"][:10] == day and r["match"] != "unmatched"
               and datetime.fromisoformat(r["send_at"]) <= now and r["type"] in ("email_24h", "sms_24h")
               and r["skip"] in ("", "late_skip")]
    rows = [r for r in allrows if r["type"] == "email_24h"]
    texts = [r for r in allrows if r["type"] == "sms_24h"]
    if len(rows) != expect:
        return {"ok": False, "count": len(rows), "detail": f"expected {expect}; nothing sent"}
    con = _db()
    for r in texts:  # no catch-up 24h texts: mark handled so the scheduler never sends them late
        con.execute("INSERT OR IGNORE INTO sent VALUES (?,?,?,?,?,?,?)",
                    (f"{r['ev']}|{r['type']}|{r['start']}", r["ev"], r["type"], r["start"], "sent",
                     "suppressed: catch-up text not sent (Matt t234u)", now.isoformat()))
    con.commit()
    out = []
    for r in rows:
        key = f"{r['ev']}|{r['type']}|{r['start']}"
        if con.execute("SELECT 1 FROM sent WHERE key=? AND status='sent'", (key,)).fetchone():
            out.append({"ev": r["ev"], "already": True})
            continue
        res = deliver(r)
        ok = bool(res.get("ok"))
        con.execute("INSERT OR REPLACE INTO sent VALUES (?,?,?,?,?,?,?)",
                    (key, r["ev"], r["type"], r["start"], "sent" if ok else "failed", "catchup", now.isoformat()))
        con.commit()
        rec = _public(dict(r, due=True), at=datetime.now(TZ).isoformat(), ok=ok, catchup=True)
        with (_dir() / "sent.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        out.append({"ev": r["ev"], "ok": ok, "at": rec["at"]})
    return {"ok": all(o.get("ok") or o.get("already") for o in out), "count": len(rows), "sent": out}


def report(days: float = 2.0, now: datetime | None = None) -> dict[str, Any]:
    from collections import Counter

    now = now or datetime.now(TZ)
    rows = plan(now, days)
    visits = {r["ev"]: r["match"] for r in rows}
    out: dict[str, Any] = {"visits": len(visits), "match": dict(Counter(visits.values()))}
    for t in TYPES:
        rs = [r for r in rows if r["type"] == t]
        out[t] = {"would_send": sum(1 for r in rs if not r["skip"]),
                  "skipped": dict(Counter(r["skip"] for r in rs if r["skip"]))}
    nxt = sorted((max(datetime.fromisoformat(r["send_at"]), now), r["type"]) for r in rows if not r["skip"])
    out["due_now_on_enable"] = sum(1 for r in rows if r["due"])
    out["first_send_times"] = [(a.astimezone(TZ).strftime("%a %-I:%M %p CT"), t) for a, t in nxt[:6]]
    return out


def test_burner(now: datetime | None = None) -> dict[str, Any]:
    """Forced send of all three types to the burner phone/email only (synthetic visit, no calendar)."""
    now = now or datetime.now(TZ)
    start = (now + timedelta(minutes=29)).replace(second=0, microsecond=0)
    base = {"ev": _h("synthetic-" + now.isoformat()), "event_id": "synthetic", "start": start.isoformat(),
            "match": "synthetic", "title": "Visit", "when_label": start.strftime("%a %b %d · %I:%M %p").replace(" 0", " "),
            "duration": 30, "event_type": "", "name": "Demo Tester Test", "email": TEST_EMAIL,
            "phone": TEST_PHONE, "consent": True, "skip": "", "due": True}
    rows = [dict(base, type=t, channel="email" if t.startswith("email") else "sms", send_at=now.isoformat())
            for t in TYPES]
    results = []
    for r in rows:
        assert r["email"] == TEST_EMAIL and r["phone"] == TEST_PHONE
        res = deliver(r)
        results.append({"type": r["type"], "ok": bool(res.get("ok")),
                        "id": str(res.get("request_id") or "")[:80], "detail": str(res.get("detail") or "")[:120]})
    return {"start": start.isoformat(), "results": results}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "tick":
        print(json.dumps({"at": datetime.now(TZ).isoformat(), **tick()}))
    elif cmd == "report":
        print(json.dumps(report(float(sys.argv[2]) if len(sys.argv) > 2 else 2.0), indent=1))
    elif cmd == "catchup":
        print(json.dumps(catchup_24h_email(sys.argv[2], int(sys.argv[3])), indent=1))
    elif cmd == "test":
        print(json.dumps(test_burner(), indent=1))
