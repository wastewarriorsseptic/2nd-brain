"""Run with: ./venv/bin/python tests/test_hardening.py

ICS escaping, safe same-site redirects, cross-site logout, the abuse limiter, and email-verified checks.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A
from fastapi.testclient import TestClient

# ICS: hostile text can't add calendar lines
ics = A._build_event_ics(
    1, "🎉", "Party\r\nATTENDEE;CN=Bad:mailto:evil@x.com", datetime(2026, 9, 25, 10),
    "a\nATTENDEE:evil", "x", organizer_email="me@x.com", attendee_email="g@x.com>\r\nX-BAD:1", attendee_name="Al:ice",
)
lines = ics.split("\r\n")
assert sum(l.startswith("ATTENDEE") for l in lines) == 1
assert not any(l.startswith("X-BAD") for l in lines)

# verified email
assert A._email_is_verified({"email_verified": True})
assert A._email_is_verified({"email_verified": "true"})
assert not A._email_is_verified({"email_verified": False})
assert not A._email_is_verified({"email_verified": "false"})
assert not A._email_is_verified({})

with TestClient(A.app) as c:
    # a cross-site link must not log you out
    assert c.get("/logout", headers={"sec-fetch-site": "cross-site"}, follow_redirects=False).headers["location"] == "/"

    # limiter: 10 invites/hour, then 429
    A._rate_hits.clear()
    codes = [c.post("/realms/share/", data={"realm_id": 1, "email": "a@b.c"}, follow_redirects=False).status_code for _ in range(12)]
    assert 429 not in codes[:10] and set(codes[10:]) == {429}

# same-site return url never points at another host
class R:
    def __init__(self, h): self.headers = h
assert A._same_site_return_url(R({"referer": "https://evil.example/x?y=1", "host": "usetaskmonster.app"})) == "/"
assert A._same_site_return_url(R({"referer": "https://usetaskmonster.app/a?b=1", "host": "usetaskmonster.app"})) == "/a?b=1"

print("ALL TESTS PASSED")
