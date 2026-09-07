"""scripts/modejson.py — demo vs live in one JSON object."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import modejson  # noqa: E402


def test_default_is_demo() -> None:
    assert modejson.mode_from_env("") == "demo"
    assert modejson.mode_from_env("demo") == "demo"


def test_live_aliases() -> None:
    assert modejson.mode_from_env("live") == "live"
    assert modejson.mode_from_env("PROD") == "live"
    assert modejson.mode_from_env("production") == "live"


def test_json_shape() -> None:
    demo = json.dumps({"mode": modejson.mode_from_env("demo")}, separators=(",", ":"))
    live = json.dumps({"mode": modejson.mode_from_env("live")}, separators=(",", ":"))
    assert demo == '{"mode":"demo"}'
    assert live == '{"mode":"live"}'


def test_desk_mode_env() -> None:
    old = os.environ.get("DESK_MODE")
    os.environ["DESK_MODE"] = "production"
    try:
        assert modejson.mode_from_env() == "live"
    finally:
        if old is None:
            os.environ.pop("DESK_MODE", None)
        else:
            os.environ["DESK_MODE"] = old
