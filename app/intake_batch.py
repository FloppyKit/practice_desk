"""Intake-link outreach to imported clients, throttled and resumable.

Who: clients with an upcoming (Confirmed) visit in the chart, soonest visit first, whose intake is not
complete. Everyone else gets no bulk email; when they book, the booker puts their personal intake link
in the confirmation automatically (main.api_book).

Throttle: at most DAILY_CAP sends per Chicago day, spaced evenly across SEND_START..SEND_END, at most one
per `tick`. Run `tick` from cron every few minutes. State lives in STATE_DIR/state.json, so it resumes.
Safety: `tick` sends only if STATE_DIR/ENABLED exists AND STATE_DIR/template.txt exists. Matt gets no
per-send copy; `summary` sends one count-only daily summary.

  python -m app.intake_batch dry-run
  python -m app.intake_batch tick
  python -m app.intake_batch summary
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import clients_db

CT = ZoneInfo("America/Chicago")
DAILY_CAP = int(os.environ.get("INTAKE_BATCH_DAILY_CAP", "100"))
SEND_START = int(os.environ.get("INTAKE_BATCH_START_HOUR", "9"))
SEND_END = int(os.environ.get("INTAKE_BATCH_END_HOUR", "17"))
STATE_DIR = Path(os.environ.get("INTAKE_BATCH_DIR") or (clients_db.DB_PATH.parent / "intake-batch"))


def _state() -> dict[str, Any]:
    p = STATE_DIR / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"sent": {}, "last_send_at": "", "summaries": {}}


def _save(st: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(STATE_DIR / "state.json")


def _log(event: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "log.jsonl", "a") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **event}) + "\n")


def upcoming_clients(now: datetime | None = None) -> list[dict[str, Any]]:
    """Clients with a Confirmed future visit in the imported chart, soonest first."""
    now = now or datetime.now(timezone.utc)
    con = sqlite3.connect(str(clients_db.DB_PATH))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT c.id, c.email, c.display_name, c.given_name, c.preferred_name, c.status, MIN(a.start_utc) AS next_visit"
        " FROM imported_appointments a JOIN clients c ON c.id = a.client_id"
        " WHERE a.status = 'Confirmed' AND a.start_utc > ? AND c.status != 'archived'"
        " GROUP BY c.id ORDER BY next_visit",
        (now.strftime("%Y-%m-%dT%H:%M:%S"),),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def plan(now: datetime | None = None) -> dict[str, Any]:
    from . import onboarding

    st = _state()
    ups = upcoming_clients(now)
    queue, no_email, complete, already = [], 0, 0, 0
    for r in ups:
        if not str(r.get("email") or "").strip():
            no_email += 1
            continue
        if r["id"] in st["sent"]:
            already += 1
            continue
        if onboarding.status(client_id=r["id"])["complete"]:
            complete += 1
            continue
        queue.append(r)
    return {"upcoming": len(ups), "no_email": no_email, "complete": complete, "already_sent": already,
            "queue": queue}


def _today(now: datetime) -> str:
    return now.astimezone(CT).strftime("%Y-%m-%d")


def _sent_on(st: dict[str, Any], day: str) -> int:
    return sum(1 for v in st["sent"].values() if v.get("day") == day)


def interval_minutes() -> float:
    return (SEND_END - SEND_START) * 60 / max(1, DAILY_CAP)


def dry_run() -> dict[str, Any]:
    p = plan()
    q = len(p["queue"])
    days = (q + DAILY_CAP - 1) // DAILY_CAP if q else 0
    return {
        "clients_with_upcoming_visit": p["upcoming"],
        "skipped_no_email": p["no_email"],
        "skipped_intake_complete": p["complete"],
        "skipped_already_sent": p["already_sent"],
        "eligible": q,
        "day1_sends": min(q, DAILY_CAP),
        "days_to_finish": days,
        "spacing_minutes": round(interval_minutes(), 1),
        "window_ct": f"{SEND_START:02d}:00-{SEND_END:02d}:00",
        "enabled": (STATE_DIR / "ENABLED").exists(),
        "template_present": (STATE_DIR / "template.txt").exists(),
    }


def _render(tpl: str, row: dict[str, Any], url: str) -> tuple[str, str]:
    first = (row.get("preferred_name") or row.get("given_name") or "").strip() or "there"
    visit = ""
    try:
        visit = datetime.fromisoformat(str(row["next_visit"]).replace("Z", "+00:00")).replace(
            tzinfo=timezone.utc).astimezone(CT).strftime("%a %b %-d")
    except Exception:
        pass
    text = tpl.replace("{first_name}", first).replace("{intake_url}", url).replace("{visit_date}", visit)
    subject, _, body = text.partition("\n")
    return subject.removeprefix("Subject:").strip(), body.lstrip("\n")


def tick(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not (STATE_DIR / "ENABLED").exists():
        return {"sent": 0, "why": "disabled"}
    tpl_path = STATE_DIR / "template.txt"
    if not tpl_path.exists():
        return {"sent": 0, "why": "no template"}
    local = now.astimezone(CT)
    if not (SEND_START <= local.hour < SEND_END):
        return {"sent": 0, "why": "outside window"}
    st = _state()
    day = _today(now)
    if _sent_on(st, day) >= DAILY_CAP:
        return {"sent": 0, "why": "daily cap"}
    if st.get("last_send_at"):
        last = datetime.fromisoformat(st["last_send_at"])
        if now - last < timedelta(minutes=interval_minutes()):
            return {"sent": 0, "why": "spacing"}
    p = plan(now)
    if not p["queue"]:
        return {"sent": 0, "why": "queue empty"}
    row = p["queue"][0]
    from .main import mint_client_intake
    from .notify import send_email

    full = clients_db.get_by_id(row["id"]) or {}
    url = mint_client_intake(full)
    subject, body = _render(tpl_path.read_text(), row, url)
    res = send_email(str(full["email"]).strip(), subject, body)
    ok = bool(res.get("ok"))
    st["last_send_at"] = now.isoformat()
    if ok:
        st["sent"][row["id"]] = {"day": day, "at": now.isoformat()}
    _save(st)
    _log({"event": "send", "client_id": row["id"], "ok": ok})
    return {"sent": 1 if ok else 0, "ok": ok}


def summary(now: datetime | None = None, *, send: bool = True) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    st = _state()
    day = _today(now)
    p = plan(now)
    out = {"day": day, "sent_today": _sent_on(st, day), "sent_total": len(st["sent"]),
           "remaining": len(p["queue"]), "skipped_no_email": p["no_email"], "intake_complete": p["complete"]}
    if send and st["summaries"].get(day) is None:
        from .notify import default_staff_emails, send_email

        body = "\n".join(f"{k}: {v}" for k, v in out.items()) + "\n\n(Counts only. Intake-link outreach daily summary.)"
        for to in default_staff_emails():
            send_email(to, f"Intake outreach summary {day}", body)
        st["summaries"][day] = out
        _save(st)
    return out


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "dry-run"
    if cmd == "dry-run":
        print(json.dumps(dry_run(), indent=1))
    elif cmd == "tick":
        print(json.dumps(tick()))
    elif cmd == "summary":
        print(json.dumps(summary()))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
