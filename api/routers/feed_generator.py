"""
GET /xrpc/app.bsky.feed.getFeed

Calls all registered feeds in Thompson Sampling order, logs impressions +
chunk_posts, and returns a flat post slate to bsky.app.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from api.config import settings
from bandit.thompson import rank_feeds
from ingestion import atproto_client
from store import get_db
from store.models import Feed, Impression, ChunkPost

logger = logging.getLogger(__name__)
router = APIRouter(tags=["feed-generator"])


@router.get("/xrpc/app.bsky.feed.getFeedSkeleton")
def get_feed(
    feed: str = Query(..., description="AT URI of the generator record (ignored in POC)"),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    db: Session = Depends(get_db),
):
    user_did = settings.default_user_did

    feeds = db.query(Feed).order_by(Feed.display_name).all()
    if not feeds:
        return {"feed": []}

    # Rank all arms (cheap — just beta samples), then fetch only the top N
    ranked_uris = rank_feeds(user_did, [f.feed_uri for f in feeds], db)
    feed_map = {f.feed_uri: f for f in feeds}
    top_feeds = [feed_map[uri] for uri in ranked_uris[:settings.feeds_per_slate]]

    slate: list[dict] = []
    now = datetime.now(timezone.utc)

    for feed_row in top_feeds:
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
        db.flush()

        for position, post in enumerate(chunk.posts):
            db.add(ChunkPost(
                impression_id=impression.id,
                post_uri=post.uri,
                post_age_seconds=post.age_seconds,
                position=len(slate) + position,
            ))
            slate.append({"post": post.uri, "feedContext": feed_row.feed_uri})

    db.commit()

    logger.info(
        "getFeed: user=%s top_feeds=%d posts=%d",
        user_did, len(top_feeds), len(slate),
    )
    return {"feed": slate[:limit]}
