from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(os.environ.get("BOOKER_CONFIG", "/app/config/event-types.yaml"))
SA_PATH = Path(
    os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        "/secrets/google-calendar/service-account.json",
    )
)
DEFAULT_CALENDAR = os.environ.get("GOOGLE_CALENDAR_ID", "")


_CFG_CACHE: dict[str, Any] = {"key": None, "data": None}


def load_config() -> dict[str, Any]:
    # deskfix12: YAML parse was ~45 ms and runs 3x per drawer open; cache it by file mtime/size.
    import copy

    st = CONFIG_PATH.stat()
    key = (st.st_mtime_ns, st.st_size)
    if _CFG_CACHE["key"] != key:
        with CONFIG_PATH.open() as f:
            _CFG_CACHE["data"] = yaml.safe_load(f) or {}
        _CFG_CACHE["key"] = key
    data = copy.deepcopy(_CFG_CACHE["data"])
    data.setdefault("timezone", "America/Chicago")
    data.setdefault("busy_calendars", [DEFAULT_CALENDAR] if DEFAULT_CALENDAR else [])
    data.setdefault("event_types", {})
    from .event_store import apply_overlay

    return apply_overlay(data)


def get_event_type(config: dict[str, Any], slug: str) -> dict[str, Any] | None:
    et = config.get("event_types", {}).get(slug)
    if not et or et.get("enabled") is False:
        return None
    return et
