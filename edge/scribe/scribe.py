#!/usr/bin/env python3
"""LiveKit RAM scribe — thin pipe.

Join office after host + guest consent. Audio only in /dev/shm.
POST chunks to a backend URL. Mail the DAP. Wipe. Never write PHI to disk.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import struct
import time
import traceback
import urllib.error
import urllib.request  # desk_ingest used these without importing (NameError → ok:false)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from backends import (
    backend_is_phi,
    backend_kind,
    note_configured,
    phi_only,
    stt_configured,
    transcribe,
    write_note,
)

SHM = Path(os.environ.get("SCRIBE_SHM", "/dev/shm"))
ROOM_NAME = os.environ.get("LIVEKIT_ROOM", "office")
LK_URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
LK_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LK_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
NOTIFY_URL = os.environ.get("NOTIFY_WEBHOOK_URL", "http://127.0.0.1:8791/notify")
NOTIFY_TOKEN = os.environ.get("NOTIFY_WEBHOOK_TOKEN", "")
MAIL_TO = os.environ.get("SCRIBE_TO", "matt@drmattbrown.com")
CHUNK_SEC = int(os.environ.get("SCRIBE_CHUNK_SEC", "90"))
POLL_SEC = float(os.environ.get("SCRIBE_POLL_SEC", "3"))
BIND = os.environ.get("SCRIBE_BIND", "127.0.0.1")
PORT = int(os.environ.get("SCRIBE_PORT", "8793"))
LOG = Path(os.environ.get("SCRIBE_LOG", "/home/deploy/edge/logs/scribe.jsonl"))

SAMPLE_RATE = 16000


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log_event(obj: dict[str, Any]) -> None:
    row = {"ts": utc(), **obj}
    line = json.dumps(row, default=str) + "\n"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(line, end="", flush=True)


def mix_pcm(bufs: list[bytes]) -> bytes:
    """Sum s16 mono tracks (host + guest) into one stream; pad short ones with silence."""
    bufs = [b[: len(b) - (len(b) % 2)] for b in bufs if b]
    if not bufs:
        return b""
    if len(bufs) == 1:
        return bufs[0]
    from array import array

    n = max(len(b) for b in bufs) // 2
    acc = [0] * n
    for b in bufs:
        a = array("h")
        a.frombytes(b)
        for i, v in enumerate(a):
            acc[i] += v
    return array("h", (32767 if v > 32767 else -32768 if v < -32768 else v for v in acc)).tobytes()


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    data_size = len(pcm)
    return (
        b"RIFF"
        + struct.pack("<I", 36 + data_size)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", data_size)
        + pcm
    )


def mail(subject: str, html: str) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    if not NOTIFY_TOKEN:
        return {"ok": False, "detail": "no notify token"}
    payload = json.dumps(
        {
            "kind": "email",
            "to": MAIL_TO,
            "subject": subject,
            "body": html,
            "html": True,
            "inline_logo": True,
        }
    ).encode()
    req = urllib.request.Request(
        NOTIFY_URL,
        data=payload,
        headers={"Content-Type": "application/json", "X-Notify-Token": NOTIFY_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "detail": f"http {e.code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:160]}




def desk_ingest(
    note: str, transcript: str, minutes: int, target: dict[str, str] | None = None
) -> dict[str, Any]:
    """Push blank draft to Practice Desk. Never log bodies."""
    url = (os.environ.get("DESK_SCRIBE_INGEST_URL") or "").strip()
    if not url:
        return {"ok": False, "detail": "unset"}
    tok = (os.environ.get("DESK_SCRIBE_INGEST_TOKEN") or "").strip()
    payload = json.dumps(
        {
            "note": note or "",
            "transcript": transcript or "",
            "title": "Visit note",
            "template": "blank",
            "duration_minutes": int(minutes or 0),
            "room": ROOM_NAME,
            # Host may tag the visit (participant attrs desk_client_id/email/name).
            "client_id": (target or {}).get("client_id", ""),
            "email": (target or {}).get("email", ""),
            "name": (target or {}).get("name", ""),
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if tok:
        headers["X-Scribe-Ingest-Token"] = tok
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode() or "{}"
        data = json.loads(raw) if raw else {}
        return {"ok": bool(data.get("ok")), "id": data.get("id") or ""}
    except Exception as e:
        return {"ok": False, "detail": type(e).__name__}


def note_html(note: str, minutes: int, stt_ok: bool) -> str:
    body = (
        note.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>\n")
        if note
        else "<p>No note produced.</p>"
    )
    warn = "" if stt_ok else "<p><em>Transcription backend failed or was unset.</em></p>"
    return (
        f"<p>Visit length about {minutes} minutes. Draft only — edit before charting.</p>"
        f"{warn}<div style='font-family:Georgia,serif;line-height:1.45'>{body}</div>"
        "<p style='color:#666;font-size:12px'>Audio was held in RAM on the visit server and wiped. "
        "Nothing clinical is stored there.</p>"
    )


class State:
    def __init__(self) -> None:
        self.joined = False
        self.last: dict[str, Any] = {"ok": True, "detail": "idle"}


STATE = State()


class Health(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        body = json.dumps(
            {
                "ok": True,
                "service": "scribe",
                "room": ROOM_NAME,
                "joined": STATE.joined,
                "stt": stt_configured(),
                "note": note_configured(),
                "backend": backend_kind(),
                "phi": backend_is_phi(),
                "phi_only": phi_only(),
                "last": STATE.last,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_health() -> None:
    httpd = ThreadingHTTPServer((BIND, PORT), Health)
    Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"scribe health http://{BIND}:{PORT}", flush=True)


def _attr(p: Any, key: str) -> str:
    attrs = getattr(p, "attributes", None) or {}
    if isinstance(attrs, dict):
        return str(attrs.get(key) or "")
    try:
        return str(attrs.get(key) or "")
    except Exception:
        return ""


def _ident(p: Any) -> str:
    return str(getattr(p, "identity", "") or "")


def is_scribe(identity: str) -> bool:
    return identity.startswith("scribe-")


def is_host(identity: str) -> bool:
    return identity.startswith("host-")


async def token_consent() -> bool:
    import urllib.request

    url = os.environ.get("SCRIBE_CONSENT_URL", "http://127.0.0.1:7882/api/scribe/ready")
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode() or "{}")
        return bool(data.get("consented"))
    except Exception:
        return False


async def room_snapshot() -> tuple[bool, bool, bool]:
    """host present, guest consented, host notes on (default on)."""
    from livekit import api

    http = LK_URL.replace("ws://", "http://").replace("wss://", "https://")
    lk = api.LiveKitAPI(http, LK_KEY, LK_SECRET)
    try:
        res = await lk.room.list_participants(api.ListParticipantsRequest(room=ROOM_NAME))
    except Exception:
        try:
            await lk.aclose()
        except Exception:
            pass
        return False, await token_consent(), True
    try:
        await lk.aclose()
    except Exception:
        pass
    parts = list(getattr(res, "participants", None) or [])
    host = any(is_host(_ident(p)) for p in parts)
    guest_attr = any(
        (not is_host(_ident(p)))
        and (not is_scribe(_ident(p)))
        and _attr(p, "scribe_consent") in ("1", "yes", "true")
        for p in parts
    )
    guest_ok = guest_attr or await token_consent()
    host_on = True
    for p in parts:
        if is_host(_ident(p)):
            flag = _attr(p, "scribe_on")
            if flag in ("0", "no", "false"):
                host_on = False
    return host, guest_ok, host_on


async def mint_scribe_token() -> str:
    from livekit import api

    tok = api.AccessToken(LK_KEY, LK_SECRET)
    tok.with_identity(f"scribe-{ROOM_NAME}").with_name("Scribe").with_ttl(datetime.timedelta(hours=6))  # livekit-api>=1 needs timedelta
    grants = dict(room_join=True, room=ROOM_NAME, can_publish=False, can_subscribe=True)
    try:
        tok.with_grants(api.VideoGrants(**grants, hidden=True))
    except TypeError:
        tok.with_grants(api.VideoGrants(**grants))
    return tok.to_jwt()


class Session:
    def __init__(self) -> None:
        self.pcm = bytearray()  # kept for compatibility; per-track buffers below
        self.tracks: dict[str, bytearray] = {}
        self.parts: list[str] = []
        self.started = time.time()
        self.last_flush = time.time()
        self.lock = asyncio.Lock()
        self.shm = SHM / f"scribe-{os.getpid()}.wav"
        self.target: dict[str, str] = {}

    def wipe_file(self) -> None:
        try:
            if self.shm.exists():
                self.shm.unlink()
        except OSError:
            pass

    async def add_pcm(self, chunk: bytes, key: str = "") -> None:
        # One buffer per track: appending two tracks into one buffer interleaved them.
        async with self.lock:
            self.tracks.setdefault(key, bytearray()).extend(chunk)

    async def flush_chunk(self, force: bool = False) -> None:
        async with self.lock:
            if not any(self.tracks.values()):
                return
            if not force and (time.time() - self.last_flush) < CHUNK_SEC:
                return
            bufs = [bytes(b) for b in self.tracks.values()]
            for b in self.tracks.values():
                b.clear()
            self.last_flush = time.time()
        pcm = await asyncio.to_thread(mix_pcm, bufs)
        if not stt_configured():
            log_event({"event": "stt_skip", "bytes": len(pcm)})
            return
        wav = pcm_to_wav(pcm)
        try:
            self.shm.write_bytes(wav)
            text = ""
            extra_note = ""
            out: dict[str, Any] = {}
            try:
                out = await asyncio.to_thread(transcribe, self.shm.read_bytes())  # keep audio flowing
                text = (out.get("transcript") or out.get("text") or "").strip()
                extra_note = (out.get("note") or "").strip()
            finally:
                self.wipe_file()
            if text:
                self.parts.append(text)
            if extra_note:
                self.parts.append("\n[backend note]\n" + extra_note)
            log_event(
                {
                    "event": "stt_ok",
                    "backend": backend_kind(),
                    "engine": str(out.get("engine") or "")[:40],
                    "aws_error": str(out.get("aws_error") or "")[:120],
                    "bytes": len(pcm),
                    "chars": len(text),
                }
            )
        except Exception as e:
            self.wipe_file()
            log_event({"event": "stt_fail", "bytes": len(pcm), "err": str(e)[:160]})

    async def finish(self) -> None:
        await self.flush_chunk(force=True)
        minutes = max(1, int((time.time() - self.started) / 60))
        transcript = "\n".join(self.parts).strip()
        stt_ok = bool(transcript)
        note = ""
        if transcript and note_configured():
            try:
                out = write_note(transcript, minutes)
                note = (out.get("note") or "").strip()
                log_event(
                    {
                        "event": "note_ok",
                        "backend": backend_kind(),
                        "note_engine": str(out.get("note_engine") or "")[:40],
                        "chars": len(note),
                    }
                )
            except Exception as e:
                log_event({"event": "note_fail", "err": str(e)[:160]})
        if not note and transcript:
            note = transcript[:4000]
        subject = f"Visit note · {time.strftime('%b %-d')} · {minutes} min"
        if note or transcript:
            ingested = desk_ingest(note or transcript, transcript, minutes, self.target)
            log_event(
                {
                    "event": "desk_ingest",
                    "ok": bool(ingested.get("ok")),
                    "id": (ingested.get("id") or "")[:32],
                    "targeted": bool(self.target.get("client_id")),
                }
            )
        if note:
            mailed = mail(subject, note_html(note, minutes, stt_ok))
            log_event({"event": "mailed", "ok": bool(mailed.get("ok")), "minutes": minutes})
            STATE.last = {"ok": bool(mailed.get("ok")), "detail": "mailed", "minutes": minutes}
        elif not stt_configured():
            mailed = mail(
                "Visit scribe · backend unset",
                "<p>A consented visit ended but no STT URL / OpenRouter key is configured. "
                "Audio was wiped. Nothing to attach.</p>",
            )
            log_event({"event": "mailed_unset", "ok": bool(mailed.get("ok"))})
            STATE.last = {"ok": False, "detail": "backend unset"}
        else:
            mailed = mail(
                "Visit scribe · no transcript",
                "<p>A consented visit ended but the transcription backend returned nothing. Audio was wiped.</p>",
            )
            log_event({"event": "mailed_empty", "ok": bool(mailed.get("ok"))})
            STATE.last = {"ok": False, "detail": "empty transcript"}
        self.parts.clear()
        self.pcm.clear()
        self.tracks.clear()
        self.target = {}
        self.wipe_file()


async def consume_audio(track: Any, session: Session) -> None:
    from livekit import rtc

    key = str(getattr(track, "sid", "") or id(track))

    stream = None
    try:
        try:
            # keyword-only in livekit>=1; positional raised TypeError → 48 kHz fallback
            stream = rtc.AudioStream.from_track(track=track, sample_rate=SAMPLE_RATE, num_channels=1)
        except TypeError:
            stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
        async for ev in stream:
            frame = getattr(ev, "frame", ev)
            data = getattr(frame, "data", b"")
            if data:
                await session.add_pcm(bytes(data), key)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log_event({"event": "audio_err", "err": str(e)[:160]})
    finally:
        if stream is not None:
            try:
                await stream.aclose()
            except Exception:
                pass


async def run_session() -> None:
    from livekit import rtc

    token = await mint_scribe_token()
    room = rtc.Room()
    session = Session()
    tasks: list[asyncio.Task[Any]] = []
    STATE.joined = True
    log_event({"event": "join", "room": ROOM_NAME, "backend": backend_kind()})

    def on_track(track: Any, pub: Any, participant: Any) -> None:
        if getattr(track, "kind", None) != rtc.TrackKind.KIND_AUDIO:
            return
        if is_scribe(_ident(participant)):
            return
        tasks.append(asyncio.create_task(consume_audio(track, session)))

    room.on("track_subscribed", on_track)

    try:
        await room.connect(LK_URL, token)
        for p in room.remote_participants.values():
            for pub in p.track_publications.values():
                if pub.track and pub.kind == rtc.TrackKind.KIND_AUDIO:
                    on_track(pub.track, pub, p)
        while True:
            await asyncio.sleep(5)
            await session.flush_chunk(False)
            humans = [
                p
                for p in room.remote_participants.values()
                if not is_scribe(_ident(p))
            ]
            for p in humans:
                if is_host(_ident(p)) and _attr(p, "desk_client_id"):
                    session.target = {
                        "client_id": _attr(p, "desk_client_id")[:80],
                        "email": _attr(p, "desk_client_email")[:200],
                        "name": _attr(p, "desk_client_name")[:200],
                    }
            if not humans:
                # wait briefly — host might be alone between guests
                await asyncio.sleep(8)
                humans = [
                    p
                    for p in room.remote_participants.values()
                    if not is_scribe(_ident(p))
                ]
                if not humans:
                    break
    finally:
        for t in tasks:
            t.cancel()
        try:
            await room.disconnect()
        except Exception:
            pass
        try:
            await session.finish()
        except Exception:
            log_event({"event": "finish_err", "err": traceback.format_exc()[-200:]})
        STATE.joined = False
        log_event({"event": "leave", "room": ROOM_NAME})


async def main() -> None:
    start_health()
    if not LK_KEY or not LK_SECRET:
        log_event({"event": "fatal", "err": "LIVEKIT keys unset"})
        while True:
            await asyncio.sleep(60)
    log_event(
        {
            "event": "boot",
            "stt": stt_configured(),
            "note": note_configured(),
            "room": ROOM_NAME,
            "backend": backend_kind(),
        }
    )
    while True:
        try:
            if STATE.joined:
                await asyncio.sleep(POLL_SEC)
                continue
            host, guest_ok, host_on = await room_snapshot()
            if host and guest_ok and host_on:
                await run_session()
        except Exception:
            log_event({"event": "loop_err", "err": traceback.format_exc()[-240:]})
        await asyncio.sleep(POLL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
