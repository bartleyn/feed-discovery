"""
Periodic feed health checker.

Fetches 1 post from each registered feed to verify it is still active.
Tracks consecutive failures on the Feed row; logs a warning when a feed
crosses the configured threshold.
"""

import logging
from datetime import datetime, timedelta, timezone

from store import SessionLocal
from store.models import Feed

logger = logging.getLogger(__name__)


def record_fetch_outcome(feed: Feed, got_posts: bool, now: datetime) -> None:
    """Update a feed's liveness counters after any attempt to fetch from it.

    """
    feed.last_checked_at = now
    if got_posts:
        feed.consecutive_failures = 0
    else:
        feed.consecutive_failures = (feed.consecutive_failures or 0) + 1


def is_quarantined(feed: Feed, now: datetime, threshold: int, retry_hours: float) -> bool:
    """True when the feed should be skipped for ranking.

    """
    failures = feed.consecutive_failures or 0
    if failures < threshold:
        return False
    last = feed.last_checked_at
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last < timedelta(hours=retry_hours)


def check_all_feeds() -> None:
    """Called by APScheduler. Checks every feed and updates health columns."""
    # Import here to avoid a circular import at module load time
    from api.config import settings
    from ingestion import atproto_client

    db = SessionLocal()
    try:
        feeds = db.query(Feed).all()
        logger.info("Feed health check: checking %d feeds", len(feeds))

        for feed in feeds:
            now = datetime.now(timezone.utc)
            try:
                chunk = atproto_client.get_chunk(
                    feed.feed_uri,
                    limit=1,
                    user_did=settings.default_user_did or None,
                )
                healthy = bool(chunk.posts)
            except Exception as exc:
                logger.warning("Health check error for %s: %s", feed.feed_uri, exc)
                healthy = False

            if healthy and (feed.consecutive_failures or 0) > 0:
                logger.info(
                    "Feed recovered: %s (was %d consecutive failures)",
                    feed.display_name,
                    feed.consecutive_failures,
                )
            record_fetch_outcome(feed, healthy, now)
            if not healthy:
                if feed.consecutive_failures >= settings.feed_health_failure_threshold:
                    logger.warning(
                        "Feed unhealthy: %s (%d consecutive empty/error responses)",
                        feed.display_name,
                        feed.consecutive_failures,
                    )
                else:
                    logger.debug(
                        "Feed returned no posts: %s (failure %d/%d)",
                        feed.display_name,
                        feed.consecutive_failures,
                        settings.feed_health_failure_threshold,
                    )

            db.add(feed)

        db.commit()
        logger.info("Feed health check complete")
    except Exception as exc:
        db.rollback()
        logger.exception("Feed health check failed: %s", exc)
    finally:
        db.close()
