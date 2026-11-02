"""Shlink short links for any desk link (booking, pay, forms, intake).

The long URL is never logged. Titles are generic (no PHI). validUntil matches the
target token's expiry so a short link never outlives what it points to.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

SHLINK_BASE = os.environ.get("SHLINK_BASE", "http://127.0.0.1:8081").rstrip("/")
SHLINK_ENV = os.environ.get("SHLINK_ENV", "/secrets/shlink.env")
TITLES = {"book": "Booking link", "pay": "Payment link", "forms": "Forms link",
          "intake": "Intake link", "file": "Shared file"}


def shlink_domain() -> str:
    return (os.environ.get("SHLINK_DOMAIN") or "go.drmattbrown.com").strip()


def shlink_key() -> str:
    got = (os.environ.get("SHLINK_API_KEY") or "").strip()
    if got:
        return got
    try:
        with open(SHLINK_ENV, encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k in ("SHLINK_API_KEY", "INITIAL_API_KEY") and v:
                    return v.strip().strip("'\"")
    except OSError:
        pass
    return ""


def _post(path: str, payload: dict, key: str, method: str = "POST", opener=urlopen) -> dict:
    req = Request(SHLINK_BASE + path, data=json.dumps(payload).encode(), method=method, headers={
        "X-Api-Key": key, "Content-Type": "application/json", "Accept": "application/json"})
    with opener(req, timeout=20) as resp:
        return json.loads(resp.read().decode() or "{}")


def cloak_url(long_url: str, kind: str = "book", *, valid_until: int | None = None,
              custom_slug: str = "", opener=urlopen) -> tuple[str, bool]:
    """-> (url, cloaked). Falls back to the long URL on any failure. kind picks a PHI-free title."""
    key = shlink_key()
    if not key or not long_url.startswith("https://"):
        return long_url, False
    payload: dict = {"longUrl": long_url, "domain": shlink_domain(), "title": TITLES.get(kind, "Link"),
                     "findIfExists": True, "crawlable": False, "forwardQuery": True}
    if valid_until:
        payload["validUntil"] = datetime.fromtimestamp(int(valid_until), timezone.utc).isoformat(timespec="seconds")
    if custom_slug:
        payload["customSlug"] = re.sub(r"[^a-z0-9-]", "", custom_slug.lower())[:40]
    try:
        try:
            out = _post("/rest/v3/short-urls", payload, key, opener=opener)
        except HTTPError as e:
            if e.code not in (400, 409) or not custom_slug:
                return long_url, False
            out = _post(f"/rest/v3/short-urls/{payload['customSlug']}?domain={payload['domain']}",
                        {"longUrl": long_url, "title": payload["title"]}, key, method="PATCH", opener=opener)
        short = str(out.get("shortUrl") or "").strip()
        if short.startswith("https://"):
            return short, True
    except Exception:
        pass
    return long_url, False
