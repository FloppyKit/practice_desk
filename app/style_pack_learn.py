"""Learn-on-sign: update local style-pack counts from scrubbed signed notes.

PHI stays on-box. Name/DOB header lines are scrubbed before mining.
Never log note bodies. Not a cloud fine-tune — retrieval pack only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("style_pack_learn")

_LOCK = threading.Lock()

NAME_LINE = re.compile(r"(?im)^\s*Name\s*:.*$")
DOB_LINE = re.compile(r"(?im)^\s*Date\s+of\s+[Bb]irth\s*:.*$")
# also bare DOB:
DOB_LINE2 = re.compile(r"(?im)^\s*DOB\s*:.*$")

MED_NAMES = re.compile(
    r"\b("
    r"Sertraline|Escitalopram|Fluoxetine|Bupropion|Venlafaxine|Duloxetine|Mirtazapine|"
    r"Trazodone|Hydroxyzine|Propranolol|Lamotrigine|Lithium|Aripiprazole|Quetiapine|"
    r"Olanzapine|Risperidone|Guanfacine|Atomoxetine|Methylphenidate|Amphetamine|"
    r"Vyvanse|Adderall|Lexapro|Zoloft|Prozac|Wellbutrin|Effexor|Cymbalta|Buspirone|"
    r"Clonazepam|Lorazepam|Alprazolam|Gabapentin|Modafinil|Armodafinil|Naltrexone|"
    r"Topiramate|Zolpidem|Prazosin|Clonidine|Vortioxetine|Vilazodone|Desvenlafaxine|"
    r"Nortriptyline|Amitriptyline|Klonopin|Intuniv|Ritalin|Zenzedi|Ketamine|"
    r"Concerta|Focalin|Strattera|Lamictal|Abilify|Seroquel|Xanax|Ativan|Ambien|"
    r"Benadryl|Diphenhydramine|Melatonin"
    r")\b",
    re.I,
)
SUPP_NAMES = re.compile(
    r"\b("
    r"Magnesium|Omega-?3|Fish Oil|Vitamin D|Vitamin B|Ashwagandha|L-Theanine|Theanine|"
    r"Melatonin|NAC|Zinc|Iron|Creatine|Glycine|Inositol|Saffron|Rhodiola|CBD|"
    r"Lion'?s? Mane|B-complex|Fish oil"
    r")\b",
    re.I,
)
DOSE = re.compile(
    r"\b([A-Za-z][A-Za-z-]{2,30})\s+(\d+(?:\.\d+)?\s?(?:mg|mcg|g|iu))\b",
    re.I,
)
HEADER = re.compile(
    r"^(After Visit Summary|Return Visit|Meet Again|Medications|Supplements|"
    r"Other Recommendations|Subjective|Objective|Assessment|Plan)\s*:?\s*$",
    re.I,
)


def enabled() -> bool:
    return (os.environ.get("STYLE_PACK_LEARN") or "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def style_pack_dir() -> Path:
    raw = (os.environ.get("STYLE_PACK_DIR") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / "config" / "style-pack"


def learned_dir() -> Path:
    raw = (os.environ.get("STYLE_PACK_LEARNED_DIR") or "").strip()
    if raw:
        p = Path(raw)
    else:
        p = Path(__file__).resolve().parent.parent / "data" / "style-pack-learned"
    p.mkdir(parents=True, exist_ok=True)
    return p


def scrub_phi_headers(text: str) -> str:
    """Strip Name/DOB lines. Keep clinical body."""
    text = NAME_LINE.sub("Name: [REDACTED]", text or "")
    text = DOB_LINE.sub("Date of birth: [REDACTED]", text)
    text = DOB_LINE2.sub("DOB: [REDACTED]", text)
    return text


def fields_to_text(fields: dict[str, Any]) -> str:
    """Flatten signed note fields into mineable text (no client ids)."""
    parts: list[str] = []
    order = [
        "return_visit",
        "subjective",
        "objective",
        "assessment",
        "plan",
        "medications",
        "supplements",
        "recommendations",
        "long_run",
        "data",
        "addendum",
    ]
    labels = {
        "return_visit": "Meet Again",
        "medications": "Medications",
        "supplements": "Supplements",
        "recommendations": "Other Recommendations",
        "subjective": "Subjective",
        "objective": "Objective",
        "assessment": "Assessment",
        "plan": "Plan",
        "long_run": "Long Run",
        "data": "Data",
        "addendum": "Addendum",
    }
    for k in order:
        v = fields.get(k)
        if not v:
            continue
        s = str(v).strip()
        if not s:
            continue
        parts.append(f"{labels.get(k, k)}:\n{s}")
    # named template values
    for k, v in (fields or {}).items():
        if k in order or k in {"name", "dob", "physician", "dos", "duration", "addendum_date"}:
            continue
        if isinstance(v, str) and v.strip():
            parts.append(f"{k}:\n{v.strip()}")
    return "\n\n".join(parts)


def _bump_list(items: list[dict], label: str, n: int = 1) -> list[dict]:
    found = False
    out = []
    for it in items:
        lab = str(it.get("label") or "")
        if lab.lower() == label.lower():
            out.append({"label": lab, "count": int(it.get("count") or 0) + n})
            found = True
        else:
            out.append(it)
    if not found:
        out.append({"label": label, "count": n})
    out.sort(key=lambda x: (-int(x.get("count") or 0), str(x.get("label") or "").lower()))
    return out[:200]


def _extract(text: str) -> dict[str, Any]:
    meds: dict[str, int] = {}
    supps: dict[str, int] = {}
    headers: dict[str, int] = {}
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        m = HEADER.match(s)
        if m:
            h = m.group(1).title()
            headers[h] = headers.get(h, 0) + 1
        for m in MED_NAMES.finditer(s):
            lab = m.group(1).title() + " (mentioned)"
            meds[lab] = meds.get(lab, 0) + 1
        for m in SUPP_NAMES.finditer(s):
            lab = m.group(1).title() + " (mentioned)"
            supps[lab] = supps.get(lab, 0) + 1
        for m in DOSE.finditer(s):
            lab = f"{m.group(1).title()} {m.group(2).lower().replace(' ', '')}"
            # rough: if looks like known supp word, count as supp
            if SUPP_NAMES.search(m.group(1)):
                supps[lab] = supps.get(lab, 0) + 1
            else:
                meds[lab] = meds.get(lab, 0) + 1
    return {"medications": meds, "supplements": supps, "headers": headers}


def _already_learned(digest: str) -> bool:
    seen = learned_dir() / "seen_hashes.txt"
    if not seen.exists():
        return False
    # file can grow; last 20k lines enough
    try:
        lines = seen.read_text(encoding="utf-8", errors="ignore").splitlines()[-20000:]
    except OSError:
        return False
    return digest in lines


def _mark_learned(digest: str) -> None:
    seen = learned_dir() / "seen_hashes.txt"
    with seen.open("a", encoding="utf-8") as f:
        f.write(digest + "\n")


def learn_from_signed_fields(fields: dict[str, Any], *, template: str = "") -> dict[str, Any]:
    """Merge scrubbed signed note into style-pack JSON. Safe to call in a background task."""
    if not enabled():
        return {"ok": False, "reason": "disabled"}
    raw = fields_to_text(fields)
    scrubbed = scrub_phi_headers(raw)
    if len(scrubbed.strip()) < 40:
        return {"ok": False, "reason": "too_short"}
    digest = hashlib.sha256(scrubbed.encode("utf-8", errors="ignore")).hexdigest()
    with _LOCK:
        if _already_learned(digest):
            return {"ok": True, "skipped": "duplicate"}
        sp = style_pack_dir()
        form_path = sp / "formulary.json"
        sec_path = sp / "sections.json"
        if not form_path.exists():
            return {"ok": False, "reason": "no_formulary"}
        try:
            formulary = json.loads(form_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("style pack formulary read failed: %s", type(e).__name__)
            return {"ok": False, "reason": "formulary_read"}
        mined = _extract(scrubbed)
        meds = list(formulary.get("medications_top") or [])
        supps = list(formulary.get("supplements_top") or [])
        for lab, n in mined["medications"].items():
            meds = _bump_list(meds, lab, n)
        for lab, n in mined["supplements"].items():
            supps = _bump_list(supps, lab, n)
        formulary["medications_top"] = meds
        formulary["supplements_top"] = supps
        formulary["last_learned_at"] = datetime.now(timezone.utc).isoformat()
        notes = list(formulary.get("notes") or [])
        marker = "Updated live via learn-on-sign (local; Name/DOB scrubbed)."
        if marker not in notes:
            notes.append(marker)
        formulary["notes"] = notes
        form_path.write_text(json.dumps(formulary, indent=2) + "\n", encoding="utf-8")

        if sec_path.exists():
            try:
                sections = json.loads(sec_path.read_text(encoding="utf-8"))
                freq = dict(sections.get("corpus_header_freq") or {})
                for h, n in mined["headers"].items():
                    freq[h] = int(freq.get(h) or 0) + n
                sections["corpus_header_freq"] = freq
                sections["last_learned_at"] = formulary["last_learned_at"]
                sec_path.write_text(json.dumps(sections, indent=2) + "\n", encoding="utf-8")
            except Exception as e:
                log.warning("style pack sections update failed: %s", type(e).__name__)

        _mark_learned(digest)
        # append tiny audit line — no body
        audit = learned_dir() / "learn_log.jsonl"
        with audit.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "at": formulary["last_learned_at"],
                        "template": (template or "")[:40],
                        "digest": digest[:16],
                        "med_keys": len(mined["medications"]),
                        "supp_keys": len(mined["supplements"]),
                    }
                )
                + "\n"
            )
    return {"ok": True, "digest": digest[:16]}


def learn_safe(fields: dict[str, Any], template: str = "") -> None:
    """Background entry: never raise into request path."""
    try:
        learn_from_signed_fields(fields, template=template)
    except Exception as e:
        log.warning("style pack learn failed: %s", type(e).__name__)
