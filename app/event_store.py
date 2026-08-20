"""Desk-editable event types. Overlay on shipped YAML. Lives in PRACTICE_DIR."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from .practice import ROOT

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
RESERVED = {
    "api",
    "desk",
    "static",
    "cal",
    "health",
    "thanks",
    "cancel",
    "book",
    "assets",
}
_SLUG = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _path():
    return ROOT / "event-types.json"


def overlay() -> dict[str, Any]:
    p = _path()
    if not p.is_file():
        return {"event_types": {}}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"event_types": {}}
    if not isinstance(raw, dict):
        return {"event_types": {}}
    types = raw.get("event_types")
    if not isinstance(types, dict):
        raw["event_types"] = {}
    return raw


def apply_overlay(cfg: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(cfg)
    types = dict(out.get("event_types") or {})
    for slug, patch in (overlay().get("event_types") or {}).items():
        if not isinstance(patch, dict):
            continue
        if patch.get("_deleted"):
            types.pop(slug, None)
            continue
        cur = dict(types.get(slug) or {})
        for k, v in patch.items():
            if k.startswith("_"):
                continue
            cur[k] = v
        types[slug] = cur
    out["event_types"] = types
    return out


def _hours(raw: Any) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for day in WEEKDAYS:
        block = raw.get(day)
        if not isinstance(block, dict):
            continue
        start = str(block.get("start") or "").strip()
        end = str(block.get("end") or "").strip()
        if not (_HHMM.match(start) and _HHMM.match(end)):
            continue
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        if (eh, em) <= (sh, sm):
            continue
        out[day] = {"start": f"{sh:02d}:{sm:02d}", "end": f"{eh:02d}:{em:02d}"}
    return out


def _durs(raw: Any) -> list[int]:
    if raw is None:
        return [30]
    if isinstance(raw, int):
        return [max(5, min(int(raw), 240))]
    if isinstance(raw, float):
        return [max(5, min(int(raw), 240))]
    if isinstance(raw, str):
        parts = re.split(r"[\s,]+", raw.strip())
        out = []
        for p in parts:
            if p.isdigit():
                out.append(max(5, min(int(p), 240)))
        return out or [30]
    if isinstance(raw, list):
        out = []
        for x in raw:
            try:
                out.append(max(5, min(int(x), 240)))
            except (TypeError, ValueError):
                continue
        return out or [30]
    return [30]


def _prices(raw: Any, durs: list[int]) -> tuple[dict[int, float], str | None, float | None]:
    by: dict[int, float] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                by[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
    if len(durs) == 1 and not by and raw is not None and not isinstance(raw, dict):
        try:
            return {}, None, float(raw)
        except (TypeError, ValueError):
            pass
    return by, None, None


def clean_slug(raw: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", str(raw or "").strip().lower()).strip("-")
    if not _SLUG.match(s) or s in RESERVED:
        raise ValueError("use a short slug like couples-60 (letters, numbers, dashes)")
    return s


def public_type(slug: str, et: dict[str, Any]) -> dict[str, Any]:
    durs = _durs(et.get("duration_minutes"))
    by = et.get("price_by_duration") or {}
    prices = {}
    for d in durs:
        if d in by or str(d) in by:
            prices[str(d)] = by.get(d, by.get(str(d)))
        elif et.get("price_usd") is not None:
            prices[str(d)] = et.get("price_usd")
    return {
        "slug": slug,
        "title": et.get("title") or slug,
        "enabled": et.get("enabled", True) is not False,
        "public": et.get("public", True) is not False,
        "duration_minutes": durs if len(durs) > 1 else durs[0],
        "durations": durs,
        "calendar_duration_minutes": et.get("calendar_duration_minutes") or None,
        "price_usd": et.get("price_usd"),
        "price_by_duration": {str(k): v for k, v in (by.items() if isinstance(by, dict) else [])},
        "price_label": et.get("price_label") or "",
        "prices": prices,
        "slot_interval_minutes": int(et.get("slot_interval_minutes") or 30),
        "minimum_notice_minutes": int(et.get("minimum_notice_minutes") or 120),
        "booking_window_days": int(et.get("booking_window_days") or 30),
        "weekly_hours": et.get("weekly_hours") or {},
        "shipped": slug in ("new-client", "follow-up", "intro-15"),
    }


def list_types(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for slug, et in (cfg.get("event_types") or {}).items():
        if not isinstance(et, dict):
            continue
        out.append(public_type(slug, et))
    out.sort(key=lambda x: (not x["enabled"], x["title"].lower()))
    return out


def save_type(cfg: dict[str, Any], slug: str, fields: dict[str, Any], *, creating: bool = False) -> dict[str, Any]:
    slug = clean_slug(slug)
    existing = (cfg.get("event_types") or {}).get(slug)
    if creating and existing:
        raise ValueError("that slug already exists")
    if not creating and not existing:
        raise ValueError("unknown service")
    durs = _durs(fields.get("duration_minutes") if "duration_minutes" in fields else (existing or {}).get("duration_minutes"))
    by_in = fields.get("price_by_duration")
    if by_in is None and "prices" in fields:
        by_in = fields.get("prices")
    by, _, single = _prices(by_in, durs)
    if not by and fields.get("price_usd") is not None:
        try:
            single = float(fields.get("price_usd"))
        except (TypeError, ValueError):
            single = None
    hours = None
    if fields.get("weekly_hours") is not None:
        hours = _hours(fields.get("weekly_hours"))
    patch: dict[str, Any] = {
        "title": str(fields.get("title") or (existing or {}).get("title") or slug).strip()[:80],
        "enabled": bool(fields.get("enabled", True)),
        "public": bool(fields.get("public", True)),
        "duration_minutes": durs if len(durs) > 1 else durs[0],
        "slot_interval_minutes": max(5, min(int(fields.get("slot_interval_minutes") or 30), 60)),
        "minimum_notice_minutes": max(0, min(int(fields.get("minimum_notice_minutes") or 120), 20160)),
        "booking_window_days": max(1, min(int(fields.get("booking_window_days") or 30), 180)),
    }
    cal = fields.get("calendar_duration_minutes")
    if cal in (None, "", 0, "0"):
        patch["calendar_duration_minutes"] = None
    else:
        try:
            patch["calendar_duration_minutes"] = max(int(durs[0] if durs else 30), min(int(cal), 300))
        except (TypeError, ValueError):
            patch["calendar_duration_minutes"] = None
    if by:
        patch["price_by_duration"] = by
        patch["price_usd"] = None
        patch["price_labels"] = {d: ("No charge" if by[d] == 0 else f"${int(by[d]) if by[d] == int(by[d]) else by[d]}") for d in by}
    elif single is not None:
        patch["price_usd"] = single
        patch["price_label"] = "No charge" if single == 0 else f"${int(single) if single == int(single) else single}"
        patch["price_by_duration"] = {}
    if creating and not hours:
        # Copy follow-up hours so a new type is bookable immediately.
        seed = (cfg.get("event_types") or {}).get("follow-up") or {}
        patch["weekly_hours"] = _hours(seed.get("weekly_hours")) or {
            "monday": {"start": "09:00", "end": "17:00"},
            "tuesday": {"start": "09:00", "end": "17:00"},
            "wednesday": {"start": "09:00", "end": "17:00"},
            "thursday": {"start": "09:00", "end": "17:00"},
            "friday": {"start": "09:00", "end": "15:00"},
        }
    elif hours is not None:
        patch["weekly_hours"] = hours
    if creating:
        cal_id = ""
        for et in (cfg.get("event_types") or {}).values():
            if isinstance(et, dict) and et.get("google_calendar_id"):
                cal_id = et.get("google_calendar_id")
                break
        if cal_id:
            patch["google_calendar_id"] = cal_id
    ov = overlay()
    types = ov.setdefault("event_types", {})
    cur = dict(types.get(slug) or {})
    cur.pop("_deleted", None)
    cur.update({k: v for k, v in patch.items() if v is not None or k in ("price_usd", "calendar_duration_minutes", "price_by_duration")})
    types[slug] = cur
    ROOT.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(ov, indent=2) + "\n", encoding="utf-8")
    merged = apply_overlay(cfg)
    return public_type(slug, (merged.get("event_types") or {}).get(slug) or cur)


def delete_type(cfg: dict[str, Any], slug: str) -> None:
    slug = str(slug or "").strip()
    if slug in ("new-client", "follow-up", "intro-15"):
        cur = dict((cfg.get("event_types") or {}).get(slug) or {})
        cur["enabled"] = False
        cur["public"] = False
        save_type(cfg, slug, cur)
        return
    ov = overlay()
    types = ov.setdefault("event_types", {})
    types[slug] = {"_deleted": True}
    ROOT.mkdir(parents=True, exist_ok=True)
    _path().write_text(json.dumps(ov, indent=2) + "\n", encoding="utf-8")
