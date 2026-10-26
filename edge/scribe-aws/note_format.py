"""Strict note/AVS format for scribe-aws + Matt's dot phrases (read-only snapshot).

AVS shape from 3,864 de-identified AVS (needle pack): "Dr. Brown's notes from today's visit",
then Medications: / Supplements: / Other recommendations: / Meet again:, "• " bullets,
"100mg" (no space), verbs Start/Consider/Continue/Increase/Reduce/Stop/Try; a newly started
item is usually the full dot phrase (what it does, how to take, product links).
Subjective stays free text; Objective/Assessment/Plan/AVS are rendered here from JSON.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PHRASES_PATH = Path(os.environ.get("DOT_PHRASES_PATH") or "/dot-phrases/dot-phrases.json")

# alias group -> (aliases as they may appear in an ASR transcript, dot-phrase id or None)
GROUPS: dict[str, tuple[list[str], str | None]] = {
    "NAC": (["nac", "n acetylcysteine", "n acetyl cysteine", "acetylcysteine", "knack", "nak"], "nacsupplement"),
    "Vitamin D": (["vitamin d", "vit d", "vitamin d3", "d3"], "vitdsupp"),
    "Magnesium glycinate": (["magnesium glycinate", "mag glycinate"], "magglycinate"),
    "Magnesium threonate": (["magnesium threonate", "mag threonate"], "magthreonate"),
    "Magnesium malate": (["magnesium malate"], "magmalate"),
    "Magnesium chloride": (["magnesium chloride"], "magchloride"),
    "Magnesium": (["magnesium"], "magnesium"),
    "L-Theanine": (["l theanine", "theanine"], "ltheaninesupp"),
    "Omega-3 fish oil": (["omega 3", "omega three", "omega", "fish oil", "epa"], "omega3sup"),
    "Inositol": (["inositol"], "inositolsupp"),
    "Saffron": (["saffron"], "saffronsupp"),
    "Zinc": (["zinc"], "zincsupp"),
    "Vitamin B complex": (["b complex", "b vitamin", "b vitamins", "vitamin b"], "vitbcomplex"),
    "5-HTP": (["5 htp", "five htp"], "5htpsupp"),
    "Agmatine": (["agmatine"], "agmatinesupp"),
    "Alpha GPC": (["alpha gpc"], "alphagpcsupp"),
    "Ashwagandha": (["ashwagandha"], "ashwagandhasupp"),
    "CBD": (["cbd", "cannabidiol"], "cbdsupp"),
    "Glutamine": (["glutamine"], "glutaminesupp"),
    "Lion's mane": (["lion's mane", "lions mane"], "lionsmanemush"),
    "Lemon balm": (["lemon balm"], "lemonbalm"),
    "Passionflower": (["passionflower", "passion flower"], "passionflower"),
    "Turmeric": (["turmeric", "tumeric", "curcumin"], "tumericsupp"),
    "Creatine": (["creatine"], "creatine"),
    "Probiotic": (["probiotic", "probiotics"], "startprobiotic"),
    "Folic acid": (["folic acid", "folate", "methylfolate"], "folicacid"),
    "Melatonin": (["melatonin"], None),
    "Multivitamin": (["multivitamin", "multi vitamin"], None),
    "Zoloft": (["zoloft", "sertraline"], "startzoloft"),
    "Propranolol": (["propranolol", "inderal"], "newpropranolol"),
    "Benadryl": (["benadryl", "diphenhydramine"], "benadrylmed"),
    "Ritalin": (["ritalin", "methylphenidate"], "ritalinscript"),
    "Dextromethorphan": (["dextromethorphan", "dxm"], "dextromethorphanscript"),
}


def add_brand_generic(terms: str) -> None:
    """'Zoloft (sertraline), trazodone, ...' -> alias groups (no phrase) for the mention guard."""
    for part in terms.split(","):
        part = part.strip()
        m = re.match(r"^([^()]+?)\s*(?:\(([^)]+)\))?$", part)
        if not m:
            continue
        a, b = m.group(1).strip(), (m.group(2) or "").strip()
        names = [norm(a)] + ([norm(b)] if b else [])
        if not any(norm(k) == names[0] or names[0] in GROUPS[k][0] for k in GROUPS):
            GROUPS[a] = (names, None)


def norm(s: str) -> str:
    s = (s or "").lower().replace("-", " ").replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", s)).strip()


def _has(text_n: str, alias: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", text_n) is not None


_cache: dict[str, Any] = {"mtime": None, "by_id": {}}


def phrases() -> dict[str, dict[str, str]]:
    try:
        mt = PHRASES_PATH.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != mt:
        try:
            data = json.loads(PHRASES_PATH.read_text(encoding="utf-8"))
            items = data.get("phrases", []) if isinstance(data, dict) else data
            _cache["by_id"] = {str(p.get("id")): p for p in items if isinstance(p, dict) and p.get("body")}
            _cache["mtime"] = mt
        except (OSError, json.JSONDecodeError):
            return _cache["by_id"]
    return _cache["by_id"]


def mentioned_groups(transcript: str) -> list[str]:
    t = norm(transcript)
    return [g for g, (aliases, _) in GROUPS.items() if any(_has(t, a) for a in aliases)]


def matched_phrases(transcript: str) -> list[tuple[str, str, str]]:
    """[(group, phrase_id, body)] for items mentioned in the transcript that have a dot phrase."""
    by_id = phrases()
    out = []
    for g in mentioned_groups(transcript):
        pid = GROUPS[g][1]
        if pid and pid in by_id:
            out.append((g, pid, str(by_id[pid]["body"]).strip()))
    return out


def prompt_block(transcript: str) -> str:
    m = matched_phrases(transcript)
    if not m:
        return ""
    parts = [f"[{pid}] ({g})\n{body}" for g, pid, body in m]
    return (
        "DOT PHRASES (the clinician's own patient-facing wording for items whose names occur in this "
        "transcript). Use one ONLY for an item the clinician actually starts/recommends/continues in "
        "this visit. For a newly started or newly recommended item: put the clinician's stated dose and "
        "instructions in dose/instructions (the clinician always wins; the app prints that line first), "
        "set phrase_id, and set phrase_text = the phrase's description sentences and product links copied "
        "exactly, with EVERY dosing / how-to-take / titration sentence removed (no doses or schedules in "
        "phrase_text at all). For a continued item, leave phrase_id and phrase_text empty.\n\n"
        + "\n---\n".join(parts)
    )


SCHEMA = (
    'Return JSON only with exactly these keys:\n'
    '{"subjective": "free text paragraph(s), patient report only",\n'
    ' "objective": ["observation explicitly stated by the clinician", ...] or [],\n'
    ' "assessment": [{"problem": "e.g. Anxiety", "status": "e.g. fairly stable on current regimen"}, ...],\n'
    ' "medications": [{"action": "continue|start|consider|increase|reduce|stop|try", "name": "Zoloft", '
    '"dose": "100mg", "instructions": "daily"}],\n'
    ' (dose = strength per tablet/capsule exactly as stated, e.g. 600mg; never a computed total; '
    'instructions = count/timing as stated, e.g. 2 caps twice a day with food)\n'
    ' "supplements": [{"action": "...same...", "name": "NAC", "dose": "600mg", '
    '"instructions": "2 caps twice a day with food", "phrase_id": "nacsupplement or empty", '
    '"phrase_text": "only for newly started items with a dot phrase: description + links, no dosing; else empty"}],\n'
    ' "recommendations": ["each non-med recommendation the clinician actually gave, patient language; never repeat a medication or supplement here"],\n'
    ' "return_visit": "only the interval as said, e.g. In 12 weeks; or empty"}\n'
    'Medications = prescription drugs; Supplements = OTC vitamins/supplements. Include every med and '
    'supplement the patient is taking or that was changed/started. Use brand names as the clinician '
    'said them. Doses written like 100mg / 2000 IUs.'
)


def parse(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        d = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return {"subjective": text[:4000]}
    return d if isinstance(d, dict) else {}


def has_content(f: dict[str, Any]) -> bool:
    return any(bool(v) and (not isinstance(v, str) or v.strip()) for v in f.values())


def _lst(v: Any) -> list[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [x.strip().lstrip("-•* ").strip() for x in v.splitlines() if x.strip()]
    return []


def _mg(s: str) -> str:
    return re.sub(r"(\d)\s+(mg|mcg|g)\b", r"\1\2", s)


_URL = re.compile(r"https?://\S+")


def _strip_urls(s: str, allowed: set[str]) -> str:
    """Keep only links that come from a matched dot phrase; drop a line left as a bare label."""
    out = []
    for line in s.splitlines():
        new = _URL.sub(lambda m: m.group(0) if m.group(0).rstrip(".,)") in allowed else "", line)
        if new != line and len(new.strip()) < 40:
            continue
        out.append(new.rstrip())
    return "\n".join(out)


def _mentioned(name: str, t_norm: str) -> bool:
    n = norm(name)
    if not n:
        return False
    for g, (aliases, _) in GROUPS.items():
        if n == norm(g) or n in aliases or any(_has(n, a) for a in aliases if len(a) > 3):
            if any(_has(t_norm, a) for a in aliases):
                return True
    words = [w for w in n.split() if len(w) > 3 and w not in ("vitamin", "daily", "supplement", "complex", "caps", "capsules")]
    return bool(words) and all(_has(t_norm, w) for w in words)


_DOSING = re.compile(
    r"(?i)(\d\s*(mg|mcg|g|iu|ius|caps?|capsules?|tabs?|tablets?|teaspoons?|tablespoons?)\b|\bdose\b|\bdosing\b|"
    r"\btake\b|\bstart with\b|\bstart by\b|\bincrease\b|\bdecrease\b|\btitrat|\brepeat\b|\bcaps?\b|\btabs?\b|"
    r"\bteaspoons?\b|\btablespoons?\b|\btwice\b|\bonce\b|\bdaily\b|\ba day\b|\bat night\b|\bmorning\b)")


def phrase_for(name: str) -> tuple[str, str]:
    """(phrase_id, description+links with every dosing/how-to-take sentence removed) for an item name."""
    n = norm(name)
    by_id = phrases()
    for g, (aliases, pid) in GROUPS.items():
        if pid and pid in by_id and (n == norm(g) or n in aliases or any(_has(n, a) for a in aliases if len(a) > 3)):
            out: list[str] = []
            for line in str(by_id[pid]["body"]).splitlines():
                line = line.strip()
                if not line or re.match(r"(?i)^(start|consider|continue)\b[^.]*:$", line):
                    continue
                line = re.sub(r"(?i)^(start|consider)\s+[^:.]{1,40}:\s*", "", line)
                if _URL.search(line):
                    out.append(line)
                    continue
                if line.lower() in ("or", "and"):
                    out.append(line)
                    continue
                keep = [x for x in re.split(r"(?<=[.!?])\s+", line) if x.strip() and not _DOSING.search(x)]
                if keep:
                    out.append(" ".join(keep))
            while out and out[-1].lower() in ("or", "and"):
                out.pop()
            while out and out[0].lower() in ("or", "and"):
                out.pop(0)
            return pid, "\n".join(out)
    return "", ""


_VERB = {"start": "Start", "consider": "Consider", "increase": "Increase", "reduce": "Reduce",
         "decrease": "Reduce", "stop": "Stop", "try": "Try"}


def _item_line(it: Any, allowed_urls: set[str]) -> tuple[str, str]:
    """(AVS text, one-line plan text)."""
    if not isinstance(it, dict):
        x = _mg(str(it)).strip()
        return x, x
    act = str(it.get("action") or "").strip().lower()
    name = str(it.get("name") or "").strip()
    dose = _mg(str(it.get("dose") or "").strip())
    ins = str(it.get("instructions") or "").strip()
    pid, pt = phrase_for(name) if act in ("start", "consider", "try") else ("", "")
    if act in ("increase", "reduce", "decrease") and dose:
        core = f"{_VERB[act]} {name} to {dose} {ins}"
    elif act == "stop":
        core = f"Stop {name} {ins}"
    else:
        sep = " - " if dose and re.match(r"(?i)^(\d|one|two|three|four|half|take|1/2)\b", ins) else " "
        core = f"{_VERB.get(act, '')} {name} {dose}{sep}{ins}"
    core = re.sub(r"\s+", " ", core).strip()
    if pt and act in ("start", "consider", "try", ""):
        body = [x for x in pt.splitlines() if x.strip()]
        return "\n  ".join([core] + body), core
    return core, core


def render(f: dict[str, Any], minutes: int, transcript: str) -> tuple[str, dict[str, int]]:
    t_norm = norm(transcript)
    allowed = {u.rstrip(".,)") for _, _, b in matched_phrases(transcript) for u in _URL.findall(b)}
    stats = {"dropped_unmentioned": 0, "phrases_used": 0}

    def items(key: str) -> list[tuple[str, str]]:
        out = []
        for it in _lst(f.get(key)):
            name = it.get("name", "") if isinstance(it, dict) else str(it)
            if isinstance(it, dict) and not _mentioned(name, t_norm):
                stats["dropped_unmentioned"] += 1
                continue
            avs_txt, plan_txt = _item_line(it, allowed)
            if "\n" in avs_txt:
                stats["phrases_used"] += 1
            if avs_txt:
                out.append((avs_txt, plan_txt))
        return out

    meds, supps = items("medications"), items("supplements")
    recs = [r for r in (_strip_urls(str(x), allowed).strip() for x in _lst(f.get("recommendations"))) if r]
    # a recommendation that just restates a listed med/supplement is a duplicate of that bullet
    item_groups = set(mentioned_groups(" ".join(p for _, p in meds + supps)))
    recs = [r for r in recs if not (set(mentioned_groups(r)) & item_groups)]
    ret = re.sub(r"(?i)^(please\s+)?(let's\s+)?(plan to\s+)?(meet again|follow[- ]?up|return( visit)?|see you)\s*:?\s*",
                 "", str(f.get("return_visit") or "").strip()).strip().rstrip(".")
    ret = ret[:1].upper() + ret[1:]
    obj = [str(x).strip() for x in _lst(f.get("objective")) if str(x).strip()]
    ass = []
    for a in _lst(f.get("assessment")):
        if isinstance(a, dict):
            p, s = str(a.get("problem") or "").strip(), str(a.get("status") or "").strip()
            if p or s:
                ass.append(f"{p}: {s}" if p and s else p or s)
        elif str(a).strip():
            ass.append(str(a).strip())

    def short(pairs: list[tuple[str, str]]) -> str:
        return "; ".join(p for _, p in pairs)

    soap = [f"## SOAP (draft · ~{minutes} min)", "**Subjective**", str(f.get("subjective") or "").strip() or "—", "",
            "**Objective**", "\n".join("- " + x for x in obj) or "—", "",
            "**Assessment**", "\n".join("- " + x for x in ass) or "—", "",
            "**Plan**"]
    plan = [("Medications", short(meds)), ("Supplements", short(supps)), ("Other", "; ".join(recs)),
            ("Follow-up", ret)]
    soap.append("\n".join(f"- {k}: {v}" for k, v in plan if v) or "—")
    avs = ["", "## After Visit Summary", "Dr. Brown’s notes from today’s visit"]
    if ret:
        avs += ["", "Meet again:", ret]
    for title, lines in (("Medications:", [a for a, _ in meds]), ("Supplements:", [a for a, _ in supps]),
                         ("Other recommendations:", recs)):
        if lines:
            avs += ["", title] + ["• " + x for x in lines]
    return "\n".join(soap + avs).strip(), stats
