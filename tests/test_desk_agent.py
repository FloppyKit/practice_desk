"""Pa desk agent: Bedrock converse only (never api.x.ai), tool round-trip. Fake network."""
from __future__ import annotations

import json


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def test_desk_agent_bedrock_tool_round_trip_no_xai() -> None:
    from app import desk_agent as d

    seen: list[dict] = []
    replies = [
        {"output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "t1", "name": "list_note_templates", "input": {}}}]}}},
        {"output": {"message": {"role": "assistant", "content": [{"text": "<thinking>I listed them.</thinking> You have the SOAP and AVS templates."}]}}},
    ]

    def urlopen(req, timeout=0):
        assert "x.ai" not in req.full_url and "bedrock-runtime." in req.full_url and req.full_url.endswith("/converse")
        seen.append(json.loads(req.data.decode()))
        return _Resp(replies[len(seen) - 1])

    saved = (d.urllib.request.urlopen, d.BEDROCK_TOKEN)
    d.urllib.request.urlopen, d.BEDROCK_TOKEN = urlopen, "unit-token"
    try:
        out = d.run_desk_agent([{"role": "assistant", "content": "Hi"}, {"role": "user", "content": "what templates?"}],
                               book_fn=None, send_fn=None, mail_fn=None)
    finally:
        d.urllib.request.urlopen, d.BEDROCK_TOKEN = saved
    assert out["ok"] and out["reply"].startswith("You have") and out["tools"] == ["list_note_templates"]
    assert out["model"].startswith("bedrock-")
    assert seen[0]["messages"][0]["role"] == "user" and seen[0]["toolConfig"]["tools"]
    tr = seen[1]["messages"][-1]["content"][0]["toolResult"]
    assert tr["toolUseId"] == "t1" and "templates" in tr["content"][0]["text"]


def test_desk_agent_has_no_xai_path() -> None:
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("app", "desk_agent.py").read_text()
    assert "x.ai" not in src and "XAI" not in src
