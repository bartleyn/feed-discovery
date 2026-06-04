from fastapi import APIRouter
from api.config import settings

router = APIRouter(tags=["did"])

FEED_RKEY = "feed-discovery"


@router.get("/.well-known/did.json")
def did_document():
    doc: dict = {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/security/multikey/v1",
        ],
        "id": settings.feed_generator_did,
        "verificationMethod": [
            {
                "id": f"{settings.feed_generator_did}#atproto",
                "type": "Multikey",
                "controller": settings.feed_generator_did,
                "publicKeyMultibase": settings.feed_generator_public_key_multibase,
            }
        ],
        "service": [
            {
                "id": "#bsky_fg",
                "type": "BskyFeedGenerator",
                "serviceEndpoint": f"https://{settings.feed_generator_hostname}",
            }
        ],
    }
    return doc


@router.get("/xrpc/app.bsky.feed.describeFeedGenerator")
def describe_feed_generator():
    feed_uri = f"at://{settings.default_user_did}/app.bsky.feed.generator/{FEED_RKEY}"
    return {
        "did": settings.feed_generator_did,
        "feeds": [
            {
                "uri": feed_uri,
                "acceptsInteractions": True,
            }
        ],
    }
