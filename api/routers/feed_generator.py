"""
GET /xrpc/app.bsky.feed.getFeed

Phase 2: round-robin across all registered feeds, logs impressions + chunk_posts.
Phase 3 will swap the ordering for Thompson Sampling.
"""

import logging
import random
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.config import settings
from ingestion import atproto_client
from store import get_db
from store.models import Feed, Impression, ChunkPost

logger = logging.getLogger(__name__)
router = APIRouter(tags=["feed-generator"])


@router.get("/xrpc/app.bsky.feed.getFeed")
def get_feed(
    feed: str = Query(..., description="AT URI of the generator record (ignored in POC)"),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    db: Session = Depends(get_db),
):
    user_did = settings.default_user_did

    feeds = db.query(Feed).order_by(Feed.display_name).all()
    if not feeds:
        return {"feed": [], "cursor": None}

    # Shuffle so repeated calls don't always favour the same feed.
    # Phase 3 replaces this with Thompson Sampling rank.
    feed_order = feeds[:]
    random.shuffle(feed_order)

    slate: list[str] = []
    now = datetime.now(timezone.utc)

    for feed_row in feed_order:
        chunk = atproto_client.get_chunk(feed_row.feed_uri)
        if not chunk.posts:
            continue

        impression = Impression(
            user_did=user_did,
            feed_uri=feed_row.feed_uri,
            shown_at=now,
            posts_shown=len(chunk.posts),
        )
        db.add(impression)
        db.flush()  # assigns impression.id

        for position, post in enumerate(chunk.posts):
            db.add(ChunkPost(
                impression_id=impression.id,
                post_uri=post.uri,
                post_age_seconds=post.age_seconds,
                position=len(slate) + position,
            ))
            slate.append(post.uri)

    db.commit()

    feed_items = [{"post": uri} for uri in slate[:limit]]
    logger.info(
        "getFeed: user=%s feeds=%d posts=%d",
        user_did, len(feed_order), len(feed_items),
    )
    return {"feed": feed_items, "cursor": None}
