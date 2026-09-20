"""
GET /xrpc/app.bsky.feed.getFeedSkeleton

Calls feeds in Thompson Sampling order, logs impressions + chunk_posts,
and returns a flat post slate to bsky.app.

Chunks are kept atomic: the response is assembled whole-chunk-at-a-time up to
`limit`, and a chunk that doesn't fit is deferred to the next page (excluded
from the cursor) rather than truncated, so a section post is never separated
from the posts above it.
"""

import logging
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from itertools import islice

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from api.auth import LXM_GET_FEED_SKELETON, bearer_token, verify_service_jwt
from api.config import settings
from bandit.thompson import rank_feeds, rank_feeds_anonymous
from bot.intro_post import ensure_intro_post
from bot.section_poster import ensure_section_post
from ingestion import atproto_client
from ingestion.caller import FeedChunk
from ingestion.feed_health import is_quarantined, record_fetch_outcome
from store import get_db
from store.models import Feed, Impression, ChunkPost

logger = logging.getLogger(__name__)
router = APIRouter(tags=["feed-generator"])

CURSOR_SEP = "|"

# Separates the source feed URI from the impression id inside feedContext.
CONTEXT_SEP = "|"

_HARMFUL_TERMS = {"rape", "noncon", "non-con", "dubcon", "dub-con"}


def _batched(it, n):
    it = iter(it)
    while batch := list(islice(it, n)):
        yield batch


def _filter_harmful_posts(posts):
    """Hydrate posts via getPosts and drop any containing harmful terms.

    Only called for feeds with requires_filtering=True. Adds one getPosts
    round-trip per 25 posts but is skipped entirely for clean feeds.
    """
    safe = []
    for uri_batch in _batched([p.uri for p in posts], 25):
        try:
            resp = atproto_client._client.app.bsky.feed.get_posts({"uris": uri_batch})
            safe_uris = {
                p.uri for p in resp.posts
                if not any(t in (p.record.text or "").lower() for t in _HARMFUL_TERMS)
            }
        except Exception as exc:
            logger.warning("getPosts failed during filtering: %s", exc)
            safe_uris = set(uri_batch)  # fail open — better to show than to drop everything
        safe.extend(p for p in posts if p.uri in safe_uris)
    return safe


@router.get("/xrpc/app.bsky.feed.getFeedSkeleton")
def get_feed(
    feed: str = Query(..., description="AT URI of the generator record (ignored in POC)"),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    authorization: str | None = Header(None),
    db: Session = Depends(get_db),
):
    # if no token go anonymous
    token = bearer_token(authorization)
    user_did: str | None = None
    if token is not None:
        try:
            user_did = verify_service_jwt(token, settings.feed_generator_did, LXM_GET_FEED_SKELETON)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
    anonymous = user_did is None

    now = datetime.now(timezone.utc)
    all_feeds = db.query(Feed).all()
    # Feeds that keep returning nothing sit out for a retry window instead of being ranked and
    # penalised on every request.
    feeds = [
        f for f in all_feeds
        if not is_quarantined(
            f, now, settings.feed_health_failure_threshold, settings.feed_health_retry_hours
        )
    ]
    if not feeds:
        return {"feed": []}

    # Cursor encodes all feed URIs tried so far this session
    seen_uris: set[str] = set(cursor.split(CURSOR_SEP)) if cursor else set()

    feed_map = {f.feed_uri: f for f in feeds}

    slate: list[dict] = []

    # First page only: add intro post
    if cursor is None:
        intro_uri = ensure_intro_post(db)
        if intro_uri:
            slate.append({"post": intro_uri, "feedContext": feed})
    # One reqId per getFeedSkeleton call. Stamped on every impression created
    req_id = uuid.uuid4().hex
    tried_uris: list[str] = []
    batch_limit = 4  # max batches per request to bound latency on pathological cases

    pending: list[tuple[Feed, list]] = []
    slate_post_uris: set[str] = set()

    for _batch in range(batch_limit):
        # Fresh Thompson draw per batch; the previous batch's failures have
        # already been recorded on the feed rows, so quarantine applies.
        ranked_uris = (
            rank_feeds_anonymous(feeds) if anonymous else rank_feeds(user_did, feeds, db)
        )
        candidates = [feed_map[uri] for uri in ranked_uris if uri not in seen_uris]
        if not candidates:
            break

        batch = candidates[:settings.feeds_per_slate]
        batch_uris = [f.feed_uri for f in batch]
        seen_uris.update(batch_uris)
        tried_uris.extend(batch_uris)

        # Fetch feeds but don't wait too long, abandon them if too long
        pool = ThreadPoolExecutor(max_workers=len(batch))
        futures = {
            pool.submit(atproto_client.get_chunk, f.feed_uri, user_did=user_did): f
            for f in batch
        }
        deadline = time.monotonic() + settings.upstream_deadline_seconds
        pending_futs = set(futures)
        chunks_by_uri: dict[str, FeedChunk] = {}
        while pending_futs:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending_futs = wait(
                pending_futs, timeout=remaining, return_when=FIRST_COMPLETED
            )
            for fut in done:
                feed_row = futures[fut]
                try:
                    chunks_by_uri[feed_row.feed_uri] = fut.result()
                except Exception as exc:
                    logger.warning("get_chunk failed for %s: %s", feed_row.feed_uri, exc)
        if pending_futs:
            logger.info(
                "upstream deadline hit: %d/%d feeds abandoned this batch",
                len(pending_futs), len(batch),
            )
        pool.shutdown(wait=False, cancel_futures=True)

        chunks = [
            chunks_by_uri.get(
                f.feed_uri, FeedChunk(feed_uri=f.feed_uri, posts=[], fetched_at=now)
            )
            for f in batch
        ]

        # Strip the section posts that some feeds pick up from this feed
        known_section_uris: set[str] = {
            f.section_post_uri for f in all_feeds if f.section_post_uri
        }

        for feed_row, chunk in zip(batch, chunks):
            record_fetch_outcome(feed_row, bool(chunk.posts), now)

            # bsky.app silently drops URIs it has already rendered, so dedupe here
            posts = [
                p for p in chunk.posts
                if p.uri not in slate_post_uris and p.uri not in known_section_uris
            ]

            if posts and feed_row.requires_filtering:
                posts = _filter_harmful_posts(posts)

            if not posts:
                logger.debug("empty chunk: feed=%s user=%s", feed_row.feed_uri, user_did)
                continue

            logger.debug(
                "chunk ok: feed=%s posts=%d uris=%s",
                feed_row.feed_uri,
                len(posts),
                [p.uri for p in posts[:3]],
            )
            slate_post_uris.update(p.uri for p in posts)
            pending.append((feed_row, posts))

        if pending:
            break  # got posts — stop fetching more batches

    # A chunk that doesn't fit is deferred to the next page
    deferred_uris: set[str] = set()
    chunks_included = 0
    for idx, (feed_row, posts) in enumerate(pending):
        section_uri = ensure_section_post(feed_row, db)
        size = len(posts) + (1 if section_uri else 0)

        if len(slate) + size > limit:
            if chunks_included:
                deferred_uris = {f.feed_uri for f, _ in pending[idx:]}
                break
            # No chunk served yet and this one alone exceeds the limit
            # (pathologically small `limit`): serve the posts that fit
            # rather than returning an empty page.
            posts = posts[:max(0, limit - len(slate))]
            section_uri = None
            if not posts:
                deferred_uris = {f.feed_uri for f, _ in pending[idx:]}
                break
            deferred_uris = {f.feed_uri for f, _ in pending[idx + 1:]}

        chunks_included += 1
        impression: Impression | None = None
        if not anonymous:
            impression = Impression(
                user_did=user_did,
                feed_uri=feed_row.feed_uri,
                shown_at=now,
                posts_shown=len(posts),
                req_id=req_id,
            )
            db.add(impression)
            db.flush()

        for position, post in enumerate(posts):
            if impression is not None:
                db.add(ChunkPost(
                    impression_id=impression.id,
                    post_uri=post.uri,
                    post_age_seconds=post.age_seconds,
                    position=len(slate) + position,
                ))
            # feedContext = "<feed_uri>|<impression_id>" so an interaction maps
            # back to the exact impression regardless of client reordering.
            # Anonymous slates carry the bare feed_uri: there is no impression.
            context = (
                f"{feed_row.feed_uri}{CONTEXT_SEP}{impression.id}"
                if impression is not None else feed_row.feed_uri
            )
            slate.append({"post": post.uri, "feedContext": context})

        if section_uri:
            logger.debug(
                "section post: feed=%s uri=%s", feed_row.feed_uri, section_uri
            )
            slate.append({"post": section_uri})

        if deferred_uris:
            break

    if deferred_uris:
        tried_uris = [u for u in tried_uris if u not in deferred_uris]

    db.commit()

    next_cursor = CURSOR_SEP.join(tried_uris) if tried_uris else None

    logger.info(
        "getFeed: user=%s batches=%d tried=%d posts=%d deferred=%d",
        "anonymous" if anonymous else user_did,
        _batch + 1, len(tried_uris), len(slate), len(deferred_uris),
    )

    response: dict = {"feed": slate, "reqId": req_id}
    if next_cursor:
        response["cursor"] = next_cursor
    return response
