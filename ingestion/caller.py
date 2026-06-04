"""
AT Protocol feed caller.

Authenticates once at startup, then calls app.bsky.feed.getFeed
on behalf of the authenticated user. Returns a structured chunk
(list of posts) for a given feed URI.
"""

import logging
import threading
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


_CHUNK_CACHE_TTL = 120  # seconds — how long a fetched chunk stays fresh


class ATProtoClient:
    """Thin wrapper around the atproto Client, authenticated at init."""

    def __init__(self):
        self._client: Client | None = None
        self._did: str = ""
        self._chunk_cache: dict[str, tuple[FeedChunk, datetime]] = {}
        self._cache_lock = threading.Lock()

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

        # Request 2× what we need — the AppView filters posts after hydration
        # (deleted posts, blocked accounts, preference filters), so asking for
        # exactly n often returns fewer than n.
        fetch_limit = min(n * 2, 100)

        try:
            response = self._client.app.bsky.feed.get_feed({"feed": feed_uri, "limit": fetch_limit})
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

    def get_actor_likes(
        self, actor_did: str, since: datetime, until: datetime
    ) -> list[tuple[str, datetime]]:
        """
        Return (post_uri, indexed_at) for likes the actor made between since and until.

        Walks pages until indexed_at falls before `since` or we exhaust results.
        Returns at most 200 likes (safety cap).
        """
        if self._client is None:
            raise RuntimeError("ATProtoClient.login() must be called first")

        results: list[tuple[str, datetime]] = []
        cursor = None
        while len(results) < 200:
            try:
                resp = self._client.app.bsky.feed.get_actor_likes(
                    {"actor": actor_did, "limit": 50, **({"cursor": cursor} if cursor else {})}
                )
            except Exception as exc:
                logger.warning("get_actor_likes failed for %s: %s", actor_did, exc)
                break

            for item in resp.feed:
                try:
                    indexed_at = datetime.fromisoformat(
                        item.post.indexed_at.replace("Z", "+00:00")
                    )
                except Exception:
                    continue
                if indexed_at < since:
                    return results
                if indexed_at <= until:
                    results.append((item.post.uri, indexed_at))

            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break

        return results

    def get_reposted_by(
        self, post_uri: str, actor_did: str, before: datetime
    ) -> list[tuple[str, datetime]]:
        """
        Return (post_uri, indexed_at) if actor_did reposted post_uri before `before`.

        Checks up to the first 100 reposters (sufficient for POC).
        """
        if self._client is None:
            raise RuntimeError("ATProtoClient.login() must be called first")

        try:
            resp = self._client.app.bsky.feed.get_reposted_by(
                {"uri": post_uri, "limit": 100}
            )
        except Exception as exc:
            logger.warning("get_reposted_by failed for %s: %s", post_uri, exc)
            return []

        for profile in resp.reposted_by:
            if profile.did == actor_did:
                # atproto SDK doesn't expose indexed_at on repost profiles;
                # use `before` as a conservative timestamp.
                return [(post_uri, before)]
        return []


# Module-level singleton — initialised at app startup
atproto_client = ATProtoClient()
