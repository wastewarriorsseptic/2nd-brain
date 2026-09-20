"""Run with: ./venv/bin/python tests/test_csrf.py

Cross-site state-changing requests must be rejected; same-site ones, Sign in with Apple's callback,
and plain page loads must keep working.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A
from fastapi.testclient import TestClient

with TestClient(A.app) as c:
    def post(headers):
        return c.post("/items/delete/", data={"item_id": 1}, headers=headers, follow_redirects=False).status_code

    assert post({}) != 403, "requests with no browser markers should pass"
    assert post({"origin": "http://testserver"}) != 403, "same-origin POST must pass"
    assert post({"origin": "https://evil.example"}) == 403, "cross-origin POST must be blocked"
    assert post({"origin": "null"}) == 403, "null origin must be blocked"
    assert post({"sec-fetch-site": "cross-site"}) == 403, "cross-site fetch must be blocked"
    assert post({"referer": "https://evil.example/x"}) == 403, "cross-site referer must be blocked"
    assert c.post("/auth/callback/apple", headers={"origin": "https://appleid.apple.com"}, follow_redirects=False).status_code != 403, "Apple callback is exempt"
    assert c.get("/privacy", headers={"origin": "https://evil.example"}).status_code == 200, "GET is never blocked"

print("ALL TESTS PASSED")
