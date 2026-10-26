#!/usr/bin/env python3
"""scribe-aws: tiny 127.0.0.1 adapter that speaks the edge-scribe backend contract.

  POST /transcribe  multipart file=audio/wav (16 kHz mono s16)
      → AWS Transcribe Medical *streaming* (PRIMARYCARE, CONVERSATION, speaker labels).
        No S3, nothing written to disk. Any AWS failure → whisper-worker (same body).
      ← {"transcript", "engine": "aws-transcribe-medical"|"whisper-fallback", "aws_error"?}
  POST /note        json {transcript, duration_minutes}
      → Bedrock Nova Pro (BAA account, Bedrock API key — same mechanism as the site chat)
        with booker's own style_pack.draft_prompt_context() priors (mounted read-only)
        and the same JSON field contract as booker POST /api/desk/note/draft.
      ← {"note": SOAP + AVS (Meet again → Meds → Supplements → Other), "note_engine"}
        booker's /api/desk/note/draft calls xAI (its own comment: "not a BAA path"), so it is
        used only if DESK_DRAFT_ALLOW_NONBAA=1. Any failure → raw transcript, nothing lost.
  GET  /health

Never logs bodies. Logs only sizes, engines, and error class names.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

BIND = os.environ.get("SCRIBE_AWS_BIND", "127.0.0.1")
PORT = int(os.environ.get("SCRIBE_AWS_PORT", "8795"))
REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
SPECIALTY = os.environ.get("AWS_MEDICAL_SPECIALTY", "PRIMARYCARE")  # only value streaming accepts
WHISPER = os.environ.get("WHISPER_URL", "http://127.0.0.1:8794").rstrip("/")
# Nova Pro: 300k-token context. ~100k chars = 120-min visit; 400k chars (~100k tokens) is a sane ceiling.
NOTE_MAX_CHARS = int(os.environ.get("NOTE_MAX_CHARS", "400000"))
NOTE_MAX_TOKENS = int(os.environ.get("NOTE_MAX_TOKENS", "8000"))
DRAFT_URL = os.environ.get("DESK_DRAFT_URL", "http://127.0.0.1:8090/api/desk/note/draft")
STAFF_SECRET = (os.environ.get("DESK_STAFF_SECRET") or "").strip()
PACE = float(os.environ.get("AWS_STREAM_PACE", "2.0"))  # x realtime when sending audio
BEDROCK_TOKEN = (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
BEDROCK_REGION = os.environ.get("BEDROCK_REGION") or REGION
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-pro-v1:0")
ALLOW_NONBAA = (os.environ.get("DESK_DRAFT_ALLOW_NONBAA") or "").strip() in ("1", "true", "yes")
AWS_OFF = (os.environ.get("SCRIBE_AWS_DISABLE") or "").strip() in ("1", "true", "yes")
CHUNK = 3200  # 100 ms of 16 kHz s16 mono


def log(obj: dict[str, Any]) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **obj}), flush=True)


def aws_creds() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))


def wav_to_pcm(wav: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(wav)) as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("need mono s16 wav")
        return w.readframes(w.getnframes()), w.getframerate()


async def _aws_medical(pcm: bytes, rate: int) -> str:
    from aws_sdk_transcribe_streaming.client import AsyncTranscribeStreamingClient
    from aws_sdk_transcribe_streaming.config import AsyncTranscribeStreamingConfig
    from aws_sdk_transcribe_streaming.models import (
        AudioEvent,
        AudioStreamAudioEvent,
        MedicalTranscriptResultStreamTranscriptEvent,
        StartMedicalStreamTranscriptionInput,
    )
    # Credentials: standard AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from env_file.
    # Duplex event streams need the CRT (HTTP/2) transport, not the aiohttp default.
    from smithy_http.aio.crt import AWSCRTHTTPClient

    cfg = await AsyncTranscribeStreamingConfig.resolve(region=REGION, transport=AWSCRTHTTPClient())
    client = AsyncTranscribeStreamingClient(config=cfg)
    stream = await client.start_medical_stream_transcription(
        input=StartMedicalStreamTranscriptionInput(
            language_code="en-US",
            media_sample_rate_hertz=rate,
            media_encoding="pcm",
            specialty=SPECIALTY,
            type="CONVERSATION",
            show_speaker_label=True,
        )
    )
    lines: list[str] = []

    async def send() -> None:
        step = CHUNK / (rate * 2) / max(PACE, 0.1)
        for i in range(0, len(pcm), CHUNK):
            await stream.input_stream.send(
                AudioStreamAudioEvent(value=AudioEvent(audio_chunk=pcm[i : i + CHUNK]))
            )
            await asyncio.sleep(step)
        await stream.input_stream.close()

    async def recv() -> None:
        _, out = await stream.await_output()
        async for ev in out:
            if not isinstance(ev, MedicalTranscriptResultStreamTranscriptEvent):
                continue
            for res in ev.value.transcript.results or []:
                if res.is_partial or not res.alternatives:
                    continue
                alt = res.alternatives[0]
                spk = ""
                for it in alt.items or []:
                    if getattr(it, "speaker", None):
                        spk = it.speaker
                        break
                txt = (alt.transcript or "").strip()
                if txt:
                    lines.append((f"spk_{spk}: " if spk != "" else "") + txt)

    async with stream:
        await asyncio.gather(send(), recv())
    # merge consecutive lines from the same speaker
    merged: list[str] = []
    for ln in lines:
        head = ln.split(": ", 1)[0] if ln.startswith("spk_") else ""
        if merged and head and merged[-1].startswith(head + ": "):
            merged[-1] += " " + ln.split(": ", 1)[1]
        else:
            merged.append(ln)
    return "\n".join(merged).strip()


def _post(url: str, body: bytes, headers: dict[str, str], timeout: int = 150) -> dict[str, Any]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode() or "{}")


def _errname(e: BaseException) -> str:
    code = getattr(e, "code", None) or getattr(e, "error_code", None) or ""
    msg = str(e)
    # keep it short and secret-free: class + first clause of message
    if "status:" in msg:  # smithy CallError: "... - status: 403 - id: ...#AccessDeniedException"
        short = msg[msg.index("status:") :][:120]
    else:
        short = msg.split(" because ")[0][:90]
    if "arn:aws" in short:
        short = short.split("arn:aws")[0] + "arn:…"
    return f"{type(e).__name__}{(':' + str(code)) if code else ''}: {short}"[:160]


def do_transcribe(raw: bytes, ctype: str) -> dict[str, Any]:
    audio = _parse_multipart(raw, ctype) if "multipart/form-data" in ctype.lower() else raw
    aws_err = ""
    if AWS_OFF:
        aws_err = "disabled"
    elif not aws_creds():
        aws_err = "no_credentials"
    else:
        t0 = time.time()
        try:
            pcm, rate = wav_to_pcm(audio)
            text = asyncio.run(asyncio.wait_for(_aws_medical(pcm, rate), timeout=140))
            if text:
                log({"event": "aws_ok", "bytes": len(pcm), "chars": len(text), "sec": round(time.time() - t0, 1)})
                return {"transcript": text, "engine": "aws-transcribe-medical"}
            aws_err = "empty"
        except Exception as e:  # noqa: BLE001 — any AWS failure falls back
            aws_err = _errname(e)
    log({"event": "aws_fallback", "aws_error": aws_err})
    out = _post(WHISPER + "/transcribe", raw, {"Content-Type": ctype})
    text = (out.get("transcript") or out.get("text") or "").strip()
    log({"event": "whisper_ok", "chars": len(text)})
    return {"transcript": text, "engine": "whisper-fallback", "aws_error": aws_err}


def _avs(fields: dict[str, str], minutes: int) -> str:
    f = {k: (fields.get(k) or "").strip() for k in fields}
    def sec(title: str, body: str, keep: bool = True) -> str:
        # SOAP headers stay (shown as '—'); empty patient-facing AVS sections are omitted.
        return f"**{title}**\n{body or '—'}\n" if (body or keep) else ""
    return "\n".join(
        x for x in [
            f"## SOAP (draft · ~{minutes} min)",
            sec("Subjective", f.get("subjective", "")),
            sec("Objective", f.get("objective", "")),
            sec("Assessment", f.get("assessment", "")),
            sec("Plan", f.get("plan", "")),
            "## After-Visit Summary",
            sec("Meet again", f.get("return_visit", ""), keep=False),
            sec("Medications", f.get("medications", ""), keep=False),
            sec("Supplements", f.get("supplements", ""), keep=False),
            sec("Other recommendations", f.get("recommendations", ""), keep=False),
        ] if x
    ).strip()


_FIELDS = ("subjective", "objective", "assessment", "plan",
           "return_visit", "medications", "supplements", "recommendations")


# scribe-aws-only drug-name spellings (booker formulary is mounted read-only; it lacks some).
EXTRA_TERMS = (
    "Zoloft (sertraline), Remeron (mirtazapine), NAC (N-acetylcysteine), Wellbutrin (bupropion), "
    "Lexapro (escitalopram), Prozac (fluoxetine), Celexa (citalopram), Paxil (paroxetine), "
    "Effexor (venlafaxine), Pristiq (desvenlafaxine), Cymbalta (duloxetine), Luvox (fluvoxamine), "
    "trazodone, Klonopin (clonazepam), Ativan (lorazepam), Xanax (alprazolam), hydroxyzine, "
    "buspirone (Buspar), propranolol, prazosin, gabapentin, lithium, Lamictal (lamotrigine), "
    "Seroquel (quetiapine), Abilify (aripiprazole), Adderall (amphetamine salts), "
    "Vyvanse (lisdexamfetamine), Ritalin (methylphenidate), Concerta (methylphenidate ER), "
    "Intuniv (guanfacine), Strattera (atomoxetine), Zenzedi (dextroamphetamine), "
    "L-theanine, inositol, magnesium glycinate, omega-3 fish oil, vitamin D, melatonin"
)

def _grounded_style(style: str) -> str:
    """Keep style-pack form (voice, section order, field map, stems with {slots}, spellings);
    drop stock content (fixed advice stems, default supplement menu) so it can't leak into notes."""
    out = []
    for line in (style or "").splitlines():
        if line.startswith("Useful stems"):
            head, _, rest = line.partition(":")
            stems = [x.strip() for x in rest.split("|") if "{" in x]
            if stems:
                out.append(head + " (templates only; fill solely from the transcript): " + " | ".join(stems))
            continue
        if line.startswith("Default supplement menu"):
            continue
        if line.startswith(("Common meds", "Common supplements")):
            out.append(line.split(":", 1)[0] + " — SPELLING REFERENCE ONLY, never add one that was not discussed:"
                       + line.split(":", 1)[1])
            continue
        out.append(line)
    return "\n".join(out).strip()


def _style_ctx() -> str:
    try:
        import style_pack  # booker/app/style_pack.py, mounted read-only

        return style_pack.draft_prompt_context() if style_pack.enabled_for_draft() else ""
    except Exception:
        return ""


def _item(x: Any) -> str:
    """Model sometimes returns [{name, dose, frequency}] — render as one plain line."""
    if isinstance(x, dict):
        return " ".join(str(x[k]).strip() for k in ("name", "dose", "frequency", "instructions", "notes")
                        if x.get(k)) or json.dumps(x)
    return str(x)


def _parse_fields(text: str) -> dict[str, str]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        parsed = {"plan": text[:2000]}
    if not isinstance(parsed, dict):
        parsed = {}
    out = {}
    for k in _FIELDS:
        v = parsed.get(k) or ""
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            v = "\n".join("- " + _item(x) for x in v)
        out[k] = str(v)[:8000]
    return out


def _bedrock_fields(transcript: str) -> dict[str, str]:
    """Same prompt/field contract as booker /api/desk/note/draft, on Bedrock (BAA)."""
    style = _grounded_style(_style_ctx())
    prompt = (
        "From this visit transcript, fill a SOAP note and an after-visit summary. "
        "GROUNDING RULE: every fact, finding, med, dose, supplement, recommendation, and follow-up "
        "must come from what was actually said in the transcript. Do not add stock advice, default "
        "supplements, generic lifestyle tips, or exam/mental-status findings (appearance, grooming, "
        "affect, engagement, eye contact, etc.) unless the clinician said them. If nothing was said for "
        "a field, return an empty string for it. "
        "JSON only with keys: subjective, objective, assessment, "
        "plan, return_visit, medications, supplements, recommendations. "
        "AVS fields (return_visit/medications/supplements/recommendations) are patient language. "
        "Every value is a plain string (use '- ' lines for lists, never objects). "
        "Medications and supplements: correct drug spelling (brand and generic if said), strength, "
        "count, and frequency exactly as the clinician stated it (e.g. 'NAC 600 mg, 2 caps twice daily'; "
        "do not convert to totals the clinician did not use as the instruction); "
        "include every med and supplement the patient reports taking. "
        "Objective = only observations explicitly stated in the transcript; patient-reported info "
        "belongs in Subjective. The transcript is machine speech-to-text: fix obvious mis-hearings of "
        "drug names to the correct spelling only when context makes the drug unambiguous "
        "(e.g. 'knack' / 'in acetylcyste' -> NAC / N-acetylcysteine; 'certa line' -> sertraline).\n\n"
        "STYLE PACK use: formatting, section order, phrasing and drug-name spelling only; it is never a "
        "source of content.\n"
        + (style + "\n\n" if style else "")
        + "Drug-name spellings (reference only): " + EXTRA_TERMS + "\n\n"
        + "TRANSCRIPT:\n"
        + transcript[:NOTE_MAX_CHARS]
    )
    system = (
        "You draft psychiatry notes for this practice. Reply with JSON only. No markdown fence. "
        "Use STYLE PACK priors for form, never for content. Never invent meds, doses, findings, or "
        "recommendations absent from the transcript."
    )
    body = json.dumps(
        {
            "system": [{"text": system}],
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"temperature": 0.2, "maxTokens": NOTE_MAX_TOKENS},
        }
    ).encode()
    url = (
        f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com/model/"
        f"{urllib.parse.quote(BEDROCK_MODEL, safe='')}/converse"
    )
    out = _post(url, body, {"Content-Type": "application/json", "Authorization": "Bearer " + BEDROCK_TOKEN}, 110)
    parts = (((out.get("output") or {}).get("message") or {}).get("content")) or []
    text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
    return _parse_fields(text)


def _desk_draft_fields(transcript: str, minutes: int) -> dict[str, str]:
    out = _post(
        DRAFT_URL,
        json.dumps({"transcript": transcript, "duration": str(minutes)}).encode(),
        {"Content-Type": "application/json", "X-Staff-Secret": STAFF_SECRET},
        timeout=110,
    )
    return out.get("fields") or {} if out.get("ok") else {}


def do_note(payload: dict[str, Any]) -> dict[str, Any]:
    transcript = str(payload.get("transcript") or "").strip()
    minutes = int(payload.get("duration_minutes") or 0)
    errs: list[str] = []
    writers = []
    if BEDROCK_TOKEN:
        writers.append(("bedrock-" + BEDROCK_MODEL.split(":")[0], lambda: _bedrock_fields(transcript)))
    else:
        errs.append("bedrock:no_token")
    if ALLOW_NONBAA and STAFF_SECRET:
        writers.append(("desk-draft-xai", lambda: _desk_draft_fields(transcript, minutes)))
    for name, fn in writers if transcript else []:
        try:
            fields = fn()
            if any((v or "").strip() for v in fields.values()):
                note = _avs(fields, minutes)
                log({"event": "note_ok", "note_engine": name, "chars": len(note)})
                return {"note": note, "title": "Visit note", "note_engine": name}
            errs.append(name + ":empty")
        except urllib.error.HTTPError as e:
            errs.append(f"{name}:http_{e.code}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{name}:{type(e).__name__}")
    err = ",".join(errs) or "empty_transcript"
    log({"event": "note_fallback", "err": err[:160]})
    return {"note": transcript, "title": "Visit note", "note_engine": "raw-transcript"}


def _parse_multipart(body: bytes, content_type: str) -> bytes:
    marker = "boundary="
    if marker not in content_type:
        return b""
    boundary = content_type.split(marker, 1)[1].split(";")[0].strip().strip('"').encode()
    for part in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        if b'name="file"' in head:
            return data[:-2] if data.endswith(b"\r\n") else data
    return b""


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _json(self, code: int, obj: dict[str, Any]) -> None:
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self) -> None:
        if self.path.split("?")[0] == "/health":
            self._json(200, {"ok": True, "service": "scribe-aws", "region": REGION,
                             "aws_creds": aws_creds(), "aws_disabled": AWS_OFF,
                             "specialty": SPECIALTY, "bedrock": bool(BEDROCK_TOKEN),
                             "bedrock_model": BEDROCK_MODEL, "style_pack": bool(_style_ctx()),
                             "nonbaa_draft": ALLOW_NONBAA and bool(STAFF_SECRET)})
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 40_000_000:
            self._json(400, {"error": "bad_size"})
            return
        raw = self.rfile.read(n)
        path = self.path.split("?")[0]
        try:
            if path == "/transcribe":
                self._json(200, do_transcribe(raw, self.headers.get("Content-Type") or ""))
            elif path == "/note":
                self._json(200, do_note(json.loads(raw.decode() or "{}")))
            else:
                self._json(404, {"error": "not_found"})
        except Exception as e:  # noqa: BLE001
            log({"event": "error", "path": path, "err": type(e).__name__})
            self._json(502, {"error": type(e).__name__})


if __name__ == "__main__":
    log({"event": "boot", "region": REGION, "aws_creds": aws_creds(), "bedrock": bool(BEDROCK_TOKEN)})
    ThreadingHTTPServer((BIND, PORT), H).serve_forever()
