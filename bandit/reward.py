"""
Reward polling job — runs 30 minutes after each impression.

Polls AT Protocol for the user's interactions on served post URIs,
computes a scalar reward in [0, 1], and updates the arm's Beta parameters.

Scheduled via APScheduler, started in api/main.py lifespan.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from api.config import settings
from bandit.thompson import update_arm
from ingestion import atproto_client
from store import SessionLocal
from store.models import Impression, Interaction

logger = logging.getLogger(__name__)

ACTION_WEIGHTS = {
    "like": 1,
    "repost": 2,
    "reply": 3,
}


def _poll_impression(impression: Impression, db: Session) -> None:
    post_uris = {cp.post_uri for cp in impression.chunk_posts}
    cutoff = impression.shown_at.replace(tzinfo=timezone.utc) + timedelta(
        minutes=settings.reward_window_minutes
    )
    shown_at = impression.shown_at.replace(tzinfo=timezone.utc)

    # Likes
    for post_uri, occurred_at in atproto_client.get_actor_likes(
        impression.user_did, since=shown_at, until=cutoff
    ):
        if post_uri in post_uris:
            db.add(Interaction(
                impression_id=impression.id,
                post_uri=post_uri,
                user_did=impression.user_did,
                action="like",
                occurred_at=occurred_at,
            ))

    # Reposts — check each served post individually
    for post_uri in post_uris:
        for _, occurred_at in atproto_client.get_reposted_by(
            post_uri, actor_did=impression.user_did, before=cutoff
        ):
            db.add(Interaction(
                impression_id=impression.id,
                post_uri=post_uri,
                user_did=impression.user_did,
                action="repost",
                occurred_at=occurred_at,
            ))

    db.flush()


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
                _poll_impression(impression, db)
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
    scheduler = BackgroundScheduler()
    scheduler.add_job(process_due_impressions, "interval", minutes=1, id="reward_poll")
    scheduler.start()
    logger.info("Reward scheduler started (poll interval: 1 min)")
    return scheduler
