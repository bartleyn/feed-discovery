"""
Reward job — closes impressions after the reward window and updates arm state.

Interactions are logged in real time by the sendInteractions endpoint.
This job simply aggregates whatever was logged and computes a scalar reward.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from api.config import settings
from bandit.thompson import update_arm
from store import SessionLocal
from store.models import Impression, Interaction

logger = logging.getLogger(__name__)

ACTION_WEIGHTS = {
    "like":         1,
    "repost":       2,
    "reply":        3,
    "quote":        2,
    "request_more": 5,
    "request_less": 0,
}


def _compute_reward(impression: Impression, db: Session) -> float:
    interactions = (
        db.query(Interaction)
        .filter_by(impression_id=impression.id)
        .all()
    )
    raw = sum(ACTION_WEIGHTS.get(i.action, 0) for i in interactions)
    # Normalise: max plausible score = posts_shown * max_weight (3) * some buffer (10)
    return min(raw / max(impression.posts_shown, 1) / 10, 1.0)


def process_due_impressions() -> None:
    """Called by APScheduler every minute. Processes impressions whose reward window has closed."""
    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=settings.reward_window_minutes
        )
        due = (
            db.query(Impression)
            .filter(
                Impression.reward.is_(None),
                Impression.shown_at <= cutoff.replace(tzinfo=None),
            )
            .all()
        )

        for impression in due:
            try:
                reward = _compute_reward(impression, db)
                impression.reward = reward
                impression.rewarded_at = datetime.now(timezone.utc)
                db.commit()
                update_arm(impression.user_did, impression.feed_uri, reward, db)
                logger.info(
                    "Rewarded impression %d (feed=%s reward=%.3f)",
                    impression.id, impression.feed_uri, reward,
                )
            except Exception as exc:
                db.rollback()
                logger.exception("Failed to process impression %d: %s", impression.id, exc)
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    from api.config import settings
    from ingestion.feed_health import check_all_feeds
    from ingestion.feed_likes import refresh_like_counts

    scheduler = BackgroundScheduler()
    scheduler.add_job(process_due_impressions, "interval", minutes=1, id="reward_poll")

    if settings.feed_health_interval_hours > 0:
        scheduler.add_job(
            check_all_feeds,
            "interval",
            hours=settings.feed_health_interval_hours,
            id="feed_health",
            next_run_time=datetime.now(timezone.utc),  # run once at startup too
        )
        logger.info(
            "Feed health scheduler started (interval: %dh)",
            settings.feed_health_interval_hours,
        )

    if settings.feed_likes_interval_hours > 0:
        scheduler.add_job(
            refresh_like_counts,
            "interval",
            hours=settings.feed_likes_interval_hours,
            id="feed_likes",
            next_run_time=datetime.now(timezone.utc),  # run once at startup too
        )
        logger.info(
            "Feed likes scheduler started (interval: %dh)",
            settings.feed_likes_interval_hours,
        )

    scheduler.start()
    logger.info("Reward scheduler started (poll interval: 1 min)")
    return scheduler
