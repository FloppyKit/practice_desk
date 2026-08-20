"""Practice note templates as section boxes. Not PHI."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SHIPPED = Path(__file__).resolve().parent.parent / "config" / "note-templates"
OVERRIDE = Path(os.environ.get("NOTE_TEMPLATES_DIR") or "/data/logs/note-templates")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
FIELD_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")
TYPES = ("date", "line", "paragraph")

KIND_GUESS = {
    "dos": "date",
    "date": "date",
    "return_visit": "line",
    "name": "line",
    "dob": "date",
    "physician": "line",
    "duration": "line",
}


def _slug(label: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "field").lower()).strip("_") or "field"
    if not ID_RE.match(base):
        base = "field"
    slug = base
    n = 2
    while slug in used:
        slug = f"{base}_{n}"
        n += 1
    used.add(slug)
    return slug


def _clean_sections(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    used: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw[:40]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("id") or "Field").strip()[:60]
        typ = str(item.get("type") or "paragraph").strip().lower()
        if typ not in TYPES:
            typ = "paragraph"
        fid = str(item.get("id") or "").strip().lower()
        if not ID_RE.match(fid):
            fid = _slug(label, used)
        elif fid in used:
            fid = _slug(fid, used)
        else:
            used.add(fid)
        out.append({"id": fid, "label": label or fid, "type": typ})
    return out


def _from_markdown(tid: str, text: str) -> dict[str, Any]:
    title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), tid)
    used: set[str] = set()
    sections = []
    for fid in FIELD_RE.findall(text):
        if fid in used:
            continue
        used.add(fid)
        sections.append(
            {
                "id": fid,
                "label": fid.replace("_", " ").title(),
                "type": KIND_GUESS.get(fid, "paragraph"),
            }
        )
    return {
        "id": tid,
        "title": title,
        "body": text,
        "fields": list(used),
        "sections": sections,
        "custom": False,
    }


def _paths(tid: str) -> tuple[Path, Path, Path, Path]:
    return (
        OVERRIDE / f"{tid}.json",
        SHIPPED / f"{tid}.json",
        OVERRIDE / f"{tid}.md",
        SHIPPED / f"{tid}.md",
    )


def _order_path() -> Path:
    return OVERRIDE / "_order.json"


def load_order() -> list[str]:
    p = _order_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if isinstance(x, str) and ID_RE.match(x)]


def save_order(ids: list[str]) -> list[str]:
    known = {t["id"] for t in list_templates(apply_order=False)}
    cleaned = [i for i in ids if i in known]
    for tid in sorted(known):
        if tid not in cleaned:
            cleaned.append(tid)
    OVERRIDE.mkdir(parents=True, exist_ok=True)
    _order_path().write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
    return cleaned


def _all_ids() -> set[str]:
    ids: set[str] = set()
    for folder in (SHIPPED, OVERRIDE):
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if p.name.startswith("_"):
                continue
            if p.suffix in (".json", ".md") and ID_RE.match(p.stem):
                ids.add(p.stem)
    return ids


def list_templates(*, apply_order: bool = True) -> list[dict[str, Any]]:
    ids = _all_ids()
    ordered = load_order() if apply_order else []
    seq = [i for i in ordered if i in ids]
    seq.extend(sorted(i for i in ids if i not in seq))
    out = []
    for tid in seq:
        spec = get_template(tid)
        if spec:
            out.append(
                {
                    "id": spec["id"],
                    "title": spec["title"],
                    "fields": spec.get("fields") or [s["id"] for s in spec.get("sections") or []],
                    "sections": spec.get("sections") or [],
                    "custom": bool(spec.get("custom")),
                }
            )
    return out


def get_template(tid: str) -> dict[str, Any] | None:
    if not ID_RE.match(tid or ""):
        return None
    oj, sj, om, sm = _paths(tid)
    for p, custom in ((oj, True), (sj, False)):
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            sections = _clean_sections(data.get("sections"))
            title = str(data.get("title") or tid)
            return {
                "id": tid,
                "title": title,
                "sections": sections,
                "fields": [s["id"] for s in sections],
                "custom": custom,
            }
    for p, custom in ((om, True), (sm, False)):
        if p.is_file():
            spec = _from_markdown(tid, p.read_text(encoding="utf-8"))
            spec["custom"] = custom
            return spec
    return None


def save_template(tid: str, body: str = "", *, title: str = "", sections: list | None = None) -> dict[str, Any]:
    if not ID_RE.match(tid or ""):
        raise ValueError("bad template id")
    OVERRIDE.mkdir(parents=True, exist_ok=True)
    if sections is not None:
        cleaned = _clean_sections(sections)
        payload = {"title": (title or tid).strip()[:80], "sections": cleaned}
        (OVERRIDE / f"{tid}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    elif body:
        text = body.replace("\r\n", "\n")
        if len(text) > 40_000:
            raise ValueError("template too long")
        (OVERRIDE / f"{tid}.md").write_text(text, encoding="utf-8")
    else:
        raise ValueError("need sections or body")
    got = get_template(tid)
    if not got:
        raise ValueError("save failed")
    return got


def render_template(tid: str, fields: dict[str, str]) -> str:
    spec = get_template(tid)
    if not spec:
        raise ValueError("unknown template")
    if spec.get("body"):
        def repl(m: re.Match[str]) -> str:
            return str(fields.get(m.group(1)) or "").strip()
        return FIELD_RE.sub(repl, spec["body"]).strip() + "\n"
    lines = [f"# {spec.get('title') or tid}", ""]
    for sec in spec.get("sections") or []:
        lines.append(f"## {sec['label']}")
        lines.append(str(fields.get(sec["id"]) or "").strip())
        lines.append("")
    return "\n".join(lines)


def delete_template(tid: str) -> None:
    if not ID_RE.match(tid or ""):
        raise ValueError("bad template id")
    oj, _, om, _ = _paths(tid)
    removed = False
    for p in (oj, om):
        if p.is_file():
            p.unlink()
            removed = True
    if not removed:
        raise ValueError("can only delete templates you created")
    order = [i for i in load_order() if i != tid]
    if _order_path().is_file():
        save_order(order)
