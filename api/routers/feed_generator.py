"""
GET /xrpc/app.bsky.feed.getFeedSkeleton

Calls feeds in Thompson Sampling order, logs impressions + chunk_posts,
and returns a flat post slate to bsky.app.

Cursor encodes which feed URIs have already been shown in this session as a
pipe-delimited string. Each page pulls the next N highest-ranked feeds from
the remainder. When all feeds are exhausted the cursor is omitted, and
bsky.app will start a fresh session on next pull.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from api.auth import verify_service_jwt
from api.config import settings
from bandit.thompson import rank_feeds
from bot.section_poster import ensure_section_post
from ingestion import atproto_client
from store import get_db
from store.models import Feed, Impression, ChunkPost

logger = logging.getLogger(__name__)
router = APIRouter(tags=["feed-generator"])

CURSOR_SEP = "|"


@router.get("/xrpc/app.bsky.feed.getFeedSkeleton")
def get_feed(
    feed: str = Query(..., description="AT URI of the generator record (ignored in POC)"),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ")
        try:
            user_did = verify_service_jwt(token, settings.feed_generator_did)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
    else:
        user_did = settings.default_user_did

    feeds = db.query(Feed).all()
    if not feeds:
        return {"feed": []}

    # Cursor encodes only the previous page's feed URIs — exclude those to
    # prevent immediate back-to-back repeats, but allow everything else.
    prev_page: set[str] = set(cursor.split(CURSOR_SEP)) if cursor else set()

    # Rank all arms fresh, skip only last page's feeds
    ranked_uris = rank_feeds(user_did, [f.feed_uri for f in feeds], db)
    feed_map = {f.feed_uri: f for f in feeds}
    candidates = [feed_map[uri] for uri in ranked_uris if uri not in prev_page]
    page_feeds = candidates[:settings.feeds_per_slate]

    if not page_feeds:
        return {"feed": []}

    slate: list[dict] = []
    now = datetime.now(timezone.utc)

    # Fetch all feed chunks in parallel — each call is a separate HTTP round trip
    # to Bluesky's API, so serial execution multiplies latency by feeds_per_slate.
    with ThreadPoolExecutor(max_workers=len(page_feeds)) as pool:
        chunks = list(pool.map(
            lambda f: atproto_client.get_chunk(f.feed_uri, user_did=user_did), page_feeds
        ))

    for feed_row, chunk in zip(page_feeds, chunks):
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

        section_uri = ensure_section_post(feed_row, db)
        if section_uri:
            slate.append({"post": section_uri})

    db.commit()

    # Next cursor = this page's feeds only (prevents immediate repeats on next pull)
    next_cursor = CURSOR_SEP.join(f.feed_uri for f in page_feeds)

    logger.info(
        "getFeed: user=%s page_feeds=%d posts=%d",
        user_did, len(page_feeds), len(slate),
    )

    response: dict = {"feed": slate[:limit]}
    if next_cursor:
        response["cursor"] = next_cursor
    return response
