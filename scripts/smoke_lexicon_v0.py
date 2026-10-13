#!/usr/bin/env python3
"""No-PHI smoke for Needle lexicon v0 matching (JSON only)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / "config" / "router-lexicon-v0.json"


def normalize(text: str) -> str:
    s = (text or "").lower()
    s = re.sub(r"[’']", "'", s)
    s = re.sub(r"[^a-z0-9'\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def score_trigger(norm: str, trigger: str) -> int:
    t = normalize(trigger)
    if not t:
        return 0
    if norm == t:
        return 1000 + len(t) * 10
    boundary = re.compile(rf"(?:^|\s){re.escape(t)}(?:\s|$)")
    if boundary.search(norm):
        return 500 + len(t) * 10
    if t in norm:
        return 200 + len(t) * 5
    words = t.split()
    if len(words) > 1 and all(re.search(rf"(?:^|\s){re.escape(w)}(?:\s|$)", norm) for w in words):
        return 120 + len(t)
    return 0


def match(text: str, doc: dict):
    norm = normalize(text)
    if not norm:
        return None
    priority = {i: n for n, i in enumerate(doc["priority"])}
    cands = []
    for intent in doc["intents"]:
        surf = intent.get("surface") or ["patient"]
        if not any(s in ("patient", "shared") for s in surf):
            continue
        if intent["id"] == "faq":
            for faq_id, phrases in (intent.get("faqTriggers") or {}).items():
                best = max((score_trigger(norm, p) for p in phrases), default=0)
                if best:
                    cands.append((best, priority.get(intent["id"], 999), intent["id"], faq_id))
            continue
        best = max((score_trigger(norm, t) for t in intent.get("triggers") or []), default=0)
        if intent["id"] == "confirm_yesno":
            if not re.fullmatch(r"(yes|y|ok|okay|do it|confirm|go|no|n|cancel|stop|nevermind|never mind)", norm):
                best = 0
        if intent["id"] == "ack_smalltalk":
            thanks = bool(re.search(r"\b(thanks|thank you|thx|ty|appreciate)\b", norm))
            greet = bool(re.match(r"^(hi|hello|hey)\b", norm) or re.match(r"^good (morning|afternoon|evening)\b", norm))
            if not (thanks or greet) or len(norm.split()) > 8:
                best = min(best, 50)
            if thanks or (greet and len(norm.split()) <= 4):
                best = max(best, 400)
        if best:
            cands.append((best, priority.get(intent["id"], 999), intent["id"], None))
    if not cands:
        return None
    crisis = [c for c in cands if c[2] == "crisis_divert"]
    refuse = [c for c in cands if c[2] == "refuse" and c[0] >= 200]
    if crisis:
        chosen = max(crisis, key=lambda c: c[0])
    elif refuse:
        chosen = max(refuse, key=lambda c: c[0])
    else:
        cands.sort(key=lambda c: (-c[0], c[1]))
        chosen = cands[0]
    return {"intentId": chosen[2], "replyKey": chosen[3], "score": chosen[0]}


def main() -> int:
    doc = json.loads(JSON_PATH.read_text())
    cases = [
        ("hi", "ack_smalltalk"),
        ("I want to book", "book"),
        ("refill my zoloft", "refill"),
        ("I'm suicidal", "crisis_divert"),
        ("what do visits cost", "faq"),
        ("change my dose", "refuse"),
    ]
    print(f"lexicon {JSON_PATH} version={doc.get('version')}")
    ok = True
    for text, expect in cases:
        hit = match(text, doc)
        got = hit["intentId"] if hit else None
        if expect == "faq":
            passed = got == "faq" and hit and hit.get("replyKey") == "fees"
            detail = f" replyKey={(hit or {}).get('replyKey')}"
        else:
            passed = got == expect
            detail = ""
        status = "PASS" if passed else "FAIL"
        if not passed:
            ok = False
        print(f"{status}\t{text!r}\texpect={expect}\tgot={got}{detail}\tscore={(hit or {}).get('score')}")
    crisis = next(i for i in doc["intents"] if i["id"] == "crisis_divert")
    body = crisis["replies"]["crisis"]
    assert "911" in body and "988" in body
    refill = next(i for i in doc["intents"] if i["id"] == "refill")
    rb = refill["replies"]["request_not_guarantee"].lower()
    assert "not a guarantee" in rb
    assert "automatic" in rb or "auto" in rb
    print("crisis/refill copy checks: PASS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
