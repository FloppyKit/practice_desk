#!/usr/bin/env python3
"""Local webhook: voice + booker → proton-cli mail send. Bind localhost only."""
from __future__ import annotations

import html as htmlmod
import json
import os
import re
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

HOST = os.environ.get("NOTIFY_BIND", "127.0.0.1")
PORT = int(os.environ.get("NOTIFY_PORT", "8791"))
TOKEN_FILE = os.environ.get(
    "NOTIFY_TOKEN_FILE", "/home/deploy/edge/scripts/.notify_token"
)
PROTON = os.environ.get("PROTON_CLI", "/home/deploy/.local/bin/proton-cli")
FROM_ADDR = os.environ.get("PROTON_FROM", "info@drmattbrown.com")
TO_ADDR = os.environ.get("PROTON_TO", "matt@drmattbrown.com")
PATH = os.environ.get("HOME", "/home/deploy") + "/.local/bin:" + os.environ.get("PATH", "")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHM = os.environ.get("PROTON_SHM", "/dev/shm")


def _safe_path(raw: str) -> str:
    s = unquote((raw or "/").strip() or "/")
    if "\x00" in s:
        raise ValueError("bad path")
    parts = [p for p in s.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError("bad path")
    return "/" + "/".join(parts) if parts else "/"


def _proton_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = PATH
    env["HOME"] = os.environ.get("HOME") or "/home/deploy"
    env["PROTON_NO_INPUT"] = "1"
    return env


SHARE_URL_RE = re.compile(r"https://drive\.proton\.me/urls/[A-Za-z0-9]+#[A-Za-z0-9_-]+")
SHLINK_ENV = os.environ.get("SHLINK_ENV", "/home/deploy/edge/secrets/shlink.env")
SHLINK_BASE = os.environ.get("SHLINK_BASE", "http://127.0.0.1:8081").rstrip("/")
SHLINK_DOMAIN = os.environ.get("SHLINK_DOMAIN", "go.psycharts.org")


def _shlink_key() -> str:
    got = (os.environ.get("SHLINK_API_KEY") or "").strip()
    if got:
        return got
    try:
        with open(SHLINK_ENV, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("INITIAL_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'").strip('"')
                if line.startswith("SHLINK_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'").strip('"')
    except OSError:
        return ""
    return ""


def _extract_share_url(blob: str, data) -> str:
    if isinstance(data, str) and data.startswith("https://drive.proton.me/"):
        return data.strip()
    if isinstance(data, dict):
        for k in ("url", "URL", "link", "publicUrl", "public_url", "shareUrl", "share_url"):
            v = data.get(k)
            if isinstance(v, str) and "drive.proton.me" in v:
                return v.strip()
        for nest in ("link", "publicLink", "share", "data", "item"):
            inner = data.get(nest)
            if isinstance(inner, (dict, str)):
                u = _extract_share_url("", inner)
                if u:
                    return u
        links = data.get("links") or data.get("publicLinks") or []
        if isinstance(links, list):
            for it in links:
                u = _extract_share_url("", it)
                if u:
                    return u
    text = blob or ""
    m = SHARE_URL_RE.search(text)
    return m.group(0) if m else ""


def drive_share_url(path: str) -> str:
    env = _proton_env()
    for args in (
        [PROTON, "--no-input", "--output", "json", "drive", "share", "get", path],
        [PROTON, "--no-input", "--yes", "--output", "json", "drive", "share", "link", path],
    ):
        r = subprocess.run(args, capture_output=True, text=True, timeout=90, env=env)
        data = None
        try:
            data = json.loads(r.stdout or "")
        except json.JSONDecodeError:
            data = None
        url = _extract_share_url((r.stdout or "") + "\n" + (r.stderr or ""), data)
        if url:
            return url
        if r.returncode != 0 and "link" in args:
            raise RuntimeError((r.stderr or r.stdout or "share failed")[:200])
    raise RuntimeError("no share url")


def cloak_url(long_url: str, title: str) -> tuple[str, bool]:
    """Return (url, cloaked). Proton fragment must survive. Never log the long URL."""
    key = _shlink_key()
    if not key or not long_url.startswith("https://"):
        return long_url, False
    sid = ""
    m = re.search(r"/urls/([A-Za-z0-9]+)", long_url)
    if m:
        sid = m.group(1).lower()
    payload: dict = {
        "longUrl": long_url,
        "domain": SHLINK_DOMAIN,
        "title": (title or "Shared file")[:80],
        "findIfExists": True,
        "crawlable": False,
        "forwardQuery": True,
    }
    if sid:
        payload["customSlug"] = "f-" + sid
    body = json.dumps(payload).encode()
    req = Request(
        SHLINK_BASE + "/rest/v3/short-urls",
        data=body,
        method="POST",
        headers={
            "X-Api-Key": key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        try:
            with urlopen(req, timeout=20) as resp:
                out = json.loads(resp.read().decode() or "{}")
        except HTTPError as e:
            if e.code not in (400, 409) or not sid:
                return long_url, False
            patch = Request(
                f"{SHLINK_BASE}/rest/v3/short-urls/{payload['customSlug']}?domain={SHLINK_DOMAIN}",
                data=json.dumps({"longUrl": long_url, "title": payload["title"]}).encode(),
                method="PATCH",
                headers={
                    "X-Api-Key": key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            with urlopen(patch, timeout=20) as resp:
                out = json.loads(resp.read().decode() or "{}")
        short = (out.get("shortUrl") or "").strip()
        if short.startswith("https://"):
            return short, True
    except Exception:
        return long_url, False
    return long_url, False


def drive_list(path: str) -> dict:
    r = subprocess.run(
        [PROTON, "--no-input", "--output", "json", "drive", "items", "list", path],
        capture_output=True,
        text=True,
        timeout=45,
        env=_proton_env(),
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "list failed")[:300])
    data = json.loads(r.stdout or "{}")
    items = []
    for it in data.get("items") or []:
        items.append(
            {
                "name": it.get("name") or "",
                "type": it.get("type") or "file",
                "size": int(it.get("size") or 0),
            }
        )
    items.sort(key=lambda x: (0 if x["type"] == "folder" else 1, x["name"].lower()))
    return {"ok": True, "path": path, "count": len(items), "items": items}


DESK_FOLDER = os.environ.get("PROTON_DESK_FOLDER", "Desk")


def _proton_json(args: list[str], timeout: int = 60) -> dict:
    r = subprocess.run(
        [PROTON, "--no-input", "--output", "json", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_proton_env(),
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "proton-cli failed")[:280])
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError("proton-cli bad json") from e
    return data if isinstance(data, dict) else {}


def _plain_body(raw: str) -> str:
    t = raw or ""
    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", t)
    t = re.sub(r"(?is)<br\s*/?>", "\n", t)
    t = re.sub(r"(?is)</p>", "\n", t)
    t = re.sub(r"(?is)<[^>]+>", " ", t)
    t = htmlmod.unescape(t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:20000]


def _addr(obj) -> tuple[str, str]:
    if isinstance(obj, dict):
        return (str(obj.get("Address") or obj.get("address") or "").strip(),
                str(obj.get("Name") or obj.get("name") or "").strip())
    if isinstance(obj, str):
        return obj.strip(), ""
    return "", ""


def _is_mine(addr: str) -> bool:
    a = (addr or "").lower()
    return a.endswith("@drmattbrown.com") or a.endswith("@psycharts.org")


def _channel(subject: str, from_addr: str = "") -> str:
    sub = (subject or "").lower()
    frm = (from_addr or "").lower()
    if sub.startswith("[fax") or "inbound fax" in sub or "sinch" in frm:
        return "fax"
    if sub.startswith("[voice") or "voicemail" in sub or "voice agent" in sub:
        return "voicemail"
    if sub.startswith("[site chat") or sub.startswith("[chat]") or "site chat" in sub:
        return "chat"
    return "email"


def _att_meta(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    name = str(raw.get("name") or raw.get("Name") or "file").strip() or "file"
    if "/" in name or "\\" in name or ".." in name:
        name = name.replace("\\", "/").rsplit("/", 1)[-1] or "file"
    return {
        "id": str(raw.get("id") or ""),
        "name": name[:180],
        "size": int(raw.get("size") or 0),
        "mime": str(raw.get("mime_type") or raw.get("mime") or ""),
    }


def mail_ensure_desk() -> dict:
    data = _proton_json(["mail", "settings", "folders", "list"])
    for f in data.get("folders") or []:
        if str(f.get("name") or "").strip().lower() == DESK_FOLDER.lower():
            return {"ok": True, "folder": f.get("name") or DESK_FOLDER, "id": f.get("id") or ""}
    created = _proton_json(
        ["--yes", "mail", "settings", "folders", "create", "--name", DESK_FOLDER]
    )
    ids = created.get("ids") or []
    return {"ok": True, "folder": DESK_FOLDER, "id": ids[0] if ids else "", "created": True}


def mail_list(folder: str = "", unread: bool = False) -> dict:
    want = (folder or DESK_FOLDER).strip() or DESK_FOLDER
    setup = mail_ensure_desk()
    folder_id = setup.get("id") or want
    label = setup.get("folder") or DESK_FOLDER
    if want.lower() not in (DESK_FOLDER.lower(), "desk", (label or "").lower()) and len(want) > 8:
        folder_id = want
        label = want
    args = ["mail", "conversations", "list", "--folder", folder_id, "--page-size", "30"]
    if unread:
        args.append("--unread")
    data = _proton_json(args, timeout=75)
    rows = []
    for c in data.get("conversations") or []:
        senders = c.get("senders") or []
        addr, name = _addr(senders[0] if senders else {})
        subj = c.get("subject") or "(no subject)"
        rows.append(
            {
                "id": c.get("id") or "",
                "subject": subj,
                "from": addr,
                "from_name": name,
                "time": int(c.get("time") or 0),
                "unread": int(c.get("num_unread") or 0) > 0,
                "messages": int(c.get("num_messages") or 1),
                "attachments": int(c.get("num_attachments") or 0),
                "channel": _channel(subj, addr),
            }
        )
    return {
        "ok": True,
        "folder": label,
        "count": len(rows),
        "total": int(data.get("total") or data.get("count") or len(rows)),
        "conversations": rows,
    }


def mail_thread(ref: str) -> dict:
    ref = (ref or "").strip()
    if not ref or len(ref) > 200:
        raise ValueError("bad ref")
    data = _proton_json(["mail", "conversations", "get", ref], timeout=75)
    conv = data.get("conversation") or {}
    msgs = []
    for m in data.get("messages") or []:
        addr, name = _addr(m.get("from") or {})
        tos = m.get("to") or []
        to_addr, _ = _addr(tos[0] if tos else {})
        atts = [_att_meta(a) for a in (m.get("attachments") or [])]
        atts = [a for a in atts if a.get("id")]
        msgs.append(
            {
                "id": m.get("id") or "",
                "from": addr,
                "from_name": name,
                "to": to_addr,
                "mine": _is_mine(addr),
                "subject": m.get("subject") or "",
                "time": int(m.get("time") or 0),
                "text": _plain_body(str(m.get("body") or "")),
                "attachments": atts,
            }
        )
    try:
        subprocess.run(
            [PROTON, "--no-input", "--yes", "mail", "conversations", "mark", "read", ref],
            capture_output=True,
            text=True,
            timeout=30,
            env=_proton_env(),
        )
    except Exception:
        pass
    senders = conv.get("senders") or []
    addr, name = _addr(senders[0] if senders else (msgs[0] if msgs else {}))
    if msgs and not addr:
        addr, name = msgs[0].get("from") or "", msgs[0].get("from_name") or ""
    subject = conv.get("subject") or (msgs[0]["subject"] if msgs else "")
    return {
        "ok": True,
        "id": conv.get("id") or ref,
        "subject": subject,
        "from": addr,
        "from_name": name,
        "channel": _channel(subject, addr),
        "messages": msgs,
    }


def mail_reply(ref: str, body: str, files: list | None = None) -> dict:
    import base64

    ref = (ref or "").strip()
    text = (body or "").strip()
    if not ref or len(ref) > 200:
        raise ValueError("bad ref")
    if (not text and not files) or len(text) > 8000:
        raise ValueError("bad body")
    tmp: list[str] = []
    try:
        cmd = [
            PROTON,
            "--no-input",
            "--yes",
            "mail",
            "conversations",
            "reply",
            ref,
            "--body",
            "-",
            "--no-quote",
        ]
        for f in (files or [])[:4]:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "file").replace("/", "_").replace("\\", "_")[:80]
            raw = str(f.get("b64") or "")
            if not raw:
                continue
            try:
                blob = base64.b64decode(raw)
            except Exception:
                continue
            if not blob or len(blob) > 8 * 1024 * 1024:
                continue
            suf = ""
            if "." in name:
                suf = "." + name.rsplit(".", 1)[-1][:8]
            fd, dest = tempfile.mkstemp(prefix="pmail-up-", dir=SHM, suffix=suf)
            os.write(fd, blob)
            os.close(fd)
            tmp.append(dest)
            cmd.extend(["--attach", dest])
        r = subprocess.run(
            cmd,
            input=text or " ",
            capture_output=True,
            text=True,
            timeout=120,
            env=_proton_env(),
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "reply failed")[:280])
        return {"ok": True, "id": ref, "attached": len(tmp)}
    finally:
        for dest in tmp:
            try:
                os.unlink(dest)
            except OSError:
                pass


def mail_download_attachment(message_id: str, att_id: str) -> tuple[bytes, str, str]:
    message_id = (message_id or "").strip()
    att_id = (att_id or "").strip()
    if not message_id or not att_id or len(message_id) > 200 or len(att_id) > 200:
        raise ValueError("bad attachment")
    fd, dest = tempfile.mkstemp(prefix="pmail-dl-", dir=SHM)
    os.close(fd)
    try:
        r = subprocess.run(
            [
                PROTON,
                "--no-input",
                "--yes",
                "mail",
                "messages",
                "attachments",
                "download",
                message_id,
                att_id,
                "--output",
                dest,
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=90,
            env=_proton_env(),
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "download failed")[:200])
        with open(dest, "rb") as fh:
            data = fh.read()
        return data, "application/octet-stream", "file"
    finally:
        try:
            os.unlink(dest)
        except OSError:
            pass


def drive_upload_bytes(fpath: str, data: bytes, suffix: str = ".bin") -> None:
    # proton-cli 2.x: a file SRC treats DEST as the target *folder* (so a full file path fails);
    # SRC "-" (stdin) lets DEST name the file. Bytes never touch disk.
    r = subprocess.run(
        [PROTON, "--no-input", "drive", "items", "upload", "-", fpath],
        input=data,
        capture_output=True,
        timeout=120,
        env=_proton_env(),
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or b"upload").decode("utf-8", "replace")[:240])


def load_token() -> str:
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return f.read().strip()


def format_voice_body(payload: dict) -> tuple[str, str]:
    method = payload.get("preferred_contact_method") or payload.get("method") or "unknown"
    name = payload.get("caller_name") or "Unknown"
    contact = payload.get("contact_value") or payload.get("callback_number") or ""
    urgency = payload.get("urgency") or "normal"
    summary = payload.get("summary") or ""
    caller_from = payload.get("callerFrom") or payload.get("caller_from") or ""
    call_id = payload.get("callId") or payload.get("call_id") or ""
    at = payload.get("at") or ""

    subject = f"[Voice {urgency}] Follow-up ({method}): {name}"
    lines = [
        "Psych Arts voice agent — follow-up request",
        "",
        f"When: {at}",
        f"Urgency: {urgency}",
        f"Name: {name}",
        f"Preferred method: {method}",
        f"Contact: {contact or '(none)'}",
        f"Inbound From: {caller_from or '(unknown)'}",
        f"Call id: {call_id}",
        "",
        "Summary:",
        summary,
        "",
        "— sent via proton-cli on psycharts-edge",
    ]
    return subject, "\n".join(lines)


DEFAULT_LOGO = os.environ.get(
    "NOTIFY_LOGO", "/home/deploy/edge/booker/static/logo.png"
)
PRACTICE_DIR = os.environ.get(
    "PRACTICE_DIR", "/home/deploy/edge/booker-logs/practice"
)
LOGO_CID = "logo@psycharts.org"


def _b64_wrap(data: bytes) -> str:
    import base64

    raw = base64.b64encode(data).decode("ascii")
    return "\n".join(raw[i : i + 76] for i in range(0, len(raw), 76))


def build_html_eml(*, to: str, subject: str, html: str, logo_path: str | None) -> str:
    """multipart/related so the blue logo is actually inline (CID we control)."""
    import email.utils
    from email.header import Header

    boundary = "psycharts" + os.urandom(8).hex()
    date = email.utils.formatdate(localtime=True)
    subj = Header(subject, "utf-8").encode()
    body_html = html
    parts = [
        f"From: {FROM_ADDR}",
        f"To: {to}",
        f"Subject: {subj}",
        f"Date: {date}",
        "MIME-Version: 1.0",
        "X-Mailer: psycharts-booker",
    ]
    logo_bytes = None
    chosen = None
    for name in ("logo.jpg", "logo.jpeg", "logo.png", "logo.webp"):
        cand = os.path.join(PRACTICE_DIR, name)
        if os.path.isfile(cand):
            chosen = cand
            break
    if not chosen:
        chosen = logo_path or DEFAULT_LOGO
    if chosen and os.path.isfile(chosen):
        try:
            with open(chosen, "rb") as f:
                logo_bytes = f.read()
        except OSError:
            logo_bytes = None
    if logo_bytes:
        body_html = body_html.replace("cid:logo.png", f"cid:{LOGO_CID}")
        parts.append(f'Content-Type: multipart/related; boundary="{boundary}"')
        parts.append("")
        parts.append(f"--{boundary}")
        parts.append("Content-Type: text/html; charset=utf-8")
        parts.append("Content-Transfer-Encoding: 8bit")
        parts.append("")
        parts.append(body_html)
        parts.append(f"--{boundary}")
        ext = os.path.splitext(chosen)[1].lower()
        mime = "image/jpeg" if ext in (".jpg", ".jpeg") else (
            "image/webp" if ext == ".webp" else "image/png"
        )
        fname = "logo" + (ext or ".png")
        parts.append(f"Content-Type: {mime}")
        parts.append("Content-Transfer-Encoding: base64")
        parts.append(f"Content-ID: <{LOGO_CID}>")
        parts.append(f'Content-Disposition: inline; filename="{fname}"')
        parts.append("")
        parts.append(_b64_wrap(logo_bytes))
        parts.append(f"--{boundary}--")
        parts.append("")
    else:
        # Hosted fallback if the file is missing
        body_html = body_html.replace(
            "cid:logo.png", "https://book.psycharts.org/api/practice/logo"
        )
        parts.append("Content-Type: text/html; charset=utf-8")
        parts.append("Content-Transfer-Encoding: 8bit")
        parts.append("")
        parts.append(body_html)
    return "\n".join(parts)


def send_proton(
    to: str,
    subject: str,
    body: str,
    *,
    html: bool = False,
    inline_logo: bool = False,
) -> tuple[bool, str]:
    env = os.environ.copy()
    env["PATH"] = PATH
    env["HOME"] = os.environ.get("HOME", "/home/deploy")
    cmd = [
        PROTON, "--no-input", "--yes",
        "mail", "messages", "send",
        "--from", FROM_ADDR,
        "--to", to,
        "--no-signature",
    ]
    eml_path = None
    try:
        if html:
            import tempfile

            eml = build_html_eml(
                to=to,
                subject=subject,
                html=body,
                logo_path=DEFAULT_LOGO if inline_logo else None,
            )
            tmp = tempfile.NamedTemporaryFile(
                "w", suffix=".eml", prefix="notify-", delete=False, encoding="utf-8"
            )
            with tmp as f:
                f.write(eml)
                eml_path = f.name
            cmd.extend(["--eml", eml_path])
        else:
            cmd.extend(["--subject", subject, "--body", "-"])
        r = subprocess.run(
            cmd,
            input=None if html else body,
            capture_output=True,
            text=True,
            timeout=90,
            env=env,
        )
    except FileNotFoundError:
        return False, f"proton-cli not found at {PROTON}"
    except subprocess.TimeoutExpired:
        return False, "proton-cli timed out"
    finally:
        if eml_path:
            try:
                os.unlink(eml_path)
            except OSError:
                pass
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, f"exit {r.returncode}: {out.strip()[:500]}"
    return True, out.strip()[:300] or "sent"



def mail_chat_replies(token: str) -> dict:
    """Staff replies in the Spruce email thread tagged [chat TOKEN]."""
    token = (token or "").strip()
    if not token or len(token) > 48 or not token.replace("-", "").replace("_", "").isalnum():
        raise ValueError("bad token")
    env = _proton_env()
    r = subprocess.run(
        [
            PROTON, "--no-input", "--output", "json",
            "mail", "conversations", "search",
            "--subject", f"[chat {token}]",
            "--limit", "5",
        ],
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
    )
    if r.returncode != 0:
        return {"ok": False, "replies": [], "detail": "search failed"}
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "replies": [], "detail": "bad search"}
    replies = []
    for conv in (data.get("conversations") or [])[:5]:
        cid = str(conv.get("id") or "")
        if not cid:
            continue
        try:
            thread = mail_thread(cid)
        except Exception:
            continue
        for msg in thread.get("messages") or []:
            if msg.get("mine"):
                continue
            text_body = str(msg.get("text") or "").strip()
            mid = str(msg.get("id") or "")
            if mid and text_body:
                replies.append({"id": mid, "text": text_body[:2000]})
    return {"ok": True, "replies": replies}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        try:
            expected = load_token()
        except Exception:
            return False
        got = self.headers.get("X-Notify-Token") or self.headers.get(
            "Authorization", ""
        ).removeprefix("Bearer ").strip()
        return bool(got and got == expected)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)

        if path == "/drive/list":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                folder = _safe_path((qs.get("path") or ["/"])[0])
                self._json(200, drive_list(folder))
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            return

        if path == "/drive/file":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                fpath = _safe_path((qs.get("path") or [""])[0])
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            if fpath == "/":
                self._json(400, {"ok": False, "error": "pick a file"})
                return
            dest = None
            try:
                fd, dest = tempfile.mkstemp(prefix="pdrive-", dir=SHM)
                os.close(fd)
                r = subprocess.run(
                    [
                        PROTON,
                        "--no-input",
                        "drive",
                        "items",
                        "download",
                        fpath,
                        "--output",
                        dest,
                        "--force",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=_proton_env(),
                )
                if r.returncode != 0:
                    self._json(
                        502,
                        {"ok": False, "error": (r.stderr or r.stdout or "dl")[:200]},
                    )
                    return
                with open(dest, "rb") as fh:
                    data = fh.read()
                name = fpath.rsplit("/", 1)[-1] or "file"
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{name}"'
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            finally:
                if dest:
                    try:
                        os.unlink(dest)
                    except OSError:
                        pass
            return

        if path == "/mail/setup":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                self._json(200, mail_ensure_desk())
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            return

        if path == "/mail/list":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                folder = (qs.get("folder") or [DESK_FOLDER])[0]
                unread = (qs.get("unread") or [""])[0] in ("1", "true", "yes")
                self._json(200, mail_list(folder, unread=unread))
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            return

        if path == "/mail/attachment":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                mid = (qs.get("message") or [""])[0]
                aid = (qs.get("id") or [""])[0]
                data, ctype, _ = mail_download_attachment(mid, aid)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
                return
            name = (qs.get("name") or ["file"])[0]
            name = name.replace("/", "_").replace("\\", "_")[:120] or "file"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/mail/thread":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                ref = (qs.get("id") or [""])[0]
                self._json(200, mail_thread(ref))
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            return

        if self.path in ("/health", "/"):
            env = os.environ.copy()
            env["PATH"] = PATH
            try:
                r = subprocess.run(
                    [PROTON, "--no-input", "account", "get", "--output", "json"],
                    capture_output=True, text=True, timeout=30, env=env,
                )
                unlocked = r.returncode == 0
                detail = (r.stdout or r.stderr or "")[:200]
            except Exception as e:
                unlocked = False
                detail = str(e)
            self._json(200, {
                "status": "ok" if unlocked else "need_login",
                "proton_session": unlocked,
                "detail": detail,
                "from": FROM_ADDR,
                "default_to": TO_ADDR,
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        p = self.path.rstrip("/") or "/"
        if p == "/mail/reply":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            try:
                self._json(
                    200,
                    mail_reply(
                        str(payload.get("id") or ""),
                        str(payload.get("body") or ""),
                        payload.get("files") if isinstance(payload.get("files"), list) else None,
                    ),
                )
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            return

        if p == "/drive/mkdir":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            try:
                folder = _safe_path(str(payload.get("path") or ""))
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            if folder == "/":
                self._json(400, {"ok": False, "error": "pick a folder"})
                return
            try:
                r = subprocess.run(
                    [PROTON, "--no-input", "--yes", "drive", "folders", "create", folder],
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=_proton_env(),
                )
                if r.returncode != 0:
                    err = (r.stderr or r.stdout or "mkdir")[:200]
                    if "exist" not in err.lower() and "already" not in err.lower():
                        self._json(502, {"ok": False, "error": err})
                        return
                self._json(200, {"ok": True, "path": folder})
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            return

        if p == "/drive/save":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            try:
                fpath = _safe_path(str(payload.get("path") or ""))
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            if fpath == "/":
                self._json(400, {"ok": False, "error": "pick a file"})
                return
            raw_b64 = payload.get("b64") or payload.get("base64")
            try:
                if raw_b64:
                    import base64

                    blob = base64.b64decode(str(raw_b64))
                    suf = "." + (fpath.rsplit(".", 1)[-1] if "." in fpath else "bin")
                else:
                    blob = str(payload.get("text") or "").encode("utf-8")
                    suf = ".txt"
                if len(blob) > 20 * 1024 * 1024:
                    self._json(400, {"ok": False, "error": "file too large (20 MB)"})
                    return
                drive_upload_bytes(fpath, blob, suf)
                self._json(200, {"ok": True, "path": fpath, "bytes": len(blob)})
            except Exception as e:
                self._json(502, {"ok": False, "error": str(e)[:200]})
            return

        if p == "/drive/share":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            try:
                fpath = _safe_path(str(payload.get("path") or ""))
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
                return
            if fpath == "/":
                self._json(400, {"ok": False, "error": "pick a file"})
                return
            title = str(payload.get("title") or fpath.rsplit("/", 1)[-1] or "file")
            try:
                proton_url = drive_share_url(fpath)
                public, cloaked = cloak_url(proton_url, title)
                self._json(
                    200,
                    {
                        "ok": True,
                        "url": public,
                        "cloaked": cloaked,
                        "kind": "cloak" if cloaked else "proton",
                    },
                )
            except Exception as e:
                self._json(502, {"ok": False, "error": "share failed"})
            return

        if p == "/mail/chat-replies":
            if not self._authed():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid json"})
                return
            try:
                self._json(200, mail_chat_replies(str(payload.get("token") or "")))
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            return

        if p != "/notify":
            self._json(404, {"error": "not found"})
            return
        try:
            expected = load_token()
        except Exception as e:
            self._json(500, {"ok": False, "error": f"token file: {e}"})
            return
        got = self.headers.get("X-Notify-Token") or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not got or got != expected:
            self._json(401, {"ok": False, "error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "payload must be object"})
            return

        # Named practice template: { "kind": "email", "template": "intake", "to", "fields": {...} }
        if payload.get("template"):
            to = (payload.get("to") or (payload.get("fields") or {}).get("email") or TO_ADDR).strip()
            if not EMAIL_RE.match(to):
                self._json(400, {"ok": False, "error": "invalid to address"})
                return
            try:
                booker_root = os.environ.get("BOOKER_ROOT", "/home/deploy/edge/booker")
                if booker_root not in sys.path:
                    sys.path.insert(0, booker_root)
                from app.email_templates import render_named  # type: ignore

                subject, html, _text, _sms = render_named(
                    str(payload.get("template")),
                    payload.get("fields") or {},
                )
                if payload.get("subject"):
                    subject = str(payload["subject"]).strip() or subject
            except Exception as e:
                self._json(400, {"ok": False, "error": f"template: {e}"})
                return
            ok, detail = send_proton(to, subject, html, html=True, inline_logo=True)
            self._json(200 if ok else 502, {"ok": ok, "detail": detail, "channel": "proton", "to": to, "html": True, "template": payload.get("template")})
            return

        # Generic email: { "kind": "email", "to", "subject", "body" }
        # Voice (default): existing follow-up fields
        kind = (payload.get("kind") or "voice").lower()
        if kind == "email" or (payload.get("subject") and payload.get("body") and "caller_name" not in payload):
            to = (payload.get("to") or TO_ADDR).strip()
            subject = (payload.get("subject") or "").strip()
            body = (payload.get("body") or "").strip()
            html = bool(payload.get("html"))
            inline_logo = bool(payload.get("inline_logo", html))
            if not subject or not body:
                self._json(400, {"ok": False, "error": "subject and body required"})
                return
            if not EMAIL_RE.match(to):
                self._json(400, {"ok": False, "error": "invalid to address"})
                return
            ok, detail = send_proton(
                to, subject, body, html=html, inline_logo=inline_logo
            )
            self._json(200 if ok else 502, {"ok": ok, "detail": detail, "channel": "proton", "to": to, "html": html})
            return

        subject, body = format_voice_body(payload)
        ok, detail = send_proton(TO_ADDR, subject, body)
        self._json(200 if ok else 502, {"ok": ok, "detail": detail, "channel": "proton", "to": TO_ADDR})


def main() -> None:
    if not os.path.isfile(TOKEN_FILE):
        print(f"missing token file: {TOKEN_FILE}", file=sys.stderr)
        sys.exit(1)
    httpd = HTTPServer((HOST, PORT), Handler)
    print(f"proton-notify listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
