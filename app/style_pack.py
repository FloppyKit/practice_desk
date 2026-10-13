"""Local style pack loader for constrained AVS drafting.

Reads config/style-pack/{formulary,phrases,sections}.json.
Never logs note bodies. Not a cloud fine-tune — retrieval priors only.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger("style_pack")


def style_pack_dir() -> Path:
    raw = (os.environ.get("STYLE_PACK_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / "config" / "style-pack"


@lru_cache(maxsize=4)
def _load_json(name: str) -> dict[str, Any]:
    path = style_pack_dir() / name
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning("style pack read %s failed: %s", name, type(e).__name__)
        return {}


def reload() -> None:
    _load_json.cache_clear()


def formulary() -> dict[str, Any]:
    return _load_json("formulary.json")


def phrases() -> dict[str, Any]:
    return _load_json("phrases.json")


def sections() -> dict[str, Any]:
    return _load_json("sections.json")


def _top_labels(items: list[Any], n: int = 25) -> list[str]:
    out: list[str] = []
    for it in items or []:
        if isinstance(it, dict):
            lab = str(it.get("label") or it.get("name") or "").strip()
        else:
            lab = str(it).strip()
        if lab:
            out.append(lab)
        if len(out) >= n:
            break
    return out


def draft_prompt_context(max_chars: int = 3500) -> str:
    """Compact priors for the note-draft LLM. Safe to put in prompts (no PHI)."""
    form = formulary()
    ph = phrases()
    sec = sections()
    meds = _top_labels(list(form.get("medications_top") or []), 30)
    supps = _top_labels(list(form.get("supplements_top") or []), 20)
    voice = ph.get("voice") if isinstance(ph.get("voice"), dict) else {}
    tone = str(voice.get("tone") or "").strip()
    avoid = voice.get("avoid") if isinstance(voice.get("avoid"), list) else []
    order = sec.get("patient_avs_order") or sec.get("chart_note_order") or []
    order_labs = []
    for item in order:
        if isinstance(item, dict):
            order_labs.append(str(item.get("label") or item.get("id") or ""))
        else:
            order_labs.append(str(item))
    order_labs = [x for x in order_labs if x]

    lines = [
        "STYLE PACK (local practice priors — prefer these; do not invent meds not in transcript):",
        f"Voice: {tone}" if tone else "Voice: direct, practical, coaching; patient-facing AVS in second person.",
    ]
    if avoid:
        lines.append("Avoid: " + "; ".join(str(a) for a in avoid[:6]))
    if order_labs:
        lines.append("Preferred AVS section order: " + " → ".join(order_labs[:10]))
    lines.append(
        "Output JSON keys only: subjective, objective, assessment, plan, "
        "return_visit, medications, supplements, recommendations."
    )
    lines.append(
        "Map: return_visit = Meet again; medications/supplements/recommendations = patient AVS body; "
        "plan can mirror recommendations briefly for SOAP."
    )
    if meds:
        lines.append("Common meds seen in this practice (use only if transcript supports): " + ", ".join(meds))
    if supps:
        lines.append("Common supplements: " + ", ".join(supps))
    stems = []
    for key in ("return_visit_stems", "med_change_stems", "lifestyle_stems"):
        vals = ph.get(key) or []
        if isinstance(vals, list):
            stems.extend(str(v) for v in vals[:4])
    if stems:
        lines.append("Useful stems (adapt, don't dump all): " + " | ".join(stems[:10]))
    defaults = ph.get("supplement_defaults") or []
    if isinstance(defaults, list) and defaults:
        lines.append("Default supplement menu if patient already on them: " + ", ".join(str(d) for d in defaults[:8]))
    text = "\n".join(lines)
    return text[:max_chars]


def enabled_for_draft() -> bool:
    return (os.environ.get("STYLE_PACK_DRAFT") or "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }
