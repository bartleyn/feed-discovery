from fastapi import APIRouter
from api.config import settings

router = APIRouter(tags=["did"])


@router.get("/.well-known/did.json")
def did_document():
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": settings.feed_generator_did,
        "service": [
            {
                "id": "#bsky_fg",
                "type": "BskyFeedGenerator",
                "serviceEndpoint": f"https://{settings.feed_generator_hostname}",
            }
        ],
    }
