"""Structural checks for Ive-fix S1–S4. No secrets, no PHI."""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fail(msg: str) -> None:
    print("FAIL", msg)
    sys.exit(1)


def ok(msg: str) -> None:
    print("ok", msg)


def main() -> int:
    desk = (ROOT / "static/desk.html").read_text()
    m = re.search(r'id="desk-banner">(.*?)</header>', desk, re.S)
    if not m:
        fail("desk banner missing")
    banner = m.group(1)
    left_m = re.search(r'desk-banner-left">(.*?)</div>', banner, re.S)
    if not left_m:
        fail("banner left missing")
    left_ids = re.findall(r'id="([^"]+)"', left_m.group(1))
    peers = ["open-files", "open-books", "open-practice", "theme-toggle", "open-client"]
    left_peers = [p for p in peers if p in left_ids]
    if left_peers:
        fail("banner peers still primary: " + ",".join(left_peers))
    if "open-calendar" not in left_ids or "open-office" not in left_ids or "open-mail" not in left_ids:
        fail("Calendar/Live office/Messages missing from primary banner")
    if "open-more" not in banner or "more-menu" not in banner:
        fail("More overflow missing")
    if "theme-toggle" in banner or "open-files" in banner:
        fail("theme or files still in banner")
    if "id=\"open-unlock\"" not in banner:
        fail("Unlock missing from More")
    ok("S1 banner primary Calendar + Live office + Messages + More")

    if "function clientsUrl" not in desk or "weekQuery" not in desk:
        fail("week-aware search helper missing")
    if "people_from_visits" not in (ROOT / "app/main.py").read_text():
        fail("week people not wired in API")
    ok("S2 search uses week people")

    leftover = []
    for i, line in enumerate(desk.splitlines(), 1):
        if "?secret=" in line and "never" not in line:
            leftover.append(f"desk.html:{i}")
    js_hits = []
    for path in (ROOT / "static").rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".js", ".html"}:
            continue
        if path.name == "desk-auth.js":
            continue
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if "?secret=" in line and "never" not in line:
                js_hits.append(f"{path.relative_to(ROOT)}:{i}")
            if ' + "secret="' in line or " + 'secret='" in line:
                js_hits.append(f"{path.relative_to(ROOT)}:{i}")
    call = (ROOT / "static/js/desk-call.js").read_text()
    if "?secret=" in call or "&secret=" in call or ' + "secret="' in call:
        js_hits.append("static/js/desk-call.js:query-secret")
    if leftover or js_hits:
        fail("?secret= leftover " + " ".join(leftover + js_hits))
    if 'id="secret"' not in desk or 'id="unlock"' not in desk:
        fail("unlock field missing")
    ok("S3 no ?secret= on desk fetches; unlock field kept")

    if desk.count("<h1") < 1:
        fail("desk h1 missing")
    if desk.count("<main") < 1:
        fail("desk main missing")
    if "aria-live" not in desk:
        fail("desk aria-live missing")
    if desk.count('role="dialog"') < 1:
        fail("desk dialog missing")
    if "skip-link" not in desk:
        fail("desk skip link missing")
    book = (ROOT / "static/book.html").read_text()
    if "<main" not in book:
        fail("book main missing")
    ok("S4 desk h1/main/aria-live/dialog/skip; book main")

    ast.parse((ROOT / "app/main.py").read_text())
    ast.parse((ROOT / "app/week_people.py").read_text())
    ast.parse((ROOT / "app/staff_auth.py").read_text())
    ok("python AST")

    start = desk.find("<script>\n    const $")
    if start < 0:
        fail("inline desk script missing")
    end = desk.find("</script>", start)
    src = desk[start + 8 : end]
    tmp = Path("/tmp/desk_inline.js")
    tmp.write_text(src)
    r = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        fail("desk inline JS: " + (r.stderr or r.stdout)[:300])
    ok("desk inline JS syntax")

    class P(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []
            self.void = {
                "meta",
                "link",
                "img",
                "br",
                "hr",
                "input",
                "source",
                "area",
                "base",
                "col",
                "embed",
                "wbr",
            }

        def handle_starttag(self, tag, attrs):
            if tag not in self.void:
                self.stack.append(tag)

        def handle_endtag(self, tag):
            if tag in self.void:
                return
            if tag in self.stack:
                while self.stack and self.stack[-1] != tag:
                    self.stack.pop()
                if self.stack and self.stack[-1] == tag:
                    self.stack.pop()

    for name in ("static/desk.html", "static/book.html"):
        p = P()
        p.feed((ROOT / name).read_text())
        print("html", name, "unclosed_tail", p.stack[-6:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
