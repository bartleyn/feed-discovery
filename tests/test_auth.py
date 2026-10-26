import base64
import json
import time

import pytest

from api import auth
from api.auth import (
    LXM_GET_FEED_SKELETON,
    LXM_SEND_INTERACTIONS,
    bearer_token,
    verify_service_jwt,
)

AUD = "did:web:feeds.example"
ISS = "did:plc:caller"


def _b64(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _token(**overrides) -> str:
    payload = {
        "iss": ISS,
        "aud": AUD,
        "lxm": LXM_GET_FEED_SKELETON,
        "exp": int(time.time()) + 60,
    }
    payload.update(overrides)
    header = _b64({"alg": "ES256K", "typ": "JWT"})
    return f"{header}.{_b64(payload)}.c2ln"  # "sig"


@pytest.fixture
def signature_ok(monkeypatch):
    """Skip DID resolution and signature maths; those belong to the atproto libs."""
    monkeypatch.setattr(auth._resolver, "resolve_atproto_key", lambda did: "key")
    monkeypatch.setattr(auth, "verify_signature", lambda key, msg, sig: True)


# --- bearer_token ---

def test_bearer_token_extracts():
    assert bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


@pytest.mark.parametrize("header", [None, "", "Basic xyz", "Bearer ", "Bearer"])
def test_bearer_token_missing(header):
    assert bearer_token(header) is None


# --- verify_service_jwt ---

def test_valid_token_returns_issuer(signature_ok):
    assert verify_service_jwt(_token(), AUD, LXM_GET_FEED_SKELETON) == ISS


def test_lxm_is_endpoint_specific(signature_ok):
    """A getFeedSkeleton token must not authorise sendInteractions, or vice versa."""
    with pytest.raises(ValueError, match="wrong lxm"):
        verify_service_jwt(_token(), AUD, LXM_SEND_INTERACTIONS)
    ok = _token(lxm=LXM_SEND_INTERACTIONS)
    assert verify_service_jwt(ok, AUD, LXM_SEND_INTERACTIONS) == ISS
    with pytest.raises(ValueError, match="wrong lxm"):
        verify_service_jwt(ok, AUD, LXM_GET_FEED_SKELETON)


def test_wrong_aud(signature_ok):
    with pytest.raises(ValueError, match="wrong aud"):
        verify_service_jwt(_token(aud="did:web:other"), AUD, LXM_GET_FEED_SKELETON)


def test_expired(signature_ok):
    with pytest.raises(ValueError, match="expired"):
        verify_service_jwt(_token(exp=int(time.time()) - 120), AUD, LXM_GET_FEED_SKELETON)


def test_small_clock_skew_tolerated(signature_ok):
    assert verify_service_jwt(_token(exp=int(time.time()) - 5), AUD, LXM_GET_FEED_SKELETON) == ISS


def test_missing_iss(signature_ok):
    with pytest.raises(ValueError, match="missing iss"):
        verify_service_jwt(_token(iss=""), AUD, LXM_GET_FEED_SKELETON)


def test_malformed():
    with pytest.raises(ValueError, match="malformed"):
        verify_service_jwt("not.a.jwt.at.all", AUD, LXM_GET_FEED_SKELETON)


def test_bad_signature_rejected(monkeypatch):
    monkeypatch.setattr(auth._resolver, "resolve_atproto_key", lambda did: "key")
    monkeypatch.setattr(auth, "verify_signature", lambda key, msg, sig: False)
    with pytest.raises(ValueError, match="signature"):
        verify_service_jwt(_token(), AUD, LXM_GET_FEED_SKELETON)


def test_unresolvable_issuer(monkeypatch):
    monkeypatch.setattr(auth._resolver, "resolve_atproto_key", lambda did: None)
    with pytest.raises(ValueError, match="could not resolve"):
        verify_service_jwt(_token(), AUD, LXM_GET_FEED_SKELETON)
