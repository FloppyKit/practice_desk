"""Chat thread store. No network."""
from __future__ import annotations

import os
from pathlib import Path

from app import chat_threads


def test_post_and_get_roundtrip() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        os.environ["BOOKER_LOG_DIR"] = raw
        os.environ["NOTIFY_WEBHOOK_TOKEN"] = ""
        os.environ["SPRUCE_INBOX_API"] = "0"
        first = chat_threads.post_message(body="Hello from the site", hear_back="chat")
        token = first["token"]
        assert first["messages"][0]["role"] == "patient"
        again = chat_threads.post_message(body="Second", token=token)
        assert len(again["messages"]) == 2
        got = chat_threads.get_thread(token)
        assert got and got["token"] == token
        assert "phone" not in got
        assert (Path(raw) / "chat-threads").is_dir()


class _Resp:
    def __init__(self, raw: str, status: int = 200):
        self._raw = raw
        self.status = status

    def read(self) -> bytes:
        return self._raw.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_spruce_note_then_staff_reply_no_sms_without_consent() -> None:
    import json
    import tempfile

    calls: list[tuple[str, str, dict | None]] = []

    def opener(req, timeout=0):
        body = json.loads(req.data.decode()) if req.data else None
        calls.append((req.get_method(), req.full_url, body))
        if req.full_url.endswith("/organization/members"):
            return _Resp(json.dumps({"members": [{"id": "entity_team", "type": "teammate"}]}))
        if req.full_url.endswith("/conversations"):
            return _Resp(
                json.dumps(
                    {
                        "conversation": {"id": "t_note", "appURL": "https://app.sprucehealth.com/thread/t_note"},
                        "postMessageRequestId": "req_open",
                    }
                ),
                status=201,
            )
        if req.full_url.endswith("/messages"):
            return _Resp(json.dumps({"requestId": "req_more"}))
        if "/items" in req.full_url:
            return _Resp(
                json.dumps(
                    {
                        "conversationItems": [
                            {
                                "id": "ti_ours",
                                "requestId": "req_open",
                                "text": "Website chat\nignore",
                                "isInternalNote": True,
                            },
                            {
                                "id": "ti_staff",
                                "requestId": "req_staff",
                                "text": "We can see you Thursday.",
                                "isInternalNote": True,
                                "createdAt": "2026-10-08T15:00:00Z",
                            },
                        ]
                    }
                )
            )
        raise AssertionError(req.full_url)

    sms: list[str] = []

    def fake_sms(*args, **kwargs):
        sms.append(kwargs.get("kind") or "sms")
        return {"ok": True}

    from app import notify

    old = {
        "BOOKER_LOG_DIR": os.environ.get("BOOKER_LOG_DIR"),
        "SPRUCE_INBOX_API": os.environ.get("SPRUCE_INBOX_API"),
        "SPRUCE_API_TOKEN": os.environ.get("SPRUCE_API_TOKEN"),
        "SPRUCE_ENV_FILE": os.environ.get("SPRUCE_ENV_FILE"),
        "NOTIFY_WEBHOOK_TOKEN": os.environ.get("NOTIFY_WEBHOOK_TOKEN"),
    }
    old_sms = notify.send_sms
    notify.send_sms = fake_sms
    try:
        with tempfile.TemporaryDirectory() as raw:
            os.environ["BOOKER_LOG_DIR"] = raw
            os.environ["SPRUCE_INBOX_API"] = "1"
            os.environ["SPRUCE_API_TOKEN"] = "test-token"
            os.environ["SPRUCE_ENV_FILE"] = "/no/such/spruce.env"
            os.environ["NOTIFY_WEBHOOK_TOKEN"] = ""
            first = chat_threads.post_message(
                body="Can I be seen this week?",
                hear_back="chat",
                phone="+15555550100",
                sms_consent=False,
                opener=opener,
            )
            token = first["token"]
            assert first["messages"][0]["role"] == "patient"
            assert "phone" not in first
            create = [c for c in calls if c[0] == "POST" and c[1].endswith("/conversations")]
            assert create, calls
            note = create[0][2]["note"]
            assert create[0][2]["type"] == "note"
            assert note["message"]["internal"] is True
            assert note["memberIds"] == ["entity_team"]
            assert "Can I be seen this week?" in note["message"]["body"][0]["value"]
            assert "+15555550100" in note["message"]["body"][0]["value"]
            chat_threads.post_message(body="Still hoping", token=token, opener=opener)
            posted = [c for c in calls if c[1].endswith("/messages")]
            assert posted and posted[0][2]["internal"] is True
            got = chat_threads.get_thread(token, opener=opener)
            assert got is not None
            roles = [m["role"] for m in got["messages"]]
            assert roles == ["patient", "patient", "staff"]
            assert got["messages"][-1]["body"] == "We can see you Thursday."
            assert sms == []
    finally:
        notify.send_sms = old_sms
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_sms_reply_only_with_consent() -> None:
    import json
    import tempfile

    from app import notify

    sms: list[tuple[str, str]] = []

    def fake_sms(phone, body, **kwargs):
        sms.append((phone, body))
        return {"ok": True, "channel": "spruce"}

    old_sms = notify.send_sms
    notify.send_sms = fake_sms
    try:
        with tempfile.TemporaryDirectory() as raw:
            os.environ["BOOKER_LOG_DIR"] = raw
            os.environ["SPRUCE_INBOX_API"] = "0"
            row = {
                "token": "threadtoken1",
                "hear_back": "sms",
                "sms_consent": True,
                "phone": "+15555550123",
                "messages": [],
                "seen_reply_ids": [],
                "spruce_posted": [],
                "spruce_request_ids": [],
            }
            (Path(raw) / "chat-threads").mkdir()
            (Path(raw) / "chat-threads" / "threadtoken1.json").write_text(json.dumps(row))
            (Path(raw) / "chat-threads" / "_spruce_index.json").write_text(
                json.dumps({"t_sms": "threadtoken1"})
            )
            result = chat_threads.ingest_spruce_item(
                {
                    "id": "ti_reply",
                    "conversationId": "t_sms",
                    "requestId": "req_human",
                    "text": "Your refill was sent.",
                    "isInternalNote": True,
                }
            )
            assert result == "reply"
            assert sms and sms[0][0] == "+15555550123"
            assert "Your refill was sent." in sms[0][1]
            assert "Dr. Matt Brown" in sms[0][1]
    finally:
        notify.send_sms = old_sms
