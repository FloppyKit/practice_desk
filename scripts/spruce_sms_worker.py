#!/usr/bin/env python3
"""Claim one queued SMS and send it through Spruce.app on this Mac.

Watch mode (default): print how many texts are due. Does not claim.
Send mode: only if SPRUCE_SMS_SEND=1 and a UI recipe exists. Until that
recipe is recorded, this refuses to click Spruce.

The booker secret stays on the VPS. This process uses SSH and never
prints the message body.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SSH_HOST = os.environ.get("SMS_BRIDGE_SSH", "do-edge")
BOOKER = "http://127.0.0.1:8090"
ENV_FILE = Path(
    os.environ.get(
        "SMS_BRIDGE_ENV",
        str(Path.home() / ".config/practice-desk/sms-bridge.env"),
    )
)


def _load_env() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _last4(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-4:] if digits else "----"


def _remote(method: str, path: str, payload: dict | None = None) -> dict:
    data = ""
    if payload is not None:
        data = "--data " + json.dumps(json.dumps(payload))
    remote = """
set -euo pipefail
secret=$(awk -F= '$1=="SMS_BRIDGE_SECRET" {print substr($0, index($0, "=")+1)}' /home/deploy/edge/booker/.env | tr -d '"' | tr -d "'")
if [ -z "$secret" ]; then
  echo '{"ok":false,"detail":"secret missing"}'
  exit 0
fi
curl -sS -m 25 -X __METHOD__ \\
  -H "X-Bridge-Secret: $secret" \\
  -H "Content-Type: application/json" \\
  __DATA__ \\
  __URL__
""".replace("__METHOD__", method).replace("__DATA__", data).replace(
        "__URL__", BOOKER + path
    )
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            SSH_HOST,
            "bash",
            "-s",
        ],
        input=remote,
        text=True,
        capture_output=True,
        timeout=40,
    )
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0 and not raw:
        raise RuntimeError((proc.stderr or "ssh failed")[-300:])
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"bad json from booker: {raw[:180]}") from e


def _can_send() -> bool:
    if os.environ.get("SPRUCE_SMS_SEND", "").strip() != "1":
        return False
    recipe = os.environ.get("SPRUCE_UI_RECIPE", "").strip()
    return bool(recipe) and Path(recipe).is_file()


def spruce_send(to: str, body: str) -> str:
    """Click Spruce. Not armed until a supervised compose path is recorded."""
    del to, body
    raise NotImplementedError("Spruce clicker not recorded")


def main() -> int:
    _load_env()
    try:
        due = _remote("GET", "/api/sms-bridge/due")
    except Exception as e:
        print(f"bridge unreachable: {e}")
        return 0
    if not due.get("ok"):
        print(f"bridge not ready: {due.get('detail') or 'unauthorized'}")
        return 0
    print(f"due={due.get('due', 0)} pending={due.get('pending', 0)}")
    if not _can_send():
        print("watch only — Spruce clicker not armed")
        return 0
    claimed = _remote("POST", "/api/sms-bridge/claim", {})
    job = claimed.get("job")
    if not job:
        return 0
    job_id = str(job.get("id") or "")
    phone = str(job.get("to") or "")
    print(f"claimed {job_id} ···{ _last4(phone) }")
    try:
        status = spruce_send(phone, str(job.get("body") or ""))
    except Exception:
        _remote("POST", "/api/sms-bridge/ack", {"id": job_id, "status": "release"})
        print(f"released {job_id}")
        return 1
    if status not in ("sent", "failed", "uncertain"):
        status = "uncertain"
    _remote("POST", "/api/sms-bridge/ack", {"id": job_id, "status": status})
    print(f"ack {job_id} {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
