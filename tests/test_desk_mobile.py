"""Tablet shell — GO-DESK-MOBILE. No PHI, no live clinic."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESK = ROOT / "static" / "desk.html"
LAYOUT = ROOT / "static" / "js" / "desk-layout.js"
CALL = ROOT / "static" / "js" / "desk-call.js"


def test_layout_module_exists() -> None:
    src = LAYOUT.read_text(encoding="utf-8")
    assert "deskLayout" in src
    assert "notes" in src and "office" in src
    r = subprocess.run(["node", "--check", str(LAYOUT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


def test_ipad_defaults_notes_tabs7_defaults_office() -> None:
    script = r"""
const path = require("path");
const layout = require(path.resolve("static/js/desk-layout.js"));
const ipad = layout.detect(
  { userAgent: "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Version/17.4 Mobile/15E148 Safari/604.1", platform: "iPad", maxTouchPoints: 5 },
  { innerWidth: 834, innerHeight: 1194, screen: { width: 834, height: 1194 } },
  "",
  ""
);
if (ipad !== "notes") throw new Error("ipad=" + ipad);
const ipados = layout.detect(
  { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Version/17.4 Safari/605.1.15", platform: "MacIntel", maxTouchPoints: 5 },
  { innerWidth: 1024, innerHeight: 1366, screen: { width: 1024, height: 1366 } },
  "",
  ""
);
if (ipados !== "notes") throw new Error("ipados=" + ipados);
const tab = layout.detect(
  { userAgent: "Mozilla/5.0 (Linux; Android 11; SM-T870) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36", platform: "Linux armv8l", maxTouchPoints: 5 },
  { innerWidth: 800, innerHeight: 1280, screen: { width: 800, height: 1280 } },
  "",
  ""
);
if (tab !== "office") throw new Error("tab=" + tab);
const stored = layout.detect(
  { userAgent: "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X)", platform: "iPad", maxTouchPoints: 5 },
  { innerWidth: 834, innerHeight: 1194, screen: { width: 834, height: 1194 } },
  "office",
  ""
);
if (stored !== "office") throw new Error("stored=" + stored);
const q = layout.detect(
  { userAgent: "Mozilla/5.0 (iPad; CPU OS 17_4 like Mac OS X)", platform: "iPad", maxTouchPoints: 5 },
  { innerWidth: 834, innerHeight: 1194, screen: { width: 834, height: 1194 } },
  "office",
  "?layout=notes"
);
if (q !== "notes") throw new Error("query=" + q);
const desk = layout.detect(
  { userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120", platform: "MacIntel", maxTouchPoints: 0 },
  { innerWidth: 1440, innerHeight: 900, screen: { width: 1440, height: 900 }, matchMedia: function () { return { matches: false }; } },
  "",
  ""
);
if (desk !== "desk") throw new Error("desk=" + desk);
"""
    r = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, (r.stderr or r.stdout or "node layout detect failed")


def test_desk_html_tablet_shell() -> None:
    html = DESK.read_text(encoding="utf-8")
    for needle in (
        'id="layout-notes"',
        'id="layout-office"',
        'id="banner-unlock"',
        'id="this-week"',
        'id="office-placeholder"',
        'id="office-join-placeholder"',
        "desk-layout.js",
        "desk-layout-notes",
        "desk-layout-office",
        "min-width:44px",
        "html.desk-touch #this-week",
        "desk-icon-label",
        "Join from this tablet",
        "Notes stay on the other device",
        "viewport-fit=cover",
    ):
        assert needle in html, f"missing {needle}"
    assert 'title="Calendar"' not in html
    assert "deskFetch" in html
    assert re.search(r'id="secret"', html)
    assert "openUnlock()" in html


def test_desk_call_secret_not_on_query() -> None:
    src = CALL.read_text(encoding="utf-8")
    assert "?secret=" not in src
    assert "&secret=" not in src
    assert ' + "secret="' not in src
    assert "X-Staff-Secret" in src
    assert "deskFetch" in src


def test_notes_layout_hides_office_pane() -> None:
    html = DESK.read_text(encoding="utf-8")
    assert "html.desk-layout-notes #video-card" in html
    assert "html.desk-layout-office #note-pop" in html
    assert "layoutMode === \"notes\") return false" in html or "layoutMode === 'notes') return false" in html


def test_no_demo_live_host_in_placeholder_path() -> None:
    html = DESK.read_text(encoding="utf-8")
    assert "function officeUsesPlaceholder" in html
    assert "function joinOfficePlaceholder" in html
    assert "if (!officeUsesPlaceholder()) startOffice()" in html
    assert "Office is off in demo" in html
    assert 'getAttribute("data-demo") === "1"' in html
    assert "<script src=\"https://live.psycharts.org/static/js/host-office.js" not in html


def test_demo_desk_page_paints_data_demo_and_skips_live_script() -> None:
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "def _demo_on" in main
    assert "data-stripped-demo-host-office" in main
    html = DESK.read_text(encoding="utf-8")
    painted = html.replace("<html", '<html data-demo="1"', 1)
    painted = painted.replace(
        'src="https://live.psycharts.org/static/js/host-office.js?v=12"',
        'src="" data-stripped-demo-host-office="1"',
    )
    assert 'data-demo="1"' in painted
    assert '<script src="https://live.psycharts.org/static/js/host-office.js?v=12">' not in painted
    assert "Office is off in demo" in html
