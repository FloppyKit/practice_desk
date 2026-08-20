"""Staff secret from header or cookie. Query is a leftover fallback."""
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
        cookie = request.cookies.get(COOKIE) or ""
        if not query:
            try:
                query = request.query_params.get("secret") or ""
            except Exception:
                query = ""
    return pick_staff_secret(header=header, cookie=cookie, query=query)
