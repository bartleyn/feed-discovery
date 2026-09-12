"""
AT Protocol feed caller.

Authenticates once at startup, then calls app.bsky.feed.getFeed
on behalf of the authenticated user. Returns a structured chunk
(list of posts) for a given feed URI.
"""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
from atproto import Client
from atproto_identity.did.resolver import DidResolver

from api.config import settings
from api.service_auth import create_service_jwt

logger = logging.getLogger(__name__)


@dataclass
class PostSummary:
    uri: str
    age_seconds: int = 0


@dataclass
class FeedChunk:
    feed_uri: str
    posts: list[PostSummary]
    fetched_at: datetime


_CHUNK_CACHE_TTL = 120  # seconds — how long a fetched chunk stays fresh

_did_resolver = DidResolver()


class ATProtoClient:
    """Thin wrapper around the atproto Client, authenticated at init."""

    def __init__(self):
        self._client: Client | None = None
        self._did: str = ""
        self._chunk_cache: dict[str, tuple[FeedChunk, datetime]] = {}
        self._cache_lock = threading.Lock()
        # (feed_uri) -> (generator_did, skeleton_endpoint_url)
        self._generator_cache: dict[str, tuple[str, str]] = {}
        # Persistent pooled client — reused across get_chunk calls so repeat
        # requests to the same feed generator skip the TCP/TLS handshake.
        # Granular, tight timeouts: a dead upstream feed is abandoned in a few
        # seconds rather than the old blanket 10s, keeping us under bsky.app's
        # getFeedSkeleton budget. The per-request deadline in feed_generator.py
        # bounds the batch as a whole; these bound each individual call.
        self._http = httpx.Client(
            timeout=httpx.Timeout(connect=2.0, read=2.5, write=2.5, pool=2.5),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=40),
        )

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

    def _resolve_generator(self, feed_uri: str) -> tuple[str, str]:
        """Return (generator_did, skeleton_url) for a feed URI, with caching."""
        if feed_uri in self._generator_cache:
            return self._generator_cache[feed_uri]

        resp = self._client.app.bsky.feed.get_feed_generator({"feed": feed_uri})
        generator_did = resp.view.did

        did_doc = _did_resolver.resolve_without_validation(generator_did)
        if not did_doc:
            raise ValueError(f"Could not resolve DID document for {generator_did}")

        endpoint = None
        for svc in did_doc.get("service", []):
            if svc.get("id") in ("#bsky_fg", f"{generator_did}#bsky_fg"):
                endpoint = svc["serviceEndpoint"]
                break

        if not endpoint:
            raise ValueError(f"No #bsky_fg service in DID doc for {generator_did}")

        skeleton_url = f"{endpoint.rstrip('/')}/xrpc/app.bsky.feed.getFeedSkeleton"
        self._generator_cache[feed_uri] = (generator_did, skeleton_url)
        return generator_did, skeleton_url

    def get_chunk(self, feed_uri: str, limit: int | None = None, user_did: str | None = None) -> FeedChunk:
        """Fetch posts from a feed generator by calling getFeedSkeleton directly.

        Calls the generator's own endpoint rather than routing through the AppView,
        so personalized feeds receive the requesting user's DID (via JWT sub claim)
        instead of the service account's.
        """
        if self._client is None:
            raise RuntimeError("ATProtoClient.login() must be called before get_chunk()")

        n = limit or settings.chunk_size
        now = datetime.now(timezone.utc)

        try:
            generator_did, skeleton_url = self._resolve_generator(feed_uri)
            token = create_service_jwt(aud=generator_did, sub=user_did)
            response = self._http.get(
                skeleton_url,
                params={"feed": feed_uri, "limit": n},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            items = response.json().get("feed", [])
        except Exception as exc:
            logger.warning("Failed to fetch skeleton for %s: %s", feed_uri, exc)
            return FeedChunk(feed_uri=feed_uri, posts=[], fetched_at=now)

        posts = [PostSummary(uri=item["post"]) for item in items[:n] if "post" in item]
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
