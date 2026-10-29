"""Parse the scribe-aws fixed note format into desk note fields. No model call, no network.

Format (edge/scribe-aws/note_format.py):
  ## SOAP (draft · ~N min)
  **Subjective** / **Objective** / **Assessment** / **Plan**   (— when empty)
  ## After Visit Summary
  Dr. Brown’s notes from today’s visit
  Meet again: / Medications: / Supplements: / Other recommendations:   (• bullets; phrase text indented)
"""
from __future__ import annotations

import re

_SOAP = {"subjective": "subjective", "objective": "objective", "assessment": "assessment", "plan": "plan"}
_AVS = {
    "meet again": "return_visit",
    "medications": "medications",
    "supplements": "supplements",
    "other recommendations": "recommendations",
}
FIELDS = ("subjective", "objective", "assessment", "plan", "return_visit", "medications", "supplements",
          "recommendations")


def is_structured(text: str) -> bool:
    return bool(re.search(r"(?m)^## After Visit Summary\s*$", text or "")) or bool(
        re.search(r"(?m)^\*\*(Subjective|Plan)\*\*\s*$", text or "")
    )


def _clean(lines: list[str]) -> str:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    out = "\n".join(ln.rstrip() for ln in lines)
    return "" if out.strip() in ("—", "-") else out


def parse_structured(text: str) -> dict[str, str]:
    out = {k: "" for k in FIELDS}
    body = (text or "").replace("\r", "")
    m = re.search(r"(?m)^## After Visit Summary\s*$", body)
    soap, avs = (body[: m.start()], body[m.end():]) if m else (body, "")
    cur, buf = None, []
    for ln in soap.split("\n"):
        h = re.match(r"^\*\*(Subjective|Objective|Assessment|Plan)\*\*\s*$", ln.strip())
        if h:
            if cur:
                out[cur] = _clean(buf)
            cur, buf = _SOAP[h.group(1).lower()], []
        elif ln.startswith("## "):
            continue
        elif cur:
            buf.append(ln)
    if cur:
        out[cur] = _clean(buf)
    cur, buf = None, []
    for ln in avs.split("\n"):
        h = re.match(r"^(Meet again|Medications|Supplements|Other recommendations):\s*$", ln.strip(), re.I)
        if h and not ln[:1].isspace():
            if cur:
                out[cur] = _clean(buf)
            cur, buf = _AVS[h.group(1).lower()], []
        elif cur:
            buf.append(ln)
    if cur:
        out[cur] = _clean(buf)
    return {k: v[:8000] for k, v in out.items()}
