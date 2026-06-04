import base64
import json
import time

from atproto_crypto.verify import verify_signature
from atproto_identity.did.resolver import DidResolver

_resolver = DidResolver()

_EXPECTED_LXM = "app.bsky.feed.getFeedSkeleton"


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def verify_service_jwt(token: str, expected_aud: str) -> str:
    """Verify an AT Protocol service JWT and return the caller's DID.

    Raises ValueError with a reason string on any failure — callers should
    convert this to HTTP 401.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed JWT")

    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except Exception:
        raise ValueError("unreadable JWT payload")

    iss: str | None = payload.get("iss")
    aud: str | None = payload.get("aud")
    exp: int = payload.get("exp", 0)
    lxm: str | None = payload.get("lxm")

    if not iss:
        raise ValueError("missing iss claim")
    if aud != expected_aud:
        raise ValueError(f"wrong aud: expected {expected_aud!r}, got {aud!r}")
    if exp < time.time():
        raise ValueError("JWT expired")
    if lxm != _EXPECTED_LXM:
        raise ValueError(f"wrong lxm: {lxm!r}")

    did_key = _resolver.resolve_atproto_key(iss)
    if not did_key:
        raise ValueError(f"could not resolve signing key for {iss!r}")

    signing_input = f"{parts[0]}.{parts[1]}".encode()
    signature = _b64url_decode(parts[2])

    if not verify_signature(did_key, signing_input, signature):
        raise ValueError("invalid JWT signature")

    return iss
