"""
AT Protocol feed caller.

Authenticates once at startup, then calls app.bsky.feed.getFeed
on behalf of the authenticated user. Returns a structured chunk
(list of posts) for a given feed URI.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from atproto import Client
from api.config import settings

logger = logging.getLogger(__name__)


@dataclass
class PostSummary:
    uri: str
    cid: str
    author_did: str
    author_handle: str
    text: str
    created_at: datetime
    age_seconds: int
    like_count: int
    repost_count: int
    reply_count: int


@dataclass
class FeedChunk:
    feed_uri: str
    posts: list[PostSummary]
    fetched_at: datetime


class ATProtoClient:
    """Thin wrapper around the atproto Client, authenticated at init."""

    def __init__(self):
        self._client: Client | None = None
        self._did: str = ""

    def login(self) -> str:
        """Authenticate and return the resolved DID."""
        self._client = Client()
        profile = self._client.login(settings.atproto_handle, settings.atproto_password)
        self._did = profile.did
        logger.info("Logged in as %s (%s)", settings.atproto_handle, self._did)
        return self._did

    @property
    def did(self) -> str:
        return self._did

    def get_chunk(self, feed_uri: str, limit: int | None = None) -> FeedChunk:
        """
        Fetch the top `limit` posts from a feed generator.

        Args:
            feed_uri: Full AT URI, e.g. at://did:plc:.../app.bsky.feed.generator/whats-hot
            limit:    Number of posts to fetch. Defaults to settings.chunk_size.

        Returns:
            FeedChunk with parsed PostSummary objects.
        """
        if self._client is None:
            raise RuntimeError("ATProtoClient.login() must be called before get_chunk()")

        n = limit or settings.chunk_size
        now = datetime.now(timezone.utc)

        try:
            response = self._client.app.bsky.feed.get_feed({"feed": feed_uri, "limit": n})
        except Exception as exc:
            logger.warning("Failed to fetch feed %s: %s", feed_uri, exc)
            return FeedChunk(feed_uri=feed_uri, posts=[], fetched_at=now)

        posts: list[PostSummary] = []
        for item in response.feed[:n]:
            post = item.post
            record = post.record

            try:
                created_at = datetime.fromisoformat(
                    record.created_at.replace("Z", "+00:00")
                )
            except Exception:
                created_at = now

            age_seconds = int((now - created_at).total_seconds())
            text = getattr(record, "text", "") or ""

            counts = post.like_count or 0, post.repost_count or 0, post.reply_count or 0

            posts.append(PostSummary(
                uri=post.uri,
                cid=post.cid,
                author_did=post.author.did,
                author_handle=post.author.handle,
                text=text[:280],   # truncate for storage
                created_at=created_at,
                age_seconds=age_seconds,
                like_count=counts[0],
                repost_count=counts[1],
                reply_count=counts[2],
            ))

        return FeedChunk(feed_uri=feed_uri, posts=posts, fetched_at=now)


# Module-level singleton — initialised at app startup
atproto_client = ATProtoClient()
