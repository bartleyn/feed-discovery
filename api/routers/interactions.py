"""
POST /xrpc/app.bsky.feed.sendInteractions

bsky.app calls this endpoint when the user interacts with posts in our feed.
Each interaction includes the post URI, event type, and our feedContext (= feed URI).

We log the interaction against the most recent matching impression and, for
explicit requestMore / requestLess events, immediately update the arm.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

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


class SendInteractionsIn(BaseModel):
    interactions: list[InteractionItem]


@router.post("/xrpc/app.bsky.feed.sendInteractions")
def send_interactions(body: SendInteractionsIn, db: Session = Depends(get_db)):
    user_did = settings.default_user_did
    now = datetime.now(timezone.utc)

    for item in body.interactions:
        mapped = EVENT_MAP.get(item.event)
        if mapped is None:
            continue

        action, immediate_reward = mapped
        feed_uri = item.feedContext

        # Find the most recent unrewarded impression for this post + feed
        chunk_post = (
            db.query(ChunkPost)
            .join(Impression, ChunkPost.impression_id == Impression.id)
            .filter(
                ChunkPost.post_uri == item.item,
                Impression.user_did == user_did,
                Impression.feed_uri == feed_uri,
                Impression.reward.is_(None),
            )
            .order_by(Impression.shown_at.desc())
            .first()
        )

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
