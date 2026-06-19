"""
GET /xrpc/app.bsky.feed.getFeedSkeleton

Calls feeds in Thompson Sampling order, logs impressions + chunk_posts,
and returns a flat post slate to bsky.app.

Cursor encodes which feed URIs have already been shown in this session as a
pipe-delimited string. Each page pulls the next N highest-ranked feeds from
the remainder. When all feeds are exhausted the cursor is omitted, and
bsky.app will start a fresh session on next pull.

Chunks are kept atomic: the response is assembled whole-chunk-at-a-time up to
`limit`, and a chunk that doesn't fit is deferred to the next page (excluded
from the cursor) rather than truncated, so a section post is never separated
from the posts above it.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice

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

    # (feed_row, posts) units collected this request, assembled into the
    # response at chunk boundaries below so the limit cut never separates a
    # section post from the posts above it.
    pending: list[tuple[Feed, list]] = []
    slate_post_uris: set[str] = set()

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

        # Collect section post URIs already in the DB so we can strip them from
        # chunks — some feeds pick up our bot's section posts (which mention
        # the feed name) and return them as regular posts in their skeleton.
        known_section_uris: set[str] = {
            f.section_post_uri for f in feeds if f.section_post_uri
        }

        for feed_row, chunk in zip(batch, chunks):
            # bsky.app silently drops URIs it has already rendered, so a post
            # repeated across chunks would punch a hole in the later chunk —
            # dedupe here instead. Also strip our own section posts.
            posts = [
                p for p in chunk.posts
                if p.uri not in slate_post_uris and p.uri not in known_section_uris
            ]

            if posts and feed_row.requires_filtering:
                posts = _filter_harmful_posts(posts)

            if not posts:
                logger.debug("empty chunk: feed=%s user=%s", feed_row.feed_uri, user_did)
                update_arm(user_did, feed_row.feed_uri, reward=0.0, db=db)
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

    # Assemble whole chunks up to `limit`, logging only what is actually
    # returned. A chunk that doesn't fit is deferred to the next page (and
    # dropped from the cursor so it gets served then) — truncating it would
    # cut off its section post and leave its posts rendering under the next
    # chunk's header.
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
        impression = Impression(
            user_did=user_did,
            feed_uri=feed_row.feed_uri,
            shown_at=now,
            posts_shown=len(posts),
        )
        db.add(impression)
        db.flush()

        for position, post in enumerate(posts):
            db.add(ChunkPost(
                impression_id=impression.id,
                post_uri=post.uri,
                post_age_seconds=post.age_seconds,
                position=len(slate) + position,
            ))
            slate.append({"post": post.uri, "feedContext": feed_row.feed_uri})

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
        user_did, _batch + 1, len(tried_uris), len(slate), len(deferred_uris),
    )

    response: dict = {"feed": slate}
    if next_cursor:
        response["cursor"] = next_cursor
    return response
