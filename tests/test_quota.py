"""Run with: ./venv/bin/python tests/test_quota.py  - AI daily quota + Pro subscription endpoints."""
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from sqlmodel import create_engine, SQLModel, Session
from sqlalchemy.pool import StaticPool

import app as A
import iap

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
SQLModel.metadata.create_all(engine)
A.engine = engine

# --- throwaway Apple-like chain, trusted for this test only ---
now = datetime.now(timezone.utc)
def cert(sub, iss, pub, signer, ca, oid=None):
    b = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, sub)]))
         .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, iss)])).public_key(pub)
         .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
         .not_valid_after(now + timedelta(days=30)).add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if oid:
        b = b.add_extension(x509.UnrecognizedExtension(oid, b"\x05\x00"), critical=False)
    return b.sign(signer, hashes.SHA256())
rk, ik, lk = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
root = cert("R", "R", rk.public_key(), rk, True)
inter = cert("I", "R", ik.public_key(), rk, True, iap._OID_INTERMEDIATE)
leaf = cert("L", "I", lk.public_key(), ik, False, iap._OID_LEAF)
x5c = [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode() for c in (leaf, inter, root)]
iap._default_roots = lambda: [root]

def signed(**over):
    tx = {"bundleId": iap.BUNDLE_ID, "productId": iap.PRODUCT_YEARLY, "type": "Auto-Renewable Subscription",
          "originalTransactionId": "777", "environment": "Sandbox",
          "expiresDate": int((now + timedelta(days=365)).timestamp() * 1000)}
    tx.update(over)
    return jwt.encode(tx, lk, algorithm="ES256", headers={"x5c": x5c})

class Req:
    def __init__(self, uid): self.session = {"user_id": uid}; self.headers = {}
def body(resp): return json.loads(resp.body)

with Session(engine) as s:
    a = A.User(name="A", email="a@x.com"); b = A.User(name="B", email="b@x.com")
    s.add(a); s.add(b); s.commit(); s.refresh(a); s.refresh(b)
    aid, bid = a.id, b.id
    for _ in range(A.AI_FREE_DAILY_LIMIT - 1):
        s.add(A.AiChatMessage(user_id=aid, role="user", content="hi"))
        s.add(A.AiChatMessage(user_id=aid, role="assistant", content="yo"))  # replies never count
    s.add(A.AiChatMessage(user_id=aid, role="user", content="old", created_at=datetime.utcnow() - timedelta(days=2)))  # yesterday
    s.commit()

with Session(engine) as s:
    q = A._ai_quota_status(s, s.get(A.User, aid))
    assert q["plan"] == "free" and q["limit"] == A.AI_FREE_DAILY_LIMIT and q["used"] == A.AI_FREE_DAILY_LIMIT - 1 and q["remaining"] == 1, q

# a valid signed transaction turns Pro on and raises the limit
r = body(A.iap_verify(Req(aid), {"jws": signed()}))
assert r["ok"] and r["plan"] == "pro" and r["limit"] == A.AI_PRO_DAILY_LIMIT, r
# a forged / wrong-app transaction is refused and changes nothing
assert not body(A.iap_verify(Req(bid), {"jws": "not.a.jws"}))["ok"]
assert not body(A.iap_verify(Req(bid), {"jws": signed(bundleId="com.evil")}))["ok"]
with Session(engine) as s:
    assert not A.user_is_pro(s.get(A.User, bid))
# a second account can't claim a subscription that's active on the first
resp = A.iap_verify(Req(bid), {"jws": signed()})
assert resp.status_code == 409
# Apple's server notification (refund) turns Pro off for the linked account
note = jwt.encode({"notificationType": "REFUND", "data": {"signedTransactionInfo": signed(revocationDate=1700000000000)}},
                  lk, algorithm="ES256", headers={"x5c": x5c})
assert body(A.iap_notifications({"signedPayload": note}))["ok"]
with Session(engine) as s:
    assert not A.user_is_pro(s.get(A.User, aid))
# ...and an unsigned notification is refused
assert A.iap_notifications({"signedPayload": "junk"}).status_code == 400

# the App Review demo account is enforced even while enforcement is off for everyone else
with Session(engine) as s:
    demo = A.User(name="Demo", email="usetaskmonsterapp@gmail.com"); s.add(demo); s.commit(); s.refresh(demo)
    assert A._ai_quota_status(s, demo)["enforced"] is True
    assert A._ai_quota_status(s, s.get(A.User, bid))["enforced"] is A.AI_QUOTA_ENFORCED

print("ALL TESTS PASSED")
