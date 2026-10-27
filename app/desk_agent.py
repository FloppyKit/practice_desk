"""Pa, the staff assistant on the practice desk — tools only, never the public chat.
Writer: AWS Bedrock (Nova Pro, BAA-covered), same converse + bearer-token setup as scribe-aws. No xAI."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .patients import client_by_id, create_client, search_clients

BEDROCK_TOKEN = (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
BEDROCK_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
BEDROCK_MODEL = os.environ.get("DESK_AGENT_MODEL") or os.environ.get("BEDROCK_MODEL_ID") or "amazon.nova-pro-v1:0"
BEDROCK_URL = (
    f"https://bedrock-runtime.{BEDROCK_REGION}.amazonaws.com/model/"
    f"{urllib.parse.quote(BEDROCK_MODEL, safe='')}/converse"
)
SYSTEM = """You are Pa, the practice assistant on Dr. Matthew Brown's private desk.
Simple lines — call, open, send, book, yes/no — are handled before you. You only see leftover English.
He is the doctor. Do what he asks: look up patients, create a client, send forms,
send a branded email, book a visit, work a note template, or edit About the practice.
About the practice is the letterhead: display name, legal name, clinician name,
address, phone, fax, email, EIN, NPI. Logo and photo are files he uploads on the desk.
Note templates are section boxes. Agents fill the sections. Default chart note is SOAP.
AVS is the Plan in patient language. Addendum after lock.
If they describe a new template, write sections and save_note_template.
Confirm before sending mail or booking.
To call someone, use place_call. That only prepares the call — tell them to confirm
on the card or say yes. Never invent a patient match or a phone number.
Do not put a reason for the call in the chat. If a tool fails, say so plainly. Keep replies short."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_clients",
            "description": "Look up patients in the practice directory.",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_client",
            "description": "Add a new patient to the directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                },
                "required": ["name", "email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_forms",
            "description": "Email or mint form links. items: intake, personal, consents, card, roi, pay.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "name": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "string"}},
                    "send_email": {"type": "boolean"},
                },
                "required": ["email", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send a branded practice email. kind: form_share, intake, book_link, payment_due, document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "kind": {"type": "string"},
                    "name": {"type": "string"},
                    "notes": {"type": "string"},
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["to", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_visit",
            "description": "Book a follow-up on the calendar. start is ISO datetime.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "start": {"type": "string"},
                    "duration_minutes": {"type": "integer"},
                    "notes": {"type": "string"},
                },
                "required": ["name", "email", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_note_templates",
            "description": "List after-visit / DAP / addendum note templates.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_note_template",
            "description": "Read one note template markdown (id: avs, dap, addendum, or a custom id).",
            "parameters": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note_template",
            "description": "Create or overwrite a note template. body is markdown with {{field}} placeholders.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["id", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_call",
            "description": (
                "Prepare an outbound call from the office DID. Does not dial until "
                "the doctor confirms. Pass client_id, a directory name, or e164."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "name": {"type": "string"},
                    "e164": {
                        "type": "string",
                        "description": "Destination phone, any common format.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_practice",
            "description": "Read About the practice: name, legal, clinician, address, phone, fax, email, EIN, NPI.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_practice",
            "description": "Update About the practice letterhead fields. Omit keys you are not changing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "legal_name": {"type": "string"},
                    "clinician_name": {"type": "string"},
                    "clinician_title": {"type": "string"},
                    "address": {"type": "string"},
                    "phone": {"type": "string"},
                    "fax": {"type": "string"},
                    "email": {"type": "string"},
                    "website": {"type": "string"},
                    "meet": {"type": "string"},
                    "ein": {"type": "string"},
                    "npi": {"type": "string"},
                    "billing_role": {"type": "string", "description": "psychiatrist or therapist"},
                    "theme": {
                        "type": "object",
                        "description": "mode light|dark|custom, allow_toggle, tokens hex map",
                    },
                    "page_titles": {
                        "type": "object",
                        "description": "Titles for the two focus pages. Keys: ocd, anxiety.",
                    },
                    "licenses": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "state": {"type": "string"},
                                "number": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
]


def _tool_config() -> dict[str, Any]:
    specs = []
    for t in TOOLS:
        fn = t.get("function") or {}
        specs.append({"toolSpec": {
            "name": fn.get("name") or "",
            "description": fn.get("description") or fn.get("name") or "",
            "inputSchema": {"json": fn.get("parameters") or {"type": "object", "properties": {}}},
        }})
    return {"tools": specs}


def _bedrock(system: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """One Bedrock converse turn (Nova Pro). Messages are Bedrock-shaped content blocks."""
    if not BEDROCK_TOKEN:
        raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK unset (desk agent runs on Bedrock only)")
    payload = json.dumps(
        {
            "system": [{"text": system}],
            "messages": messages,
            "toolConfig": _tool_config(),
            "inferenceConfig": {"temperature": 0.3, "maxTokens": 2000},
        }
    ).encode()
    req = urllib.request.Request(
        BEDROCK_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + BEDROCK_TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"bedrock {e.code}: {e.read()[:200]!r}") from e


def _run_tool(name: str, args: dict[str, Any], book_fn, send_fn, mail_fn) -> Any:
    if name == "search_clients":
        return search_clients(str(args.get("q") or ""))
    if name == "create_client":
        return create_client(
            name=str(args.get("name") or ""),
            email=str(args.get("email") or ""),
            phone=str(args.get("phone") or ""),
        )
    if name == "send_forms":
        return send_fn(args)
    if name == "send_email":
        return mail_fn(args)
    if name == "book_visit":
        return book_fn(args)
    if name == "list_note_templates":
        from .note_templates import list_templates

        return {"templates": list_templates()}
    if name == "get_note_template":
        from .note_templates import get_template

        spec = get_template(str(args.get("id") or ""))
        return spec or {"error": "unknown template"}
    if name == "save_note_template":
        from .note_templates import save_template

        try:
            return save_template(str(args.get("id") or ""), str(args.get("body") or ""))
        except ValueError as e:
            return {"error": str(e)}
    if name == "place_call":
        from .desk_call import preview_call

        return preview_call(
            client_id=str(args.get("client_id") or ""),
            name=str(args.get("name") or ""),
            e164=str(args.get("e164") or args.get("phone") or ""),
        )
    if name == "get_practice":
        from .practice import load as load_practice, public as public_practice
        from .desk_call import config as call_config

        p = load_practice()
        pub = public_practice()
        call = call_config()
        return {k: p.get(k) for k in (
            "display_name", "legal_name", "clinician_name", "clinician_title",
            "address", "phone", "fax", "email", "website", "meet", "ein", "npi",
            "licenses", "billing_role", "theme", "page_titles",
        )} | {
            "has_logo": pub.get("has_logo"),
            "has_photo": pub.get("has_photo"),
            "has_callback": bool(call.get("has_callback")),
            "outbound_did": call.get("did_pretty") or call.get("did") or "",
        }
    if name == "save_practice":
        from .practice import save as save_practice

        keys = (
            "display_name", "legal_name", "clinician_name", "clinician_title",
            "address", "phone", "fax", "email", "website", "meet", "ein", "npi",
            "licenses", "billing_role", "theme", "page_titles",
        )
        payload = {k: args[k] for k in keys if k in args and args[k] is not None}
        return save_practice(payload)
    return {"error": "unknown tool"}


def _history(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Desk chat history -> Bedrock messages: starts with user, roles alternate (same-role turns merged)."""
    out: list[dict[str, Any]] = []
    for m in history[-16:]:
        role = m.get("role") or "user"
        content = (m.get("content") or "").strip()[:4000]
        if role not in ("user", "assistant") or not content:
            continue
        if not out and role != "user":
            continue
        if out and out[-1]["role"] == role:
            out[-1]["content"][0]["text"] += "\n\n" + content
        else:
            out.append({"role": role, "content": [{"text": content}]})
    while out and out[-1]["role"] != "user":
        out.pop()
    return out


def run_desk_agent(
    history: list[dict[str, str]],
    *,
    book_fn,
    send_fn,
    mail_fn,
) -> dict[str, Any]:
    messages = _history(history)
    if not messages:
        return {"ok": False, "error": "empty"}

    used: list[str] = []
    action: dict[str, Any] | None = None
    for _ in range(5):
        data = _bedrock(SYSTEM, messages)
        msg = (data.get("output") or {}).get("message") or {}
        blocks = [b for b in (msg.get("content") or []) if isinstance(b, dict)]
        calls = [b["toolUse"] for b in blocks if isinstance(b.get("toolUse"), dict)]
        text = "".join(str(b.get("text") or "") for b in blocks).strip()
        if not calls:
            out = {"ok": True, "reply": text or "(no reply)", "model": "bedrock-" + BEDROCK_MODEL, "tools": used}
            if action:
                out["action"] = action
            return out
        messages.append({"role": "assistant", "content": blocks})
        results = []
        for call in calls:
            name = str(call.get("name") or "")
            args = call.get("input") if isinstance(call.get("input"), dict) else {}
            used.append(name)
            try:
                result = _run_tool(name, args, book_fn, send_fn, mail_fn)
            except Exception as e:
                result = {"error": str(e)[:200]}
            if isinstance(result, dict) and result.get("action") == "confirm_call":
                action = {k: result[k] for k in result if k != "pending"}
            results.append({"toolResult": {
                "toolUseId": call.get("toolUseId") or name,
                "content": [{"text": json.dumps(result, default=str)[:6000]}],
            }})
        messages.append({"role": "user", "content": results})
    return {"ok": False, "error": "too many tool rounds", "tools": used}
