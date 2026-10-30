import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update(BRIDGE_BASE_URL="http://stub", BRIDGE_TOKEN="t", VERIFY_HMAC_KEY="k")
import lambda_function as L


class T(unittest.TestCase):
    def test_crisis_notify_uses_caller_and_flags(self):
        sent = []
        L._http = lambda m, p, body=None, extra=None: (sent.append((p, body)) or (200, {}))
        r = L.notify_matt({"urgency": "crisis", "preferred_contact_method": "phone", "summary": "synthetic test"},
                          {"contact_id": "c1", "caller": "+19133918760"})
        self.assertTrue(r["ok"])
        p, b = sent[-1]
        self.assertEqual(p, "/notify"); self.assertEqual(b["contact_value"], "+19133918760")
        self.assertTrue(b["summary"].startswith("CRISIS:")); self.assertEqual(b["urgency"], "crisis")

    def test_normal_still_needs_contact(self):
        L._http = lambda *a, **k: (200, {})
        r = L.notify_matt({"preferred_contact_method": "phone", "summary": "x"}, {"caller": "+19133918760"})
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
