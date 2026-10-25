"""STT + note backends. VPS never runs a model.

Contract for BYO HTTP:
  POST STT  multipart file=audio/wav  → { "transcript": "...", "note"?: "..." }
  POST NOTE json { transcript, duration_minutes, kind } → { "note": "...", "title"?: "..." }
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def backend_kind() -> str:
    return (_env("SCRIBE_BACKEND", "openrouter") or "openrouter").lower()


# Built-in phi flags (YAML on disk is docs; env can override).
_PHI = {
    "openrouter": False,
    "groq": False,
    "xai": False,
    "http": None,  # inherit
    "assemblyai": True,
    "deepgram": True,
    "azure": True,
    "aws": True,
    "local": True,
}


def backend_is_phi() -> bool:
    """True if this backend is allowed to see visit audio/transcript under SCRIBE_PHI_ONLY."""
    override = _env("SCRIBE_BACKEND_PHI")
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    flag = _PHI.get(backend_kind())
    if flag is None:
        return _env("SCRIBE_BACKEND_PHI", "0") in ("1", "true", "yes")
    return bool(flag)


def phi_only() -> bool:
    return _env("SCRIBE_PHI_ONLY") in ("1", "true", "yes")


def assert_phi_ok() -> None:
    if phi_only() and not backend_is_phi():
        raise RuntimeError(
            f"SCRIBE_PHI_ONLY=1 but backend {backend_kind()!r} is not marked PHI. "
            "Point SCRIBE_STT_URL at a BAA vendor or set SCRIBE_BACKEND_PHI=1 for a URL you trust."
        )


def stt_configured() -> bool:
    if backend_kind() == "http":
        return bool(_env("SCRIBE_STT_URL"))
    return bool(_env("OPENROUTER_API_KEY") or _env("SCRIBE_STT_URL"))


def note_configured() -> bool:
    if backend_kind() == "http":
        return bool(_env("SCRIBE_NOTE_URL"))
    return bool(_env("OPENROUTER_API_KEY") or _env("SCRIBE_NOTE_URL"))


def transcribe(wav: bytes) -> dict[str, str]:
    """Return {transcript, note?} — never log the body."""
    assert_phi_ok()
    if backend_kind() == "http" or _env("SCRIBE_STT_URL"):
        if backend_kind() == "http" or not _env("OPENROUTER_API_KEY"):
            return _http_stt(wav)
    return _openrouter_stt(wav)


def write_note(transcript: str, duration_minutes: int) -> dict[str, str]:
    assert_phi_ok()
    if backend_kind() == "http" or _env("SCRIBE_NOTE_URL"):
        if backend_kind() == "http" or not _env("OPENROUTER_API_KEY"):
            return _http_note(transcript, duration_minutes)
    return _openrouter_note(transcript, duration_minutes)


def _http_stt(wav: bytes) -> dict[str, str]:
    url = _env("SCRIBE_STT_URL")
    if not url:
        raise RuntimeError("SCRIBE_STT_URL unset")
    boundary = "scribe" + os.urandom(6).hex()
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="chunk.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    tok = _env("SCRIBE_STT_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    return _post(url, body, headers)


def _http_note(transcript: str, duration_minutes: int) -> dict[str, str]:
    url = _env("SCRIBE_NOTE_URL")
    if not url:
        raise RuntimeError("SCRIBE_NOTE_URL unset")
    payload = json.dumps(
        {"transcript": transcript, "duration_minutes": duration_minutes, "kind": "dap"}
    ).encode()
    headers = {"Content-Type": "application/json"}
    tok = _env("SCRIBE_NOTE_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    return _post(url, payload, headers)


def _openrouter_stt(wav: bytes) -> dict[str, str]:
    import base64

    key = _env("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY unset")
    model = _env("OPENROUTER_STT_MODEL", "openai/gpt-4o-mini-transcribe")
    payload = json.dumps(
        {
            "model": model,
            "input_audio": {
                "data": base64.b64encode(wav).decode("ascii"),
                "format": "wav",
            },
        }
    ).encode()
    data = _post(
        "https://openrouter.ai/api/v1/audio/transcriptions",
        payload,
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "HTTP-Referer": "https://live.psycharts.org",
            "X-Title": "Psych Arts scribe",
        },
    )
    text = (
        data.get("transcript")
        or data.get("text")
        or (data.get("output") or "")
        or ""
    )
    if isinstance(text, list):
        text = " ".join(str(x) for x in text)
    return {"transcript": str(text).strip(), "note": str(data.get("note") or "")}


SOAP_PROMPT = """You are drafting notes for Dr. Matthew Brown, D.O. from a visit transcript.
Do not invent facts. If something is unclear, say so.

Write TWO documents:

## SOAP
**Subjective**
**Objective**
**Assessment**
**Plan**

## After-Visit Summary
The AVS is the Plan in patient-facing language (meds, supplements, return visit, other recommendations). No diagnosis lecture. Short.

Keep it concise. No extra preamble."""


def _openrouter_note(transcript: str, duration_minutes: int) -> dict[str, str]:
    key = _env("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY unset")
    model = _env("OPENROUTER_NOTE_MODEL", "google/gemini-2.5-flash")
    payload = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SOAP_PROMPT},
                {
                    "role": "user",
                    "content": f"Visit length: {duration_minutes} minutes.\n\nTranscript:\n{transcript}",
                },
            ],
        }
    ).encode()
    data = _post(
        "https://openrouter.ai/api/v1/chat/completions",
        payload,
        {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
            "HTTP-Referer": "https://live.psycharts.org",
            "X-Title": "Psych Arts scribe",
        },
    )
    note = ""
    try:
        note = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        note = str(data.get("note") or data.get("text") or "")
    return {"note": str(note).strip(), "title": "Visit note"}


def _post(url: str, body: bytes, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode() or "{}"
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:240]
        raise RuntimeError(f"backend {e.code}: {detail}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"transcript": raw.strip(), "note": "", "text": raw.strip()}
    return data if isinstance(data, dict) else {"transcript": str(data)}
