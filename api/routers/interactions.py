"""
POST /xrpc/app.bsky.feed.sendInteractions

bsky.app calls this endpoint when the user interacts with posts in our feed.
Each interaction includes the post URI, event type, our feedContext
("<feed_uri>|<impression_id>"), and the reqId from the serving request.

We log the interaction against the most recent matching impression and, for
explicit requestMore / requestLess events, immediately update the arm.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.auth import LXM_SEND_INTERACTIONS, bearer_token, verify_service_jwt
from api.config import settings
from bandit.thompson import update_arm
from store import get_db
from store.models import ChunkPost, Impression, Interaction

logger = logging.getLogger(__name__)
router = APIRouter(tags=["interactions"])

# Map AT Protocol event types to our action labels and reward values.
# requestMore / requestLess trigger immediate arm updates in addition to logging.
EVENT_MAP: dict[str, tuple[str, float | None]] = {
    "app.bsky.feed.defs#interactionLike":    ("like",        None),
    "app.bsky.feed.defs#interactionRepost":  ("repost",      None),
    "app.bsky.feed.defs#interactionReply":   ("reply",       None),
    "app.bsky.feed.defs#interactionQuote":   ("quote",       None),
    "app.bsky.feed.defs#requestMore":        ("request_more", 1.0),
    "app.bsky.feed.defs#requestLess":        ("request_less", 0.0),
    "app.bsky.feed.defs#interactionSeen":    ("seen",         None),
}


class InteractionItem(BaseModel):
    item: str           # post URI
    event: str          # app.bsky.feed.defs#...
    feedContext: str | None = None
    reqId: str | None = None


class SendInteractionsIn(BaseModel):
    interactions: list[InteractionItem]


# feedContext we emit is "<feed_uri>|<impression_id>" (see feed_generator.py).
# Older impressions carry a bare feed_uri, so the impression id is optional.
CONTEXT_SEP = "|"


def _parse_feed_context(feed_context: str | None) -> tuple[str | None, int | None]:
    """Split feedContext into (feed_uri, impression_id). Tolerates the legacy
    bare-feed_uri form and any unexpected shape by returning what it can."""
    if not feed_context:
        return None, None
    feed_uri, sep, tail = feed_context.rpartition(CONTEXT_SEP)
    if sep and tail.isdigit():
        return feed_uri, int(tail)
    return feed_context, None


def _resolve_chunk_post(db, item: "InteractionItem", user_did: str):
    """Locate the exact ChunkPost an interaction refers to, preferring the
    impression id baked into feedContext, then reqId, then a best-effort
    most-recent match. Returns (chunk_post, feed_uri) or (None, feed_uri)."""
    feed_uri, impression_id = _parse_feed_context(item.feedContext)

    # 1. Exact: feedContext carries the impression id — reordering-proof.
    if impression_id is not None:
        chunk_post = (
            db.query(ChunkPost)
            .filter(ChunkPost.impression_id == impression_id, ChunkPost.post_uri == item.item)
            .first()
        )
        if chunk_post is not None:
            return chunk_post, (feed_uri or chunk_post.impression.feed_uri)

    # 2. reqId groups one request's impressions — narrow to this serve.
    if item.reqId:
        chunk_post = (
            db.query(ChunkPost)
            .join(Impression, ChunkPost.impression_id == Impression.id)
            .filter(
                ChunkPost.post_uri == item.item,
                Impression.user_did == user_did,
                Impression.req_id == item.reqId,
            )
            .order_by(Impression.shown_at.desc())
            .first()
        )
        if chunk_post is not None:
            return chunk_post, (feed_uri or chunk_post.impression.feed_uri)

    # 3. Legacy fallback: most recent unrewarded impression for post + feed.
    chunk_post = (
        db.query(ChunkPost)
        .join(Impression, ChunkPost.impression_id == Impression.id)
        .filter(
            ChunkPost.post_uri == item.item,
            Impression.user_did == user_did,
            Impression.feed_uri == feed_uri,
            Impression.rewarded_at.is_(None),
        )
        .order_by(Impression.shown_at.desc())
        .first()
    )
    return chunk_post, feed_uri


@router.post("/xrpc/app.bsky.feed.sendInteractions")
def send_interactions(
    body: SendInteractionsIn,
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    # The AppView forwards interactions with a service JWT 
    token = bearer_token(authorization)
    if token is None:
        logger.warning("sendInteractions without bearer token rejected")
        raise HTTPException(status_code=401, detail="missing service token")
    try:
        user_did = verify_service_jwt(token, settings.feed_generator_did, LXM_SEND_INTERACTIONS)
    except ValueError as exc:
        logger.warning("sendInteractions token rejected: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc))

    now = datetime.now(timezone.utc)

    for item in body.interactions:
        mapped = EVENT_MAP.get(item.event)
        if mapped is None:
            continue

        action, immediate_reward = mapped

        chunk_post, feed_uri = _resolve_chunk_post(db, item, user_did)
        if chunk_post is None:
            continue

        # Avoid duplicate interaction rows for the same (impression, post, action)
        exists = (
            db.query(Interaction)
            .filter_by(
                impression_id=chunk_post.impression_id,
                post_uri=item.item,
                action=action,
            )
            .first()
        )
        if exists:
            continue

        db.add(Interaction(
            impression_id=chunk_post.impression_id,
            post_uri=item.item,
            user_did=user_did,
            action=action,
            occurred_at=now,
        ))
        db.flush()

        # requestMore / requestLess: skip the reward window, update the arm now
        if immediate_reward is not None and feed_uri:
            update_arm(user_did, feed_uri, immediate_reward, db)
            logger.info(
                "Immediate arm update: feed=%s action=%s reward=%.1f",
                feed_uri, action, immediate_reward,
            )

    db.commit()
    return {}
