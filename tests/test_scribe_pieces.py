import importlib.util
import sys
import types
from pathlib import Path


def _load():
    sys.modules.setdefault("backends", types.SimpleNamespace(
        backend_is_phi=lambda: True, backend_kind=lambda: "t", note_configured=lambda: False,
        phi_only=lambda: True, stt_configured=lambda: True, transcribe=lambda b: {}, write_note=lambda t, m: {}))
    spec = importlib.util.spec_from_file_location("scribe_mod", Path(__file__).parent.parent / "edge/scribe/scribe.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_tracker_cut_rules():
    m = _load()
    t = m.PieceTracker(gap=60)
    assert t.tick(0, set()) == []                       # host alone: nothing
    assert t.tick(1, {"a"}) == ["open"]
    assert t.tick(10, set()) == ["pause"]               # A drops
    assert t.tick(40, {"a"}) == ["resume"]              # reconnect within 60 s: same piece
    assert t.tick(50, set()) == ["pause"]
    assert t.tick(111, set()) == ["cut"]                # gone > 60 s
    assert t.tick(120, {"b"}) == ["open"]
    assert t.tick(130, {"c"}) == ["cut", "open"]        # swapped guests between ticks
    assert t.tick(140, set()) == ["pause"]
    assert t.tick(150, {"d"}) == ["cut", "open"]        # different guest inside the gap


def test_guest_key_and_stamp():
    m = _load()
    assert m.guest_key("demo-tester-a1b2c3") == m.guest_key("demo-tester-0f9e8d") == "demo-tester"
    assert m.ct_stamp(1762117200).endswith("CT")
