"""Staff secret picker — no live secrets."""
from app.staff_auth import pick_staff_secret, secret_from_request


class _Req:
    def __init__(self, headers=None, cookies=None, query=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}


def test_header_wins():
    assert (
        pick_staff_secret(header="H", cookie="C", query="Q") == "H"
    )


def test_cookie_then_query():
    assert pick_staff_secret(header="", cookie="C", query="Q") == "C"
    assert pick_staff_secret(header="", cookie="", query="Q") == "Q"
    assert pick_staff_secret(header="  ", cookie="  ", query="") == ""


def test_request_header():
    req = _Req(headers={"x-staff-secret": "from-head"}, cookies={"dmb-desk": "from-cookie"})
    assert secret_from_request(req, "from-query") == "from-head"


def test_request_cookie():
    req = _Req(headers={}, cookies={"dmb-desk": "from-cookie"})
    assert secret_from_request(req, "from-query") == "from-cookie"
