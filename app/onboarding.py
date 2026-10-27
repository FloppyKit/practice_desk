"""New-client onboarding: intake completion flags + 'Held: paperwork due' bookings.

A new client's web/phone booking starts as a tentative calendar hold. Personal info + consents +
card on file confirms it. If the deadline passes first, the slot is released with a polite notice.
State lives in clients.sqlite (two small tables, no form answers stored). No PHI in logs.
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import clients_db

log = logging.getLogger("booker.onboarding")

HOLD_HOURS = float(os.environ.get("INTAKE_HOLD_HOURS", "48") or 48)
# Never hold past this many minutes before the visit (frees the slot when the visit is soon).
LEAD_MINUTES = float(os.environ.get("INTAKE_HOLD_LEAD_MINUTES", "120") or 120)
MIN_HOLD_MINUTES = 30.0
SWEEP_SECONDS = float(os.environ.get("INTAKE_SWEEP_SECONDS", "60") or 60)
HOLD_PREFIX = "HELD (paperwork due) · "
# Cascade order. ROI is optional (skippable); everything else confirms a held visit.
REQUIRED = ("card", "card_auth", "personal", "consents")
FORMS = ("personal", "consents", "card-auth", "roi")
NEW_SOURCE = "booker-new"


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return _utc(dt).isoformat()


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _con():
    con = clients_db.connect()
    con.execute(
        """CREATE TABLE IF NOT EXISTS intake_submissions (
          id TEXT PRIMARY KEY, client_id TEXT, email TEXT, form TEXT, link_verified INTEGER,
          received_at TEXT, pdf_path TEXT, pdf_status TEXT)"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_intake_client ON intake_submissions(client_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_intake_email ON intake_submissions(email)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS intake_holds (
          event_id TEXT PRIMARY KEY, calendar_id TEXT, client_id TEXT, email TEXT, name TEXT,
          phone TEXT, sms_consent INTEGER, title TEXT, when_label TEXT, event_type TEXT,
          start_at TEXT, created_at TEXT, deadline_at TEXT, remind_at TEXT, reminded_at TEXT,
          status TEXT, resolved_at TEXT, intake_url TEXT, summary TEXT, source TEXT)"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_holds_client ON intake_holds(client_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_holds_status ON intake_holds(status)")
    con.commit()
    return con


# ---- policy -------------------------------------------------------------------------------

def deadline_for(booked_at: datetime, start: datetime, *, hold_hours: float | None = None) -> datetime:
    """48h after booking, but never later than LEAD_MINUTES before the visit.
    A visit booked very close in gets at least MIN_HOLD_MINUTES (capped at the visit start)."""
    hours = HOLD_HOURS if hold_hours is None else hold_hours
    d = min(booked_at + timedelta(hours=hours), start - timedelta(minutes=LEAD_MINUTES))
    floor = booked_at + timedelta(minutes=MIN_HOLD_MINUTES)
    if d < floor:
        d = min(floor, start)
    return d


def remind_at_for(booked_at: datetime, deadline: datetime) -> datetime:
    return booked_at + (deadline - booked_at) / 2


def hold_phrase(booked_at: datetime, deadline: datetime) -> str:
    """'for 48 hours, until Thu Oct 29 · 4:00 PM CT' (or just 'until …' when the visit is sooner)."""
    hours = (deadline - booked_at).total_seconds() / 3600
    if hours >= HOLD_HOURS - 0.1:
        return f"for {HOLD_HOURS:g} hours, until {label(deadline)}"
    return f"until {label(deadline)}"


def label(dt: datetime, tz: str = "America/Chicago") -> str:
    from zoneinfo import ZoneInfo

    local = dt.astimezone(ZoneInfo(tz))
    return local.strftime("%a %b %d · %I:%M %p CT").replace(" 0", " ")


# ---- completion flags -------------------------------------------------------------------

def find_client(email: str = "", client_id: str = "") -> dict[str, Any] | None:
    if client_id:
        row = clients_db.get_by_id(client_id)
        if row:
            return row
    em = (email or "").strip().lower()
    return clients_db.get_by_email(em) if em else None


def record_submission(*, form: str, email: str, link_verified: bool, client_id: str = "") -> dict[str, Any]:
    row = find_client(email, client_id)
    sid = uuid.uuid4().hex
    now = _iso(datetime.now(timezone.utc))
    with _con() as con:
        con.execute(
            "INSERT INTO intake_submissions (id, client_id, email, form, link_verified, received_at, pdf_status)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, (row or {}).get("id") or "", (email or "").strip().lower(), form,
             1 if link_verified else 0, now, "pending"),
        )
    return {"id": sid, "client_id": (row or {}).get("id") or "", "received_at": now}


def set_pdf(sub_id: str, *, path: str = "", status: str = "") -> None:
    with _con() as con:
        con.execute("UPDATE intake_submissions SET pdf_path=?, pdf_status=? WHERE id=?", (path, status, sub_id))


def status(client_id: str = "", email: str = "") -> dict[str, Any]:
    row = find_client(email, client_id)
    cid = (row or {}).get("id") or ""
    em = ((row or {}).get("email") or email or "").strip().lower()
    out: dict[str, Any] = {f: {"done": False, "at": "", "pdf": ""} for f in FORMS}
    with _con() as con:
        rows = con.execute(
            "SELECT form, received_at, pdf_path FROM intake_submissions"
            " WHERE (client_id = ? AND ? != '') OR (lower(email) = ? AND ? != '') ORDER BY received_at",
            (cid, cid, em, em),
        ).fetchall()
    for r in rows:
        if r["form"] in out:
            out[r["form"]] = {"done": True, "at": r["received_at"], "pdf": r["pdf_path"] or ""}
    card = bool((row or {}).get("card_on_file"))
    flags = {
        "personal": out["personal"]["done"],
        "consents": out["consents"]["done"],
        "card": card,
        "card_auth": out["card-auth"]["done"],
        "roi": out["roi"]["done"],
    }
    return {
        "client_id": cid,
        "flags": flags,
        "forms": out,
        "complete": all(flags[k] for k in REQUIRED),
        "missing": [k for k in REQUIRED if not flags[k]],
    }


# ---- holds ----------------------------------------------------------------------------------

def needs_hold(email: str) -> tuple[bool, dict[str, Any] | None]:
    """New clients only. A client already in the phone book (not created by an earlier hold) is
    an existing client: unchanged. A booker-created client who already finished intake: no hold."""
    row = clients_db.get_by_email((email or "").strip().lower()) if email else None
    if row and (row.get("source") or "") != NEW_SOURCE:
        return False, row
    if row and status(client_id=row["id"])["complete"]:
        return False, row
    return True, row


def ensure_new_client(*, name: str, email: str, phone: str = "") -> dict[str, Any]:
    from .patients import normalize_phone

    row = clients_db.get_by_email(email.strip().lower())
    if row:
        return row
    return clients_db.create(name=name, email=email.strip().lower(),
                             phone=normalize_phone(phone) or "", source=NEW_SOURCE)


def create_hold(
    *, event_id: str, calendar_id: str, client_id: str, email: str, name: str, phone: str,
    sms_consent: bool, title: str, when_label: str, event_type: str, start: datetime,
    booked_at: datetime, deadline: datetime, intake_url: str, summary: str, source: str,
) -> dict[str, Any]:
    with _con() as con:
        con.execute(
            "INSERT OR REPLACE INTO intake_holds (event_id, calendar_id, client_id, email, name, phone,"
            " sms_consent, title, when_label, event_type, start_at, created_at, deadline_at, remind_at,"
            " reminded_at, status, resolved_at, intake_url, summary, source)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,'held',NULL,?,?,?)",
            (event_id, calendar_id, client_id, email.strip().lower(), name, phone, 1 if sms_consent else 0,
             title, when_label, event_type, _iso(start), _iso(booked_at), _iso(deadline),
             _iso(remind_at_for(booked_at, deadline)), intake_url, summary, source),
        )
    return get_hold(event_id) or {}


def get_hold(event_id: str) -> dict[str, Any] | None:
    with _con() as con:
        r = con.execute("SELECT * FROM intake_holds WHERE event_id=?", (event_id,)).fetchone()
    return dict(r) if r else None


def holds_for_client(client_id: str) -> list[dict[str, Any]]:
    with _con() as con:
        rows = con.execute("SELECT * FROM intake_holds WHERE client_id=? ORDER BY created_at DESC",
                           (client_id,)).fetchall()
    return [dict(r) for r in rows]


def holds_by_event(event_ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = [e for e in event_ids if e]
    if not ids:
        return {}
    with _con() as con:
        rows = con.execute(
            f"SELECT * FROM intake_holds WHERE event_id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
    return {r["event_id"]: dict(r) for r in rows}


def set_times(event_id: str, *, deadline: datetime, remind_at: datetime) -> dict[str, Any] | None:
    """Staff/test: move one hold's deadline. The real 48h policy is untouched."""
    with _con() as con:
        con.execute("UPDATE intake_holds SET deadline_at=?, remind_at=? WHERE event_id=? AND status='held'",
                    (_iso(deadline), _iso(remind_at), event_id))
    return get_hold(event_id)


def _claim(event_id: str, new_status: str) -> bool:
    """Atomic held -> new_status. True only for the caller that won (no double sends)."""
    with _con() as con:
        cur = con.execute(
            "UPDATE intake_holds SET status=?, resolved_at=? WHERE event_id=? AND status='held'",
            (new_status, _iso(datetime.now(timezone.utc)), event_id),
        )
        return cur.rowcount == 1


def _claim_reminder(event_id: str) -> bool:
    with _con() as con:
        cur = con.execute(
            "UPDATE intake_holds SET reminded_at=? WHERE event_id=? AND status='held' AND reminded_at IS NULL",
            (_iso(datetime.now(timezone.utc)), event_id),
        )
        return cur.rowcount == 1


# Side effects are injected so tests and the live app share one code path.
class Effects:
    def __init__(
        self,
        *,
        get_event: Callable[[str, str], dict[str, Any]],
        patch_event: Callable[..., Any],
        delete_event: Callable[[str, str], Any],
        send_email: Callable[..., Any],
        send_sms: Callable[..., Any],
        staff_email: Callable[[str, str], Any] | None = None,
        rebook_url: str = "https://book.psycharts.org/new-client",
    ) -> None:
        self.get_event = get_event
        self.patch_event = patch_event
        self.delete_event = delete_event
        self.send_email = send_email
        self.send_sms = send_sms
        self.staff_email = staff_email
        self.rebook_url = rebook_url


def _notice(fx: Effects, hold: dict[str, Any], kind: str) -> dict[str, Any]:
    from .email_templates import hold_notice

    sub, html, _text, sms = hold_notice(
        kind=kind,
        name=hold.get("name") or "",
        heading=hold.get("title") or "New client visit",
        when_label=hold.get("when_label") or "",
        hold_until_label=label(_parse(hold["deadline_at"])),
        intake_url=hold.get("intake_url") or "",
        rebook_url=fx.rebook_url,
    )
    out: dict[str, Any] = {"email": None, "sms": None}
    try:
        out["email"] = fx.send_email(hold["email"], sub, html, html=True, inline_logo=True)
    except Exception as e:  # never let a send failure undo a state change
        out["email"] = {"ok": False, "detail": str(e)[:120]}
    if hold.get("sms_consent") and hold.get("phone"):
        try:
            out["sms"] = fx.send_sms(hold["phone"], sms, kind=f"hold_{kind}",
                                     idempotency_key=f"{hold['event_id']}:hold_{kind}")
        except Exception as e:
            out["sms"] = {"ok": False, "detail": str(e)[:120]}
    return out


def _event_gone(fx: Effects, hold: dict[str, Any]) -> bool:
    try:
        ev = fx.get_event(hold["calendar_id"], hold["event_id"])
    except Exception:
        return True
    return not ev or ev.get("status") == "cancelled"


def confirm(fx: Effects, hold: dict[str, Any]) -> bool:
    if not _claim(hold["event_id"], "confirmed"):
        return False
    summary = (hold.get("summary") or "").replace(HOLD_PREFIX, "", 1) or None
    try:
        fx.patch_event(hold["calendar_id"], hold["event_id"], summary=summary, status="confirmed",
                       private_props={"hold": "confirmed"})
    except Exception as e:
        log.warning("hold confirm patch failed: %s", type(e).__name__)
    _notice(fx, hold, "confirmed")
    if fx.staff_email:
        fx.staff_email(f"[Intake] complete · visit confirmed · {hold.get('when_label')}",
                       f"Intake complete (personal, consents, card on file). The held visit on "
                       f"{hold.get('when_label')} CT is now confirmed.\nEvent: {hold['event_id']}")
    return True


def release(fx: Effects, hold: dict[str, Any]) -> bool:
    if not _claim(hold["event_id"], "released"):
        return False
    try:
        fx.delete_event(hold["calendar_id"], hold["event_id"])
    except Exception as e:
        log.warning("hold release delete failed: %s", type(e).__name__)
    _notice(fx, hold, "released")
    if fx.staff_email:
        st = status(client_id=hold.get("client_id") or "", email=hold.get("email") or "")
        fx.staff_email(f"[Intake] hold released · {hold.get('when_label')}",
                       f"The held new-client visit on {hold.get('when_label')} CT was released: intake not "
                       f"finished by the deadline (missing: {', '.join(st['missing']) or 'none'}). "
                       f"The client got a polite rebook notice.\nEvent: {hold['event_id']}")
    return True


def check_client(fx: Effects, *, client_id: str = "", email: str = "") -> list[str]:
    """Confirm every open hold for this client if intake is now complete."""
    st = status(client_id=client_id, email=email)
    if not st["complete"] or not st["client_id"]:
        return []
    done = []
    for h in holds_for_client(st["client_id"]):
        if h["status"] == "held" and confirm(fx, h):
            done.append(h["event_id"])
    return done


def sweep(fx: Effects, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    with _con() as con:
        held = [dict(r) for r in con.execute("SELECT * FROM intake_holds WHERE status='held'").fetchall()]
    out = {"checked": len(held), "confirmed": [], "reminded": [], "released": [], "gone": []}
    for h in held:
        st = status(client_id=h.get("client_id") or "", email=h.get("email") or "")
        if st["complete"]:
            if confirm(fx, h):
                out["confirmed"].append(h["event_id"])
            continue
        if now >= _parse(h["deadline_at"]):
            if _event_gone(fx, h):
                if _claim(h["event_id"], "cancelled"):
                    out["gone"].append(h["event_id"])
                continue
            if release(fx, h):
                out["released"].append(h["event_id"])
            continue
        if now >= _parse(h["remind_at"]) and not h.get("reminded_at"):
            if _event_gone(fx, h):
                if _claim(h["event_id"], "cancelled"):
                    out["gone"].append(h["event_id"])
                continue
            if _claim_reminder(h["event_id"]):
                _notice(fx, h, "reminder")
                out["reminded"].append(h["event_id"])
    return out


_thread: threading.Thread | None = None


def start_sweeper(make_fx: Callable[[], Effects]) -> None:
    global _thread
    if SWEEP_SECONDS <= 0 or (_thread and _thread.is_alive()):
        return
    stop = threading.Event()

    def run() -> None:
        while not stop.wait(SWEEP_SECONDS):
            try:
                r = sweep(make_fx())
                if r["confirmed"] or r["reminded"] or r["released"] or r["gone"]:
                    log.info("hold sweep: %s", {k: len(v) for k, v in r.items() if isinstance(v, list)})
            except Exception as e:
                log.warning("hold sweep failed: %s", type(e).__name__)

    _thread = threading.Thread(target=run, name="intake-hold-sweep", daemon=True)
    _thread.start()
