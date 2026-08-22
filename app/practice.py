"""About the practice — public identity, not PHI.

One store for desk, notes, mail, booker pages, site logo, and portal photo.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("PRACTICE_DIR") or "/data/logs/practice")
SHIPPED_LOGO = Path(__file__).resolve().parent.parent / "static" / "logo.png"

DEFAULT_PAGE_TITLES = {"ocd": "OCD", "anxiety": "Anxiety"}

PAGES = (
    ("home", "Home"),
    ("about", "About"),
    ("ocd", "OCD"),
    ("anxiety", "Anxiety"),
    ("faq", "FAQ"),
    ("contact", "Contact"),
    ("services", "Fees"),
    ("telehealth", "Telehealth"),
    ("portal", "Portal"),
    ("forms", "Forms"),
    ("pay", "Pay"),
    ("privacy", "Privacy"),
    ("terms", "Terms"),
)
PAGE_SLUGS = tuple(p[0] for p in PAGES)
KINDS = ("logo", "photo", "background") + tuple(f"bg-{s}" for s in PAGE_SLUGS)
TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MAX_BYTES = 2_500_000

DEFAULTS: dict[str, str] = {
    "display_name": "Example Practice",
    "legal_name": "",
    "clinician_name": "",
    "clinician_title": "",
    "address": "",
    "phone": "",
    "fax": "",
    "email": "",
    "website": "",
    "meet": "",
    "ein": "",
    "npi": "",
    "billing_role": "psychiatrist",
}

DEFAULT_LICENSES: list[dict[str, str]] = []
STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}
NA_STATES = {"NA", "N/A", "N.A.", "NOT APPLICABLE", "NONE"}

_TEXT = (
    "display_name",
    "legal_name",
    "clinician_name",
    "clinician_title",
    "address",
    "phone",
    "fax",
    "email",
    "website",
    "meet",
    "ein",
    "npi",
    "billing_role",
)

ROLES = ("psychiatrist", "therapist")
THEME_MODES = ("light", "dark", "paper", "mist", "ink", "slate", "custom")
THEME_TOKENS = (
    "background",
    "foreground",
    "muted",
    "border",
    "accent",
    "surface",
    "ring",
    "slot_bg",
)
DEFAULT_THEME: dict[str, Any] = {
    "mode": "light",
    "allow_toggle": True,
    "tokens": {
        "background": "#fafafa",
        "foreground": "#111111",
        "muted": "#737373",
        "border": "#e5e5e5",
        "accent": "#111111",
        "surface": "#ffffff",
        "ring": "#a3a3a3",
        "slot_bg": "#f5f5f5",
    },
}
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Matt's desk = psychiatrist. Therapist set is for a second practice.
# Sources: CMS / HHS telebehavioral table — 90791 vs 90792, 90832/34/37 vs 90833/36/38.
SERVICE_SETS: dict[str, list[dict[str, Any]]] = {
    "psychiatrist": [
        {
            "id": "fu30",
            "label": "30 min follow-up",
            "duration": 30,
            "lines": [
                {"service": "Level 3 office visit, 30 min", "code": "99213", "modifier": "95", "units": "1", "cost": "220.00", "paid": "220.00"},
                {"service": "Psychotherapy add-on, 30 min", "code": "90833", "modifier": "95", "units": "1", "cost": "45.00", "paid": "45.00"},
            ],
        },
        {
            "id": "fu60",
            "label": "60 min follow-up",
            "duration": 60,
            "lines": [
                {"service": "Level 3 office visit", "code": "99213", "modifier": "95", "units": "1", "cost": "220.00", "paid": "220.00"},
                {"service": "Psychotherapy add-on, 53+ min", "code": "90838", "modifier": "95", "units": "1", "cost": "200.00", "paid": "200.00"},
            ],
        },
        {
            "id": "init90",
            "label": "Initial consult 90 min",
            "duration": 90,
            "lines": [
                {"service": "Psychiatric diagnostic evaluation with medical services, 90 min", "code": "90792", "modifier": "95", "units": "1", "cost": "600.00", "paid": "600.00"},
            ],
        },
    ],
    "therapist": [
        {
            "id": "fu30",
            "label": "30 min therapy",
            "duration": 30,
            "lines": [
                {"service": "Psychotherapy, 30 min", "code": "90832", "modifier": "95", "units": "1", "cost": "", "paid": ""},
            ],
        },
        {
            "id": "fu45",
            "label": "45 min therapy",
            "duration": 45,
            "lines": [
                {"service": "Psychotherapy, 45 min", "code": "90834", "modifier": "95", "units": "1", "cost": "", "paid": ""},
            ],
        },
        {
            "id": "fu60",
            "label": "60 min therapy",
            "duration": 60,
            "lines": [
                {"service": "Psychotherapy, 60 min", "code": "90837", "modifier": "95", "units": "1", "cost": "", "paid": ""},
            ],
        },
        {
            "id": "init90",
            "label": "Initial consult",
            "duration": 90,
            "lines": [
                {"service": "Psychiatric diagnostic evaluation, 90 min", "code": "90791", "modifier": "95", "units": "1", "cost": "", "paid": ""},
            ],
        },
    ],
}


def _store() -> Path:
    return ROOT / "practice.json"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(raw: Any, *, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", str(raw or "")).strip()[:limit]


def _clean_ein(raw: Any) -> str:
    digits = re.sub(r"\D", "", str(raw or ""))[:9]
    if len(digits) == 9:
        return f"{digits[:2]}-{digits[2:]}"
    return digits


def _clean_npi(raw: Any) -> str:
    return re.sub(r"\D", "", str(raw or ""))[:10]


def _clean_field(key: str, raw: Any) -> str:
    if key == "ein":
        return _clean_ein(raw)
    if key == "npi":
        return _clean_npi(raw)
    if key == "billing_role":
        role = str(raw or "").strip().lower()
        return role if role in ROLES else "psychiatrist"
    return _clean_text(raw, limit=240)


def _migrate_licenses(licenses: list[dict[str, str]]) -> list[dict[str, str]]:
    out = []
    for lic in licenses:
        number = (lic.get("number") or "").strip()
        state = (lic.get("state") or "").strip()
        if number == "TW-00062" and state in ("N/A", "NA", ""):
            out.append({"state": "KS", "number": number})
        else:
            out.append(dict(lic))
    return out


def service_set(role: str = "") -> list[dict[str, Any]]:
    key = role if role in SERVICE_SETS else "psychiatrist"
    return [dict(item) for item in SERVICE_SETS[key]]


def _clean_licenses(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "").strip().upper()
        if state in NA_STATES or not state:
            state = "N/A"
        elif state not in STATE_CODES:
            state = state[:16]
        number = _clean_text(item.get("number"), limit=40)
        if not number:
            continue
        out.append({"state": state, "number": number})
    return out


def _clean_page_titles(raw: Any) -> dict[str, str]:
    out = dict(DEFAULT_PAGE_TITLES)
    if not isinstance(raw, dict):
        return out
    for key in DEFAULT_PAGE_TITLES:
        val = _clean_text(raw.get(key), limit=32)
        if val:
            out[key] = val
    return out


def pages_labeled(titles: dict[str, str] | None = None) -> list[tuple[str, str]]:
    t = titles or DEFAULT_PAGE_TITLES
    out = []
    for slug, label in PAGES:
        out.append((slug, t.get(slug) or label))
    return out


def _clean_theme(raw: Any) -> dict[str, Any]:
    base = {
        "mode": DEFAULT_THEME["mode"],
        "allow_toggle": True,
        "tokens": dict(DEFAULT_THEME["tokens"]),
    }
    if not isinstance(raw, dict):
        return base
    mode = str(raw.get("mode") or "light").strip().lower()
    base["mode"] = mode if mode in THEME_MODES else "light"
    if "allow_toggle" in raw:
        base["allow_toggle"] = bool(raw.get("allow_toggle"))
    if base["mode"] == "custom":
        base["allow_toggle"] = False
    toks = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    for key in THEME_TOKENS:
        val = str(toks.get(key) or "").strip()
        if _HEX.match(val):
            if len(val) == 4:
                val = "#" + "".join(c * 2 for c in val[1:])
            base["tokens"][key] = val.lower()
    return base


def format_licenses(licenses: list[dict[str, str]] | None = None) -> list[str]:
    """N/A prints the number only. A state prints `IL 123456789`."""
    bits: list[str] = []
    for lic in licenses or []:
        state = (lic.get("state") or "").strip()
        number = (lic.get("number") or "").strip()
        if not number:
            continue
        if state in ("N/A", "NA", ""):
            bits.append(number)
        else:
            bits.append(f"{state} {number}")
    return bits


def _asset_meta(kind: str) -> dict[str, str] | None:
    if kind not in KINDS:
        return None
    if not ROOT.is_dir():
        return None
    for p in sorted(ROOT.iterdir()):
        if p.is_file() and p.stem == kind and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            ctype = "image/jpeg" if p.suffix.lower() in {".jpg", ".jpeg"} else (
                "image/webp" if p.suffix.lower() == ".webp" else "image/png"
            )
            return {"name": p.name, "type": ctype, "bytes": str(p.stat().st_size)}
    return None


def _clean_callback(raw: Any) -> str:
    from .notify import normalize_e164

    return normalize_e164(str(raw or "")) or ""


def load() -> dict[str, Any]:
    data = dict(DEFAULTS)
    p = _store()
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict):
            for k in _TEXT:
                if raw.get(k):
                    data[k] = _clean_field(k, raw.get(k))
            if raw.get("clinician_callback"):
                data["clinician_callback"] = _clean_callback(raw.get("clinician_callback"))
            if str(raw.get("call_handset") or "").strip().lower() in ("browser", "phone"):
                data["call_handset"] = str(raw.get("call_handset")).strip().lower()
            if "licenses" in raw:
                data["licenses"] = _migrate_licenses(_clean_licenses(raw.get("licenses")))
            if raw.get("statement_next"):
                try:
                    data["statement_next"] = max(1, int(raw["statement_next"]))
                except (TypeError, ValueError):
                    pass
            if raw.get("updated"):
                data["updated"] = str(raw["updated"])
            if "theme" in raw:
                data["theme"] = _clean_theme(raw.get("theme"))
            if "page_titles" in raw:
                data["page_titles"] = _clean_page_titles(raw.get("page_titles"))
    if "licenses" not in data:
        data["licenses"] = [dict(x) for x in DEFAULT_LICENSES]
    else:
        data["licenses"] = _migrate_licenses(data.get("licenses") or [])
    data.setdefault("statement_next", 1800)
    data.setdefault("billing_role", "psychiatrist")
    if data.get("call_handset") not in ("browser", "phone"):
        data["call_handset"] = "browser"
    if data.get("billing_role") not in ROLES:
        data["billing_role"] = "psychiatrist"
    if "theme" not in data:
        data["theme"] = _clean_theme(None)
    data["page_titles"] = _clean_page_titles(data.get("page_titles"))
    data["logo"] = _asset_meta("logo")
    data["photo"] = _asset_meta("photo")
    data["backgrounds"] = {slug: _asset_meta(f"bg-{slug}") for slug in PAGE_SLUGS}
    if not any(data["backgrounds"].values()) and _asset_meta("background"):
        data["backgrounds"]["home"] = _asset_meta("background")
    return data


def _write(current: dict[str, Any]) -> dict[str, Any]:
    current["updated"] = _now()
    ROOT.mkdir(parents=True, exist_ok=True)
    payload = {k: current[k] for k in _TEXT}
    payload["licenses"] = _migrate_licenses(current.get("licenses") or [])
    payload["statement_next"] = int(current.get("statement_next") or 1800)
    payload["billing_role"] = current.get("billing_role") or "psychiatrist"
    payload["theme"] = _clean_theme(current.get("theme"))
    payload["page_titles"] = _clean_page_titles(current.get("page_titles"))
    payload["clinician_callback"] = current.get("clinician_callback") or ""
    hs = str(current.get("call_handset") or "").strip().lower()
    payload["call_handset"] = hs if hs in ("browser", "phone") else "browser"
    payload["updated"] = current["updated"]
    _store().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return load()


def save(fields: dict[str, Any]) -> dict[str, Any]:
    current = load()
    for k in _TEXT:
        if k not in fields or fields[k] is None:
            continue
        val = _clean_field(k, fields.get(k))
        if val:
            current[k] = val
    if "licenses" in fields:
        current["licenses"] = _clean_licenses(fields.get("licenses"))
    if "theme" in fields:
        current["theme"] = _clean_theme(fields.get("theme"))
    if "page_titles" in fields:
        current["page_titles"] = _clean_page_titles(fields.get("page_titles"))
    if fields.get("statement_next") is not None:
        try:
            current["statement_next"] = max(1, int(fields["statement_next"]))
        except (TypeError, ValueError):
            pass
    if "clinician_callback" in fields:
        current["clinician_callback"] = _clean_callback(fields.get("clinician_callback"))
    if "call_handset" in fields:
        hs = str(fields.get("call_handset") or "").strip().lower()
        current["call_handset"] = hs if hs in ("browser", "phone") else "browser"
    return _write(current)


def next_statement() -> str:
    current = load()
    n = int(current.get("statement_next") or 1800)
    current["statement_next"] = n + 1
    _write(current)
    return f"{n:06d}"


def public(base: str = "") -> dict[str, Any]:
    p = load()
    root = (base or "").rstrip("/")
    logo = _asset_meta("logo") or (SHIPPED_LOGO.is_file() and {"name": "logo.png"})
    photo = _asset_meta("photo")
    return {
        "ok": True,
        "name": p["display_name"],
        "legal": p["legal_name"],
        "clinician": p["clinician_name"],
        "title": p["clinician_title"],
        "address": p["address"],
        "phone": p["phone"],
        "fax": p["fax"],
        "email": p["email"],
        "website": p["website"],
        "meet": p["meet"],
        "npi": p.get("npi") or "",
        "licenses": p.get("licenses") or [],
        "logo_url": f"{root}/api/practice/logo",
        "photo_url": f"{root}/api/practice/photo" if photo else None,
        "backgrounds": {
            slug: f"{root}/api/practice/background?page={slug}"
            if (p.get("backgrounds") or {}).get(slug) or (
                slug == "home" and _asset_meta("background")
            )
            else None
            for slug in PAGE_SLUGS
        },
        "has_logo": bool(logo),
        "has_photo": bool(photo),
        "theme": _clean_theme(p.get("theme")),
        "page_titles": _clean_page_titles(p.get("page_titles")),
        "updated": p.get("updated") or "",
    }


def asset_bytes(kind: str) -> tuple[bytes, str] | None:
    if kind not in KINDS:
        return None
    meta = _asset_meta(kind)
    if meta:
        path = ROOT / meta["name"]
        if path.is_file():
            return path.read_bytes(), meta["type"]
    if kind == "logo" and SHIPPED_LOGO.is_file():
        return SHIPPED_LOGO.read_bytes(), "image/png"
    return None


def save_asset(kind: str, data: bytes, content_type: str = "") -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError("kind must be logo or photo")
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_BYTES:
        raise ValueError("image too large (max 2.5 MB)")
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not ctype or ctype == "application/octet-stream":
        if data[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ctype = "image/png"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ctype = "image/webp"
    ext = TYPES.get(ctype)
    if not ext:
        raise ValueError("use a JPEG, PNG, or WebP")
    ROOT.mkdir(parents=True, exist_ok=True)
    for old in ROOT.iterdir():
        if old.is_file() and old.stem == kind:
            old.unlink()
    dest = ROOT / f"{kind}{ext}"
    dest.write_bytes(data)
    current = load()
    return _write(current)
