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

POSITIVE_ACTIONS = frozenset({"like", "repost", "reply", "quote", "request_more"})
NEGATIVE_ACTIONS = frozenset({"request_less"})
EXPOSURE_ACTIONS = frozenset({"seen"})


def reward_from_actions(actions) -> float | None:
    """Map the actions logged against one impression to a bandit outcome.

    1.0  — the user engaged with the chunk (or asked for more)
    0.0  — the user saw the chunk and did nothing, or asked for less
    None — no evidence the chunk was ever on screen: censored, not a failure.
    """
    seen_actions = set(actions)
    if seen_actions & POSITIVE_ACTIONS:
        return 1.0
    if seen_actions & NEGATIVE_ACTIONS:
        return 0.0
    if seen_actions & EXPOSURE_ACTIONS:
        return 0.0
    return None


def _compute_reward(impression: Impression, db: Session) -> float | None:
    interactions = (
        db.query(Interaction.action)
        .filter_by(impression_id=impression.id)
        .all()
    )
    return reward_from_actions(row.action for row in interactions)


def process_due_impressions() -> None:
    """Called by APScheduler every minute. Processes impressions whose reward window has closed.

    Every due impression is closed by stamping `rewarded_at`. Only those with
    evidence of exposure move an arm; censored ones keep `reward = NULL`.
    """
    db: Session = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=settings.reward_window_minutes
        )
        due = (
            db.query(Impression)
            .filter(
                Impression.rewarded_at.is_(None),
                Impression.shown_at <= cutoff.replace(tzinfo=None),
            )
            .all()
        )

        closed = rewarded = censored = 0
        for impression in due:
            try:
                reward = _compute_reward(impression, db)
                impression.reward = reward
                impression.rewarded_at = datetime.now(timezone.utc)
                db.commit()
                closed += 1
                if reward is None:
                    censored += 1
                    continue
                update_arm(impression.user_did, impression.feed_uri, reward, db)
                rewarded += 1
                logger.info(
                    "Rewarded impression %d (feed=%s reward=%.1f)",
                    impression.id, impression.feed_uri, reward,
                )
            except Exception as exc:
                db.rollback()
                logger.exception("Failed to process impression %d: %s", impression.id, exc)
        if closed:
            logger.info(
                "Reward pass: closed=%d rewarded=%d censored(unseen)=%d",
                closed, rewarded, censored,
            )
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
