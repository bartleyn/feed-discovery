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
from bandit.thompson import rank_feeds, update_arm
from bot.intro_post import ensure_intro_post
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

    # Cursor encodes all feed URIs tried so far this session — skip them so
    # each scroll page shows fresh feed sources. Reset when cursor is absent.
    seen_uris: set[str] = set(cursor.split(CURSOR_SEP)) if cursor else set()

    feed_map = {f.feed_uri: f for f in feeds}

    slate: list[dict] = []

    # First page only: one intro post at the top describing what this feed is.
    if cursor is None:
        intro_uri = ensure_intro_post(db)
        if intro_uri:
            slate.append({"post": intro_uri, "feedContext": feed})
    now = datetime.now(timezone.utc)
    tried_uris: list[str] = []
    batch_limit = 4  # max batches per request to bound latency on pathological cases

    for _batch in range(batch_limit):
        # Re-rank each batch so empty-penalty updates from this request take effect
        ranked_uris = rank_feeds(user_did, feeds, db)
        candidates = [feed_map[uri] for uri in ranked_uris if uri not in seen_uris]
        if not candidates:
            break

        batch = candidates[:settings.feeds_per_slate]
        batch_uris = [f.feed_uri for f in batch]
        seen_uris.update(batch_uris)
        tried_uris.extend(batch_uris)

        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            chunks = list(pool.map(
                lambda f: atproto_client.get_chunk(f.feed_uri, user_did=user_did), batch
            ))

        for feed_row, chunk in zip(batch, chunks):
            if not chunk.posts:
                logger.debug("empty chunk: feed=%s user=%s", feed_row.feed_uri, user_did)
                update_arm(user_did, feed_row.feed_uri, reward=0.0, db=db)
                continue

            logger.debug(
                "chunk ok: feed=%s posts=%d uris=%s",
                feed_row.feed_uri,
                len(chunk.posts),
                [p.uri for p in chunk.posts[:3]],
            )

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
                logger.debug(
                    "section post: feed=%s uri=%s", feed_row.feed_uri, section_uri
                )
                slate.append({"post": section_uri})

        if slate:
            break  # got posts — stop fetching more batches

    db.commit()

    next_cursor = CURSOR_SEP.join(tried_uris) if tried_uris else None

    logger.info(
        "getFeed: user=%s batches=%d tried=%d posts=%d",
        user_did, _batch + 1, len(tried_uris), len(slate),
    )

    response: dict = {"feed": slate[:limit]}
    if next_cursor:
        response["cursor"] = next_cursor
    return response
