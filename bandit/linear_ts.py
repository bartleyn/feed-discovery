"""
Linear Thompson Sampling over feed embeddings, with an MMR slate.

Each feed is a vector x = [32-d feed-explorer embedding, z(log likes), 1] (feed_vectors).
A global head learns from every user's rewards; each user's head is shrunk towards
the global posterior mean plus their liked-feeds direction (user_priors). Heads are
stored as sufficient statistics (sum xx^T, sum r*x) in bandit_heads.
"""

import logging
import time
from datetime import datetime, timezone

import numpy as np
from sqlalchemy.orm import Session

from api.config import settings
from store.models import BanditHead, Feed, FeedVector, UserPrior

logger = logging.getLogger(__name__)

GLOBAL = "*"
EMB_DIM = 32
DIM = EMB_DIM + 2  # embedding, popularity, bias
POP = EMB_DIM

_rng = np.random.default_rng()
_snapshot: dict = {"loaded_at": 0.0, "vectors": {}}


# --- pure math ---

def posterior(xx: np.ndarray, rx: np.ndarray, lam: float, m0: np.ndarray):
    """Bayesian ridge posterior: precision A = lam*I + xx, mean = A^-1 (lam*m0 + rx)."""
    a = lam * np.eye(len(rx)) + xx
    return a, np.linalg.solve(a, lam * m0 + rx)


def sample_theta(a: np.ndarray, mean: np.ndarray, sigma: float, rng) -> np.ndarray:
    """Draw theta ~ N(mean, sigma^2 A^-1) via the Cholesky factor of the precision."""
    chol = np.linalg.cholesky(a)
    return mean + sigma * np.linalg.solve(chol.T, rng.standard_normal(len(mean)))


def global_prior_mean() -> np.ndarray:
    m0 = np.zeros(DIM)
    m0[POP] = settings.bandit_prior_scale * settings.bandit_pop_weight
    return m0


def user_theta(global_head, user_head, prior_vec, rng=None) -> np.ndarray:
    """A user's theta (sampled, or the posterior mean when rng is None); prior mean = global
    posterior mean + scaled liked-feeds direction."""
    _, global_mean = posterior(*global_head, settings.bandit_global_precision, global_prior_mean())
    m0 = global_mean + settings.bandit_prior_scale * prior_vec
    a, mean = posterior(*user_head, settings.bandit_user_precision, m0)
    return mean if rng is None else sample_theta(a, mean, settings.bandit_noise, rng)


def creator_of(feed_uri: str) -> str:
    return feed_uri.removeprefix("at://").split("/", 1)[0]


def mmr_order(uris, scores, emb, k, lam, explore_slots, rng) -> list[str]:
    """Greedy MMR on z-scored scores vs cosine similarity, one feed per creator; the rest by score."""
    n = len(uris)
    rel = (scores - scores.mean()) / (scores.std() + 1e-9)
    sim = emb @ emb.T
    picked: list[int] = []
    creators: set[str] = set()
    available = np.ones(n, dtype=bool)
    for slot in range(min(k, n)):
        if slot >= k - explore_slots:
            pool = np.flatnonzero(available)
            choice = int(rng.choice(pool)) if len(pool) else None
        else:
            penalty = sim[:, picked].max(axis=1) if picked else np.zeros(n)
            mmr = np.where(available, lam * rel - (1 - lam) * penalty, -np.inf)
            choice = int(np.argmax(mmr)) if np.isfinite(mmr).any() else None
        if choice is None:
            break
        picked.append(choice)
        creators.add(creator_of(uris[choice]))
        available[choice] = False
        available &= np.array([creator_of(u) not in creators for u in uris])
    rest = [i for i in np.argsort(-scores) if i not in set(picked)]
    return [uris[i] for i in picked + rest]


# --- DB layer ---

def _vectors(db: Session) -> dict[str, np.ndarray]:
    if time.monotonic() - _snapshot["loaded_at"] > settings.bandit_snapshot_ttl_seconds:
        _snapshot["vectors"] = {r.feed_uri: np.asarray(r.vec) for r in db.query(FeedVector).all()}
        _snapshot["loaded_at"] = time.monotonic()
    return _snapshot["vectors"]


def _head(db: Session, user_did: str, for_update: bool = False) -> BanditHead | None:
    q = db.query(BanditHead).filter_by(user_did=user_did)
    return (q.with_for_update() if for_update else q).one_or_none()


def _stats(head: BanditHead | None) -> tuple[np.ndarray, np.ndarray]:
    if head is None:
        return np.zeros((DIM, DIM)), np.zeros(DIM)
    return np.asarray(head.xx).reshape(DIM, DIM), np.asarray(head.rx)


def _scores(user_did: str | None, feeds: list[Feed], db: Session, rng) -> tuple[np.ndarray, np.ndarray]:
    """Feature matrix and boost-adjusted scores; user_did None scores from the global head alone."""
    vectors = _vectors(db)
    neutral = np.zeros(DIM)
    neutral[-1] = 1.0
    x = np.stack([vectors.get(f.feed_uri, neutral) for f in feeds])

    global_head = _stats(_head(db, GLOBAL))
    user_head, prior_vec = (np.zeros((DIM, DIM)), np.zeros(DIM)), np.zeros(DIM)
    if user_did is not None:
        user_head = _stats(_head(db, user_did))
        prior = db.get(UserPrior, user_did)
        if prior is not None:
            prior_vec = np.asarray(prior.vec)

    theta = user_theta(global_head, user_head, prior_vec, rng)
    boosts = np.array([f.priority_boost for f in feeds])
    return x, x @ theta + settings.bandit_prior_scale * np.log(boosts)


def rank_feeds(user_did: str | None, feeds: list[Feed], db: Session) -> list[str]:
    """MMR-ordered feed URIs from one Thompson draw; boost-0 feeds are never served."""
    feeds = [f for f in feeds if f.priority_boost > 0]
    if not feeds:
        return []
    x, scores = _scores(user_did, feeds, db, _rng)
    return mmr_order(
        [f.feed_uri for f in feeds], scores, x[:, :EMB_DIM], settings.feeds_per_slate,
        settings.mmr_lambda, settings.bandit_explore_slots, _rng,
    )


def mean_scores(user_did: str, feeds: list[Feed], db: Session) -> dict[str, float]:
    """Posterior-mean score per feed, for the status page."""
    feeds = [f for f in feeds if f.priority_boost > 0]
    if not feeds:
        return {}
    _, scores = _scores(user_did, feeds, db, rng=None)
    return {f.feed_uri: float(s) for f, s in zip(feeds, scores)}


def _accumulate(db: Session, user_did: str, x: np.ndarray, reward: float) -> None:
    head = _head(db, user_did, for_update=True)
    xx, rx = _stats(head)
    if head is None:
        head = BanditHead(user_did=user_did, pulls=0)
        db.add(head)
    head.xx = (xx + np.outer(x, x)).ravel().tolist()
    head.rx = (rx + reward * x).tolist()
    head.pulls += 1
    head.last_updated = datetime.now(timezone.utc)


def update(user_did: str, feed_uri: str, reward: float, db: Session) -> None:
    """Add one (feed, reward) observation to the user's head and the global head."""
    vec = db.get(FeedVector, feed_uri)
    if vec is None:
        logger.warning("no feed vector for %s; reward dropped", feed_uri)
        return
    x = np.asarray(vec.vec)
    _accumulate(db, GLOBAL, x, reward)
    _accumulate(db, user_did, x, reward)
    db.commit()
