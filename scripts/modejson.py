#!/usr/bin/env python3
"""Print one JSON object: desk mode from env. Default demo. No network."""
from __future__ import annotations

import json
import os


def mode_from_env(raw: str | None = None) -> str:
    v = (raw if raw is not None else os.environ.get("DESK_MODE", "demo")).strip().lower()
    if v in ("live", "prod", "production"):
        return "live"
    return "demo"


def main() -> int:
    print(json.dumps({"mode": mode_from_env()}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
