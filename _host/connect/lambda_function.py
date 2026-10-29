"""Practice Desk booker bridge for Amazon Connect AI agent tools (us-east-1).
Tools: list_visit_slots, create_visit, verify_patient, notify_matt.
Accepts Connect flow-module events (Details.Parameters) or direct test events {"tool":..,"args":{..}}.
Never logs caller-supplied values (PHI).

Server-side guards (v2):
- verify_patient success returns an HMAC verify_token bound to this ContactId + the verified identifier, 20 min expiry.
- follow-up list_visit_slots / create_visit require a valid verify_token for the SAME contact.
- create_visit is two-phase: first call returns a confirm_token + read-back; the second call must carry it,
  and the caller must have spoken an affirmative reply AFTER the read-back (checked in the contact's
  AI-agent session transcript)."""
import json, os, re, time, hmac, hashlib, base64, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = os.environ["BRIDGE_BASE_URL"].rstrip("/")          # e.g. https://hooks.psycharts.org/desk-bridge
TOKEN = os.environ["BRIDGE_TOKEN"]                         # checked by Caddy
NOTIFY_TOKEN = os.environ.get("NOTIFY_TOKEN", "")          # checked by notify server
HKEY = os.environ.get("VERIFY_HMAC_KEY", "").encode()      # signs verify/confirm tokens
INSTANCE_ID = os.environ.get("CONNECT_INSTANCE_ID", "")
ASSISTANT_ID = os.environ.get("ASSISTANT_ID", "")
VERIFY_ON = os.environ.get("VERIFY_PATIENT", "true") == "true"
DRY_RUN = os.environ.get("DRY_RUN_BOOKINGS", "false") == "true"
TZ = ZoneInfo("America/Chicago")
VERIFY_TTL, CONFIRM_TTL = 1200, 600
PHONE_NOTICE_HOURS, PHONE_WINDOW_DAYS = 14, 92   # phone line: offer times 14h..92d out
YES_RE = re.compile(r"\b(yes|yeah|yep|yup|correct|confirm(ed)?|sure|right|ok(ay)?|go ahead|book it|please do|sounds good|that works|absolutely|affirmative)\b", re.I)
NO_RE = re.compile(r"\b(no|nope|don'?t|do not|wait|stop|cancel|wrong|not)\b", re.I)

def _http(method, path, body=None, extra=None):
    h = {"Content-Type": "application/json", "X-Desk-Token": TOKEN}
    h.update(extra or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: d = json.loads(e.read() or b"{}")
        except Exception: d = {}
        return e.code, d

def _label(iso):
    try: return datetime.fromisoformat(iso).astimezone(TZ).strftime("%A %B %-d at %-I:%M %p Central")
    except Exception: return iso

def _clean(v):
    v = (v or "").strip() if isinstance(v, str) else ("" if v is None else str(v))
    return "" if v.startswith("$.") else v      # unresolved flow-module input

def _norm_email(e): return _clean(e).lower()
def _norm_phone(p): return re.sub(r"\D", "", _clean(p))[-10:]
def _h(s): return hashlib.sha256(s.encode()).hexdigest()[:20]
def _b64(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
def _b64d(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def _sign(kind, payload):
    payload = dict(payload, k=kind)
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return body + "." + _b64(hmac.new(HKEY, (kind + "." + body).encode(), hashlib.sha256).digest()[:18])

def _unsign(kind, tok):
    try:
        body, sig = _clean(tok).split(".", 1)
        exp = _b64(hmac.new(HKEY, (kind + "." + body).encode(), hashlib.sha256).digest()[:18])
        if not HKEY or not hmac.compare_digest(exp, sig): return None
        p = json.loads(_b64d(body))
        return p if p.get("k") == kind else None
    except Exception:
        return None

VERIFY_ATTR = "pd_verify_token"

def _stash_verify(ctx, tok):
    """Keep the signed verify_token on the contact so later tools need not echo it."""
    try:
        import boto3
        boto3.client("connect").update_contact_attributes(
            InstanceId=INSTANCE_ID, InitialContactId=ctx["contact_id"], Attributes={VERIFY_ATTR: tok})
    except Exception as e:
        print(json.dumps({"verify_stash_error": type(e).__name__, "contact_id": ctx.get("contact_id")}))

def _stashed_verify(ctx):
    try:
        import boto3
        r = boto3.client("connect").get_contact_attributes(InstanceId=INSTANCE_ID, InitialContactId=ctx["contact_id"])
        return (r.get("Attributes") or {}).get(VERIFY_ATTR, "")
    except Exception as e:
        print(json.dumps({"verify_fetch_error": type(e).__name__, "contact_id": ctx.get("contact_id")}))
        return ""

def _check_pv(tok, ctx):
    p = _unsign("pv", tok)
    if not p or not ctx.get("contact_id") or p.get("c") != ctx["contact_id"] or int(p.get("x", 0)) < time.time():
        return None
    return p

def _verified(a, ctx):
    """Valid verify_token for THIS contact, unexpired. Returns payload or None.
    Falls back to the token stashed on the contact when the model drops or garbles it."""
    p = _check_pv(a.get("verify_token"), ctx)
    if p or not ctx.get("contact_id"): return p
    return _check_pv(_stashed_verify(ctx), ctx)

NEED_VERIFY = {"ok": False, "error": "verification_required",
               "message": "Follow-up times and booking are only available after verify_patient succeeds on this call. "
                          "Ask for the email or phone on file and run verify_patient, or offer book.psycharts.org or a message."}

def _caller_said_yes_after(ctx, since):
    """True if the caller spoke an affirmative (and not a negative) message after `since` in this contact's AI session."""
    try:
        import boto3
        cid = ctx.get("contact_id")
        c = boto3.client("connect")
        arn = (c.describe_contact(InstanceId=INSTANCE_ID, ContactId=cid)["Contact"].get("WisdomInfo") or {}).get("SessionArn", "")
        if not arn: return False
        q = boto3.client("qconnect")
        msgs = q.list_messages(assistantId=ASSISTANT_ID, sessionId=arn.split("/")[-1], maxResults=20)["messages"]
        after = [m for m in msgs if m.get("participant") == "CUSTOMER" and m["timestamp"].timestamp() > since]
        if not after: return False
        last = max(after, key=lambda m: m["timestamp"])
        t = ((last.get("value") or {}).get("text") or {}).get("value") or ""
        return bool(YES_RE.search(t)) and not NO_RE.search(t)
    except Exception as e:
        print(json.dumps({"confirm_check_error": type(e).__name__, "contact_id": ctx.get("contact_id")}))
        return False

def list_visit_slots(a, ctx):
    vt = a.get("visit_type") or "new-client"
    if vt not in ("new-client", "follow-up"): return {"ok": False, "error": "visit_type must be new-client or follow-up"}
    if vt == "follow-up" and not VERIFY_ON:
        return {"ok": False, "error": "follow_up_disabled", "message": "Direct current patients to book.psycharts.org."}
    if vt == "follow-up" and not _verified(a, ctx): return NEED_VERIFY
    q = {"type": vt}
    if vt == "follow-up":
        d = str(a.get("duration_minutes") or "")
        if d not in ("30", "60"): return {"ok": False, "error": "ask whether the follow-up is 30 or 60 minutes"}
        q["duration"] = d
    now = datetime.now(TZ)
    lo, hi = now + timedelta(hours=PHONE_NOTICE_HOURS), now + timedelta(days=PHONE_WINDOW_DAYS)
    span = f"Phone booking covers {_label(lo.isoformat())} through {hi.strftime('%A %B %-d')}."
    want = _clean(a.get("date"))
    if want:
        try: wd = datetime.strptime(want, "%Y-%m-%d").date()
        except ValueError: return {"ok": False, "error": "date must be YYYY-MM-DD"}
        if not lo.date() <= wd <= hi.date():
            return {"ok": False, "error": "date_out_of_range", "message": span + " Ask for a date in that range."}
        q["date"] = want
    st, d = _http("GET", "/api/slots?" + urllib.parse.urlencode(q))
    if st == 404:
        return {"ok": False, "error": "visit_type_closed", "message": "That visit type is not open for phone booking right now. "
                "Do not promise a time. Offer book.psycharts.org or take a message with notify_matt."}
    if st != 200: return {"ok": False, "error": f"booker {st}", "detail": str(d.get("detail", ""))[:120]}
    raw = [s["start"] for s in (d.get("slots") or []) if lo <= datetime.fromisoformat(s["start"]) <= hi]
    same = [s for s in raw if s[:10] == want] if want else []
    if same:
        picks, note = same[:16], "All open times on the requested date."
    else:
        picks, by_day = [], {}
        for s in raw:
            if len(by_day) >= 4 and s[:10] not in by_day: break
            if len(by_day.setdefault(s[:10], [])) < 3: by_day[s[:10]].append(s); picks.append(s)
        note = ("No openings on the requested date; these are the next ones after it." if want else
                "A sample of the next open days, up to three times per day.")
    slots = [{"start": s, "label": _label(s)} for s in picks]
    return {"ok": True, "visit_type": vt, "duration_minutes": d.get("duration_minutes"), "timezone": "America/Chicago",
            "slots": slots, "message": (note + " Offer two or three of these Central times. Use the exact start when booking. "
            + span + " For another day, call list_visit_slots again with date as YYYY-MM-DD.") if slots
            else "No open times. " + span + " Offer book.psycharts.org or notify_matt."}

def verify_patient(a, ctx):
    if not VERIFY_ON: return {"ok": False, "error": "verification_disabled"}
    email, phone = _clean(a.get("email")), _clean(a.get("phone"))
    if not (email or phone): return {"ok": False, "reason": "need_email_or_phone",
                                     "message": "Ask for the email or phone number on file."}
    st, d = _http("POST", "/api/verify-patient", {"email": email, "phone": phone})
    if st != 200 or not d.get("ok"):
        return {"ok": False, "reason": d.get("reason", "not_found"),
                "message": "Could not verify. Do not book a follow-up. Offer book.psycharts.org or notify_matt."}
    if not ctx.get("contact_id") or not HKEY:
        return {"ok": False, "reason": "verification_unavailable", "message": "Offer book.psycharts.org or a message."}
    ident = ("e:" + _norm_email(email)) if email else ("p:" + _norm_phone(phone))
    tok = _sign("pv", {"c": ctx["contact_id"], "i": _h(ident), "x": int(time.time()) + VERIFY_TTL})
    _stash_verify(ctx, tok)
    return {"ok": True, "verify_token": tok,
            "message": "Verified current patient. Pass verify_token exactly as given to list_visit_slots and create_visit "
                       "for follow-ups, with the same email or phone."}

def create_visit(a, ctx):
    vt = _clean(a.get("visit_type"))
    start, name, email, phone = _clean(a.get("start")), _clean(a.get("name")), _norm_email(a.get("email")), _clean(a.get("phone")) or _clean(ctx.get("caller"))
    if len(_norm_phone(phone)) < 10: phone = _clean(ctx.get("caller"))   # e.g. "this number"
    sms_ok = _clean(a.get("sms_consent")).lower() in ("true", "yes", "1")
    for k, v in (("start", start), ("name", name), ("email", email)):
        if not v: return {"ok": False, "error": f"{k} required"}
    if vt not in ("new-client", "follow-up"): return {"ok": False, "error": "bad visit_type"}
    if not ctx.get("contact_id"): return {"ok": False, "error": "no_contact", "message": "Offer book.psycharts.org."}
    dur = ""
    if vt == "follow-up":
        if not VERIFY_ON: return {"ok": False, "error": "follow_up_disabled"}
        v = _verified(a, ctx)
        if not v: return NEED_VERIFY
        if v.get("i") not in (_h("e:" + email), _h("p:" + _norm_phone(phone))):
            return {"ok": False, "error": "identity_mismatch",
                    "message": "Use the same email or phone that passed verify_patient on this call."}
        dur = _clean(a.get("duration_minutes")) or "30"
        if dur not in ("30", "60"): return {"ok": False, "error": "duration must be 30 or 60"}
    if len(name) > 80 or re.search(r"[<>{}\[\]|\\]|https?://", name):
        return {"ok": False, "error": "invalid_name", "message": "Ask the caller to say their first and last name."}
    try: datetime.fromisoformat(start)
    except Exception: return {"ok": False, "error": "start must be an exact ISO start from list_visit_slots"}
    fp = _h("|".join([ctx["contact_id"], vt, start, dur, email, _norm_phone(phone), name.lower()]))
    ct = _clean(a.get("confirm_token"))
    if not ct:
        tok = _sign("cf", {"c": ctx["contact_id"], "f": fp, "t": int(time.time())})
        return {"ok": False, "needs_confirmation": True, "confirm_token": tok, "read_back": _label(start),
                "message": "NOT BOOKED YET. Read back the day and time (read_back), the visit type, and spell back the email, "
                           "then ask the caller to confirm. Only after the caller clearly says yes, call create_visit again "
                           "with the same details plus confirm_token."}
    p = _unsign("cf", ct)
    if not p or p.get("c") != ctx["contact_id"] or p.get("f") != fp:
        return {"ok": False, "error": "confirmation_mismatch",
                "message": "Details changed since the read-back. Call create_visit without confirm_token to read back again."}
    issued = int(p.get("t", 0))
    if time.time() - issued > CONFIRM_TTL:
        return {"ok": False, "error": "confirmation_expired", "message": "Read back the details again and get a new yes."}
    if not _caller_said_yes_after(ctx, issued):
        return {"ok": False, "error": "caller_not_confirmed",
                "message": "The caller has not said yes since the read-back. Read back the time and ask them to confirm."}
    body = {"event_type": vt, "start": start, "name": name, "email": email,
            "phone": phone, "notes": "Booked by Connect voice agent", "source": "voice", "sms_consent": sms_ok}
    if vt == "follow-up": body["duration_minutes"] = int(dur)
    if DRY_RUN: return {"ok": True, "dry_run": True, "when": _label(start), "message": "DRY RUN: not booked."}
    st, d = _http("POST", "/api/book", body)
    if st != 200: return {"ok": False, "error": f"booker {st}", "detail": str(d.get("detail", ""))[:160]}
    if d.get("held"):
        return {"ok": True, "held": True, "when": d.get("when_label") or _label(start),
                "hold_until": d.get("hold_until_label", ""), "texts": bool(d.get("sms_consent")),
                "message": d.get("message", "")}
    if d.get("intake_link_sent"):   # existing client, intake unfinished: link sent, no hold
        return {"ok": True, "intake_link_sent": True, "when": d.get("when_label") or _label(start),
                "texts": bool(d.get("sms_consent")),
                "message": "Booked. A link to finish paperwork and add a card on file was emailed"
                           + (" and texted." if d.get("sms_consent") else ".")}
    return {"ok": True, "when": d.get("when_label") or _label(start),
            "message": "Booked. Confirmation email is on the way. Join at meet.drmattbrown.com."}

def notify_matt(a, ctx):
    m = (a.get("preferred_contact_method") or "phone").lower()
    if m not in ("phone", "sms", "email", "secure_chat"): return {"ok": False, "error": "bad preferred_contact_method"}
    if m != "secure_chat" and not a.get("contact_value"): return {"ok": False, "error": "contact_value required"}
    if not a.get("summary"): return {"ok": False, "error": "summary required"}
    p = {"callId": ctx.get("contact_id", "connect"), "callerFrom": ctx.get("caller", ""),
         "caller_name": a.get("caller_name") or "Unknown", "preferred_contact_method": m,
         "contact_value": a.get("contact_value") or "", "summary": a["summary"][:600],
         "urgency": a.get("urgency") or "normal", "at": datetime.now(TZ).isoformat(), "source": "connect"}
    st, d = _http("POST", "/notify", p, {"X-Notify-Token": NOTIFY_TOKEN})
    ok = st == 200
    print(json.dumps({"notify_status": st, "contact_id": p["callId"]}))
    return {"ok": ok, "message": "Follow-up recorded." if ok else
            "Could not record. Direct them to drmattbrown.com contact form or matt@drmattbrown.com."}

TOOLS = {"list_visit_slots": list_visit_slots, "verify_patient": verify_patient, "create_visit": create_visit, "notify_matt": notify_matt}

def lambda_handler(event, context):
    det = event.get("Details") or {}
    params = dict(det.get("Parameters") or {})
    tool = event.get("tool") or params.pop("tool", "")
    args = event.get("args") or params
    cd = det.get("ContactData") or {}
    ctx = {"contact_id": cd.get("ContactId", ""), "caller": (cd.get("CustomerEndpoint") or {}).get("Address", "")}
    try:
        res = TOOLS[tool](args, ctx) if tool in TOOLS \
            else {"ok": False, "error": f"unknown tool {tool}"}
    except Exception as e:
        res = {"ok": False, "error": type(e).__name__}
    print(json.dumps({"tool": tool, "ok": res.get("ok"), "contact_id": ctx["contact_id"]}))
    # Connect needs a flat string map
    return {"ok": str(bool(res.get("ok"))).lower(), "result": json.dumps(res)}
