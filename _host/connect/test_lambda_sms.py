import os, json, unittest
os.environ.update(BRIDGE_BASE_URL="http://stub", BRIDGE_TOKEN="t", VERIFY_HMAC_KEY="k")
import lambda_function as L

class T(unittest.TestCase):
    def setUp(self):
        self.sent = []
        def http(m, p, body=None, extra=None):
            self.sent.append(body)
            return 200, {"when_label": "Wed", "held": True, "sms_consent": body["sms_consent"],
                         "hold_until_label": "for 48 hours, until Thu", "message": "Your spot is held for 48 hours."}
        L._http = http
        L._caller_said_yes_after = lambda ctx, since: True
    def book(self, **extra):
        a = dict(start="2027-01-27T14:00:00-06:00", name="Phone Test, Synth",
                 email="psycharts@mail.grokbot.com", **{"visit_type": "new-client", **extra})
        ev = lambda args: {"Details": {"Parameters": dict(tool="create_visit", **args),
               "ContactData": {"ContactId": "c1", "CustomerEndpoint": {"Address": "+19133918760"}}}}
        r1 = json.loads(L.lambda_handler(ev(a), None)["result"]); self.assertTrue(r1["needs_confirmation"])
        a["confirm_token"] = r1["confirm_token"]
        return json.loads(L.lambda_handler(ev(a), None)["result"])
    def test_caller_id_yes(self):
        r = self.book(phone="$.Modules.Input.phone", sms_consent="true")
        self.assertEqual(self.sent[-1]["phone"], "+19133918760"); self.assertIs(self.sent[-1]["sms_consent"], True)
        self.assertTrue(r["held"] and r["texts"]); self.assertIn("48 hours", r["message"])
    def test_different_number(self):
        self.book(phone="(816) 555-0100", sms_consent="yes")
        self.assertEqual(self.sent[-1]["phone"], "(816) 555-0100"); self.assertIs(self.sent[-1]["sms_consent"], True)
    def test_this_number_text(self):  # model passed the words instead of digits (Oct 28 call)
        self.book(phone="this number", sms_consent="true")
        self.assertEqual(self.sent[-1]["phone"], "+19133918760"); self.assertIs(self.sent[-1]["sms_consent"], True)
    def test_texts_declined(self):
        r = self.book(sms_consent="false")
        self.assertIs(self.sent[-1]["sms_consent"], False); self.assertFalse(r["texts"])
    def test_unconfirmed_never_books(self):
        L._caller_said_yes_after = lambda ctx, since: False
        r = self.book(sms_consent="true"); self.assertFalse(r["ok"]); self.assertEqual(self.sent, [])
    def test_existing_client_intake_link(self):
        def http(m, p, body=None, extra=None):
            self.sent.append(body)
            return 200, {"when_label": "Wed", "intake_link_sent": True, "sms_consent": body["sms_consent"]}
        L._http = http
        r = self.book(visit_type="new-client", sms_consent="false")
        self.assertTrue(r["intake_link_sent"]); self.assertFalse(r["texts"]); self.assertNotIn("held", r)
        self.assertIn("emailed.", r["message"])
unittest.main()
