import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from api.config import settings

_LXM = "app.bsky.feed.getFeedSkeleton"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def create_service_jwt(aud: str, sub: str | None = None) -> str:
    """Create a signed service-auth JWT for calling another feed generator.

    iss = our feed generator DID (did:web:feeds.barn.city)
    aud = target feed generator's DID
    sub = requesting user's DID (used by personalized generators as the viewer)
    lxm = app.bsky.feed.getFeedSkeleton
    """
    if not settings.feed_generator_signing_key:
        raise RuntimeError("FEED_GENERATOR_SIGNING_KEY not set in .env")

    private_key = ec.derive_private_key(
        int(settings.feed_generator_signing_key, 16), ec.SECP256K1()
    )

    header = {"alg": "ES256K", "typ": "JWT"}
    payload: dict = {
        "iss": settings.feed_generator_did,
        "aud": aud,
        "lxm": _LXM,
        "exp": int(time.time()) + 60,
    }
    if sub:
        payload["sub"] = sub

    h = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode()

    sig_der = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(sig_der)
    sig_bytes = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return f"{h}.{p}.{_b64url_encode(sig_bytes)}"
