"""Server-to-server staff secret: x-staff-secret header only (no cookie, no ?secret=). Browsers use desk_auth sessions."""
from __future__ import annotations

from typing import Any

HEADER = "x-staff-secret"
COOKIE = "dmb-desk"


def pick_staff_secret(
    *,
    header: str = "",
    cookie: str = "",
    query: str = "",
) -> str:
    for raw in (header, cookie, query):
        s = (raw or "").strip()
        if s:
            return s
    return ""


def secret_from_request(request: Any, query: str = "") -> str:
    header = ""
    cookie = ""
    if request is not None:
        header = request.headers.get(HEADER) or request.headers.get("X-Staff-Secret") or ""
    return pick_staff_secret(header=header)
