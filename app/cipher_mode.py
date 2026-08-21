"""CIPHER_P1 mode flags. Default off: legacy cleartext book path unchanged."""
from __future__ import annotations

import os


def _flag(name: str, default: str = "0") -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        raw = default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Master switch. 0 = legacy cleartext booking only."""
    return _flag("CIPHER_P1", "0")


def dual_write() -> bool:
    """When CIPHER_P1=1, also write cleartext sqlite/calendar detail as today."""
    if not enabled():
        return False
    return _flag("CIPHER_DUAL_WRITE", "1")


def occupancy() -> bool:
    """When CIPHER_P1=1, new calendar events are occupancy-only (opaque)."""
    if not enabled():
        return False
    return _flag("CIPHER_OCCUPANCY", "1")


def flags() -> dict[str, bool]:
    return {
        "enabled": enabled(),
        "dual_write": dual_write(),
        "occupancy": occupancy(),
    }
