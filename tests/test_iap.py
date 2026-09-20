"""Run with: ./venv/bin/python tests/test_iap.py  - Apple signed-transaction verification, using a
throwaway certificate chain (the real Apple root can't sign test data)."""
import base64
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import iap

now = datetime.now(timezone.utc)

def make_cert(subject, issuer_name, pub, signer, ca, oid=None, start=None, end=None):
    b = (x509.CertificateBuilder()
         .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
         .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_name)]))
         .public_key(pub).serial_number(x509.random_serial_number())
         .not_valid_before(start or now - timedelta(days=1)).not_valid_after(end or now + timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
    if oid:
        b = b.add_extension(x509.UnrecognizedExtension(oid, b"\x05\x00"), critical=False)
    return b.sign(signer, hashes.SHA256())

def chain(leaf_oid=iap._OID_LEAF):
    rk, ik, lk = (ec.generate_private_key(ec.SECP256R1()) for _ in range(3))
    root = make_cert("Test Root", "Test Root", rk.public_key(), rk, True)
    inter = make_cert("Test Inter", "Test Root", ik.public_key(), rk, True, iap._OID_INTERMEDIATE)
    leaf = make_cert("Test Leaf", "Test Inter", lk.public_key(), ik, False, leaf_oid)
    x5c = [base64.b64encode(c.public_bytes(serialization.Encoding.DER)).decode() for c in (leaf, inter, root)]
    return root, lk, x5c

def sign(payload, lk, x5c):
    return jwt.encode(payload, lk, algorithm="ES256", headers={"x5c": x5c})

def tx(**over):
    base = {"bundleId": iap.BUNDLE_ID, "productId": iap.PRODUCT_MONTHLY, "type": "Auto-Renewable Subscription",
            "originalTransactionId": "1000", "transactionId": "1001", "environment": "Sandbox",
            "expiresDate": int((now + timedelta(days=30)).timestamp() * 1000)}
    base.update(over)
    return base

root, lk, x5c = chain()
good = sign(tx(), lk, x5c)

# a properly signed transaction verifies and yields an entitlement
ent = iap.entitlement_from_transaction(iap.verify_signed_data(good, roots=[root]))
assert ent["original_transaction_id"] == "1000" and not ent["revoked"] and ent["expires_at"] > datetime.utcnow()

def rejected(fn):
    try:
        fn()
    except iap.IapError:
        return True
    return False

# signed by a chain that doesn't lead to the trusted root
other_root, olk, ox5c = chain()
assert rejected(lambda: iap.verify_signed_data(sign(tx(), olk, ox5c), roots=[root]))
# tampered payload (signature no longer matches)
h, p, s = good.split(".")
forged = base64.urlsafe_b64encode(b'{"bundleId":"com.usetaskmonster.ios","productId":"com.usetaskmonster.pro.yearly","originalTransactionId":"1000","expiresDate":99999999999999}').rstrip(b"=").decode()
assert rejected(lambda: iap.verify_signed_data(f"{h}.{forged}.{s}", roots=[root]))
# missing Apple marker extensions
_, blk, bx5c = chain(leaf_oid=None)
assert rejected(lambda: iap.verify_signed_data(sign(tx(), blk, bx5c), roots=[chain()[0]]))
# unsigned / garbage
assert rejected(lambda: iap.verify_signed_data("not.a.jws", roots=[root]))
assert rejected(lambda: iap.verify_signed_data(jwt.encode(tx(), "k", algorithm="HS256"), roots=[root]))
# expired certificate
er = ec.generate_private_key(ec.SECP256R1())
expired_root = make_cert("R", "R", er.public_key(), er, True, end=now - timedelta(days=1), start=now - timedelta(days=5))
assert rejected(lambda: iap.verify_signed_data(good, roots=[expired_root]))

# entitlement rules
assert rejected(lambda: iap.entitlement_from_transaction(tx(bundleId="com.evil.app")))
assert rejected(lambda: iap.entitlement_from_transaction(tx(productId="com.other.product")))
assert rejected(lambda: iap.entitlement_from_transaction(tx(expiresDate=None)))
assert iap.entitlement_from_transaction(tx(revocationDate=1700000000000))["revoked"]

print("ALL TESTS PASSED")
