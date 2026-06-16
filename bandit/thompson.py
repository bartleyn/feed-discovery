"""
Thompson Sampling for feed ranking.

Each (user_did, feed_uri) pair is a bandit arm with Beta(alpha, beta) prior.
Arm state lives in the arm_state table. Arms are initialised to Beta(1,1)
(uniform prior) on first encounter.
"""

import numpy as np
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from store.models import ArmState, Feed


def get_or_init_arm(user_did: str, feed_uri: str, db: Session) -> ArmState:
    arm = (
        db.query(ArmState)
        .filter_by(user_did=user_did, feed_uri=feed_uri)
        .first()
    )
    if arm is None:
        arm = ArmState(
            user_did=user_did,
            feed_uri=feed_uri,
            alpha=1.0,
            beta=1.0,
            pulls=0,
        )
        db.add(arm)
        db.flush()
    return arm


def rank_feeds(user_did: str, feeds: list[Feed], db: Session) -> list[str]:
    """Return feed URIs sorted by descending Thompson sample × priority_boost."""
    feed_uris = [feed.feed_uri for feed in feeds]
    arms = (
        db.query(ArmState)
        .filter(ArmState.user_did == user_did, ArmState.feed_uri.in_(feed_uris))
        .all()
    )
    arm_map = {arm.feed_uri: arm for arm in arms}

    missing = [feed for feed in feeds if feed.feed_uri not in arm_map]
    if missing:
        new_arms = [
            ArmState(user_did=user_did, feed_uri=feed.feed_uri, alpha=1.0, beta=1.0, pulls=0)
            for feed in missing
        ]
        db.add_all(new_arms)
        db.flush()
        arm_map.update({arm.feed_uri: arm for arm in new_arms})

    scores: list[tuple[str, float]] = []
    for feed in feeds:
        arm = arm_map[feed.feed_uri]
        sample = float(np.random.beta(arm.alpha, arm.beta)) * feed.priority_boost
        scores.append((feed.feed_uri, sample))
    scores.sort(key=lambda x: -x[1])
    return [uri for uri, _ in scores]


def update_arm(user_did: str, feed_uri: str, reward: float, db: Session) -> None:
    """Bayesian update: alpha += reward, beta += (1 - reward)."""
    arm = get_or_init_arm(user_did, feed_uri, db)
    arm.alpha += reward
    arm.beta += 1.0 - reward
    arm.pulls += 1
    arm.last_updated = datetime.now(timezone.utc)
    db.commit()
