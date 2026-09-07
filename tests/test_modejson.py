"""scripts/modejson.py — demo vs live in one JSON object."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT / "scripts"))
import modejson  # noqa: E402


def test_default_is_demo() -> None:
    assert modejson.mode_from_env("") == "demo"
    assert modejson.mode_from_env("demo") == "demo"


def test_live_aliases() -> None:
    assert modejson.mode_from_env("live") == "live"
    assert modejson.mode_from_env("PROD") == "live"


def test_json_shape() -> None:
    body = json.dumps({"mode": modejson.mode_from_env("demo")}, separators=(",", ":"))
    assert body == '{"mode":"demo"}'
