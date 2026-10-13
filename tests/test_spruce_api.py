"""Spruce send builder. No network. No real token."""
from __future__ import annotations

import io
import json
import os

import base64
import hashlib
import hmac

from app.spruce_api import (
    create_contact,
    invite_contact,
    send_spruce_sms,
    verify_webhook_signature,
)


class _Resp:
    def __init__(self, raw: str):
        self._raw = raw

    def read(self) -> bytes:
        return self._raw.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_spruce_post_shape(monkeypatch=None) -> None:
    captured: dict = {}

    def opener(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        captured["key"] = req.get_header("S-idempotency-key")
        captured["body"] = json.loads(req.data.decode())
        captured["timeout"] = timeout
        return _Resp(json.dumps({"requestId": "req_test"}))

    os.environ["SMS_BRIDGE"] = "spruce_api"
    result = send_spruce_sms(
        "+15555550100",
        "hello",
        idempotency_key="event:30m",
        token="test-token",
        endpoint_id="endpoint id/with space",
        opener=opener,
    )
    assert result["sent"] is True
    assert result["request_id"] == "req_test"
    assert captured["auth"] == "Bearer test-token"
    assert captured["key"] == "event:30m"
    assert "endpoint%20id" in captured["url"]
    assert captured["body"]["destination"]["smsOrEmailEndpoint"] == "+15555550100"
    assert captured["body"]["message"]["body"][0]["value"] == "hello"


def test_duplicate_key_is_settled() -> None:
    from email.message import Message
    from urllib.error import HTTPError

    def opener(req, timeout=0):
        raise HTTPError(req.full_url, 422, "duplicate", Message(), io.BytesIO(b"{}"))

    result = send_spruce_sms(
        "+15555550100",
        "hello",
        token="test-token",
        endpoint_id="ep",
        opener=opener,
    )
    assert result["ok"] is True
    assert result["already"] is True
    assert result["settled"] is True


def test_create_and_invite_shape() -> None:
    seen: list[tuple[str, str, dict]] = []

    def opener(req, timeout=0):
        seen.append((req.get_method(), req.full_url, json.loads(req.data.decode())))
        if req.full_url.endswith("/contacts"):
            return _Resp(json.dumps({"id": "entity_test", "phoneNumbers": [{"id": "ph1", "value": "+15555550100"}]}))
        return _Resp(json.dumps({"id": "invite_ok"}))

    status, contact = create_contact(
        given_name="SMS",
        family_name="Smoke",
        phone_e164="+15555550100",
        token="test-token",
        opener=opener,
    )
    assert status == 200
    assert contact["id"] == "entity_test"
    status, _body = invite_contact(
        "entity_test",
        destination_id="ph1",
        internal_endpoint_id="organizationCode_test",
        custom_message="test invite",
        token="test-token",
        opener=opener,
    )
    assert status == 200
    assert seen[0][2]["phoneNumbers"][0]["value"] == "+15555550100"
    assert seen[1][2]["destinationId"] == "ph1"
    assert "/contacts/entity_test/invite" in seen[1][1]


def test_missing_credentials() -> None:
    os.environ.pop("SPRUCE_API_TOKEN", None)
    os.environ.pop("SPRUCE_PHONE_ENDPOINT_ID", None)
    os.environ["SPRUCE_ENV_FILE"] = "/no/such/spruce.env"
    result = send_spruce_sms("+15555550100", "hello", token="", endpoint_id="")
    assert result["ok"] is False
    assert "missing" in result["detail"]


def test_webhook_signature_matches_spruce_sample() -> None:
    secret = "endpoint-secret"
    body = b'{"type":"conversationItem.created"}'
    mac = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    signature = base64.b64encode(mac).decode()
    assert verify_webhook_signature(body, signature, secret) is True
    assert verify_webhook_signature(body, signature, "other") is False
    assert verify_webhook_signature(body, "not-base64", secret) is False
