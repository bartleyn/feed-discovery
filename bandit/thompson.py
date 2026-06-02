"""
Thompson Sampling for feed ranking.

Each (user_did, feed_uri) pair is a bandit arm with Beta(alpha, beta) prior.
Arm state lives in the arm_state table. Arms are initialised to Beta(1,1)
(uniform prior) on first encounter.
"""

import numpy as np
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from store.models import ArmState


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


def rank_feeds(user_did: str, feed_uris: list[str], db: Session) -> list[str]:
    """Return feed_uris sorted by descending Thompson sample."""
    scores: list[tuple[str, float]] = []
    for uri in feed_uris:
        arm = get_or_init_arm(user_did, uri, db)
        scores.append((uri, float(np.random.beta(arm.alpha, arm.beta))))
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
