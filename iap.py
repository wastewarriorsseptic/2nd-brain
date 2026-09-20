"""Apple In-App Purchase (StoreKit 2) verification.

The iPhone app hands the server a signed transaction (a JWS) after a purchase / on launch / on
restore. Apple signs these with a certificate chain that ends at Apple's root CA, so we can check
one without calling Apple: verify the chain against the embedded Apple Root CA - G3, then verify the
JWS signature with the leaf certificate. Nothing in the payload is trusted until that passes.
"""
import base64
import os
from datetime import datetime, timezone
from typing import Iterable, List, Optional

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes

BUNDLE_ID = "com.usetaskmonster.ios"
PRODUCT_MONTHLY = "com.usetaskmonster.pro.monthly"
PRODUCT_YEARLY = "com.usetaskmonster.pro.yearly"
PRODUCT_IDS = {PRODUCT_MONTHLY, PRODUCT_YEARLY}

# Apple's marker extensions: the leaf must be an "App Store receipt signing" cert issued by the
# WWDR-style intermediate - stops any other Apple-issued certificate from being used to sign data.
_OID_LEAF = x509.ObjectIdentifier("1.2.840.113635.100.6.11.1")
_OID_INTERMEDIATE = x509.ObjectIdentifier("1.2.840.113635.100.6.2.1")

_ROOT_PEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "AppleRootCA-G3.pem")


class IapError(Exception):
    pass


def _default_roots() -> List[x509.Certificate]:
    with open(_ROOT_PEM, "rb") as f:
        return [x509.load_pem_x509_certificate(f.read())]


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def verify_signed_data(token: str, roots: Optional[Iterable[x509.Certificate]] = None,
                       check_oids: bool = True, now: Optional[datetime] = None) -> dict:
    """Returns the verified payload of an Apple-signed JWS, or raises IapError."""
    roots = list(roots) if roots is not None else _default_roots()
    now = _utc(now or datetime.now(timezone.utc))
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "ES256":
            raise IapError("unexpected signing algorithm")
        chain = [x509.load_der_x509_certificate(base64.b64decode(c)) for c in header.get("x5c", [])]
    except IapError:
        raise
    except Exception as e:
        raise IapError(f"malformed token: {e}")
    if len(chain) < 2:
        raise IapError("certificate chain too short")

    for cert in chain:
        if not (_utc(cert.not_valid_before_utc) <= now <= _utc(cert.not_valid_after_utc)):
            raise IapError("certificate outside its validity period")
    try:
        for child, parent in zip(chain, chain[1:]):
            child.verify_directly_issued_by(parent)
        top = chain[-1]
        top_fp = top.fingerprint(hashes.SHA256())
        if not any(r.fingerprint(hashes.SHA256()) == top_fp for r in roots):
            for r in roots:
                try:
                    top.verify_directly_issued_by(r)
                    break
                except Exception:
                    continue
            else:
                raise IapError("chain does not lead to the Apple root")
    except IapError:
        raise
    except Exception as e:
        raise IapError(f"certificate chain invalid: {e}")

    if check_oids:
        try:
            chain[0].extensions.get_extension_for_oid(_OID_LEAF)
            chain[1].extensions.get_extension_for_oid(_OID_INTERMEDIATE)
        except x509.ExtensionNotFound:
            raise IapError("certificate is not an App Store signing certificate")

    try:
        return jwt.decode(
            token, chain[0].public_key(), algorithms=["ES256"],
            options={"verify_aud": False, "verify_exp": False, "verify_iat": False, "verify_nbf": False},
        )
    except Exception as e:
        raise IapError(f"bad signature: {e}")


def entitlement_from_transaction(tx: dict) -> dict:
    """Validates a verified transaction payload for THIS app and returns
    {original_transaction_id, product_id, expires_at (naive UTC), revoked, environment}."""
    if tx.get("bundleId") != BUNDLE_ID:
        raise IapError("transaction is for a different app")
    if tx.get("productId") not in PRODUCT_IDS:
        raise IapError("unknown product")
    if tx.get("type") and tx.get("type") != "Auto-Renewable Subscription":
        raise IapError("not a subscription")
    original = str(tx.get("originalTransactionId") or "")
    expires_ms = tx.get("expiresDate")
    if not original or not expires_ms:
        raise IapError("transaction is missing required fields")
    return {
        "original_transaction_id": original,
        "product_id": tx["productId"],
        "expires_at": datetime.fromtimestamp(int(expires_ms) / 1000, tz=timezone.utc).replace(tzinfo=None),
        "revoked": bool(tx.get("revocationDate")),
        "environment": tx.get("environment", ""),
    }
