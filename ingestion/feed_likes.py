"""
Periodic like-count refresh for the served feed registry.

Reads each feed's lifetime `likeCount` from the public AppView
(getFeedGenerators, batches of 25, no auth) and writes it onto the Feed row.
Mirrors ingestion.feed_health — self-contained, called by APScheduler.

The wider 119k discovery pool is refreshed offline by
scripts/fetch_feed_likes.py; this in-process job keeps only the served set
(the few hundred rows in `feeds`) current for sorting/clustering.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from store import SessionLocal
from store.models import Feed

logger = logging.getLogger(__name__)

APPVIEW = "https://public.api.bsky.app"
UA = "feedmoreseemore/feed-likes-refresh"
BATCH = 25


def _fetch_like_counts(uris: list[str]) -> dict[str, int]:
    """uri -> likeCount. Feeds the AppView can't resolve are simply absent."""
    out: dict[str, int] = {}
    for i in range(0, len(uris), BATCH):
        chunk = uris[i:i + BATCH]
        qs = urllib.parse.urlencode([("feeds", u) for u in chunk])
        url = f"{APPVIEW}/xrpc/app.bsky.feed.getFeedGenerators?{qs}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("Likes refresh: batch at %d failed: %s", i, exc)
            continue
        for f in data.get("feeds", []):
            out[f["uri"]] = f.get("likeCount", 0)
    return out


def refresh_like_counts() -> None:
    """Called by APScheduler. Refreshes like_count on every served feed."""
    db = SessionLocal()
    try:
        feeds = db.query(Feed).all()
        logger.info("Likes refresh: fetching like counts for %d feeds", len(feeds))

        counts = _fetch_like_counts([f.feed_uri for f in feeds])
        now = datetime.now(timezone.utc)

        resolved = 0
        for feed in feeds:
            lc = counts.get(feed.feed_uri)
            if lc is None:
                continue  # unresolved/deleted — leave prior value intact
            feed.like_count = lc
            feed.likes_fetched_at = now
            resolved += 1

        db.commit()
        logger.info(
            "Likes refresh complete: updated %d/%d feeds", resolved, len(feeds)
        )
    except Exception as exc:
        db.rollback()
        logger.exception("Likes refresh failed: %s", exc)
    finally:
        db.close()
