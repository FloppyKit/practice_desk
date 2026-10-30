"""Practice link bases: one place for the drmattbrown.com bases.

PRACTICE_SITE_URL   public site (default https://drmattbrown.com)
PRACTICE_VIDEO_URL  video waiting room (default https://meet.drmattbrown.com/guest)
"""
from __future__ import annotations

import os


def site_url() -> str:
    return (os.environ.get("PRACTICE_SITE_URL") or "https://drmattbrown.com").strip().rstrip("/")


def video_url() -> str:
    return (os.environ.get("PRACTICE_VIDEO_URL") or "https://meet.drmattbrown.com/guest").strip().rstrip("/")


def link_map() -> list[tuple[str, str]]:
    """Old/wrong practice links found in dot phrases -> current live link."""
    s, v = site_url(), video_url()
    return [
        ("https://doxy.me/drmattbrown", v),
        ("portal.drmattbrown.com", f"{s}/portal"),
        ("www.drmattbrown.com", s),
    ]


def fix_text(body: str) -> tuple[str, int]:
    n = 0
    for old, new in link_map():
        c = body.count(old)
        if c and old not in new:
            body = body.replace(old, new)
            n += c
    return body, n
