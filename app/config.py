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


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open() as f:
        data = yaml.safe_load(f) or {}
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
