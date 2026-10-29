import numpy as np
import pytest

from api.config import settings
from bandit.linear_ts import (
    DIM, POP, creator_of, global_prior_mean, mmr_order, posterior, sample_theta, user_theta,
)

ZERO_HEAD = (np.zeros((DIM, DIM)), np.zeros(DIM))


def _unit(i: int) -> np.ndarray:
    x = np.zeros(DIM)
    x[i] = 1.0
    return x


# --- posterior / sampling ---

def test_posterior_without_data_is_the_prior():
    m0 = np.arange(DIM, dtype=float)
    _, mean = posterior(*ZERO_HEAD, lam=5.0, m0=m0)
    assert np.allclose(mean, m0)


def test_samples_centre_on_the_posterior_mean():
    rng = np.random.default_rng(0)
    a, mean = posterior(*ZERO_HEAD, lam=4.0, m0=np.ones(DIM))
    draws = np.stack([sample_theta(a, mean, sigma=1.0, rng=rng) for _ in range(4000)])
    assert np.allclose(draws.mean(0), mean, atol=0.05)
    assert np.allclose(draws.std(0), 0.5, atol=0.05)  # sigma / sqrt(lam)


def test_cold_user_mean_is_global_prior_plus_liked_direction():
    prior_vec = _unit(0)
    theta = user_theta(ZERO_HEAD, ZERO_HEAD, prior_vec)
    assert np.allclose(theta, global_prior_mean() + settings.bandit_prior_scale * prior_vec)
    assert theta[POP] > 0


def test_rewards_move_the_user_towards_what_they_engage_with():
    liked, ignored = _unit(1), _unit(2)
    xx = 30 * (np.outer(liked, liked) + np.outer(ignored, ignored))
    rx = 30 * liked
    theta = user_theta(ZERO_HEAD, (xx, rx), np.zeros(DIM))
    assert liked @ theta > 0.5 > ignored @ theta


# --- MMR slate ---

def _slate_inputs():
    uris = [
        "at://did:plc:a/app.bsky.feed.generator/1",
        "at://did:plc:a/app.bsky.feed.generator/2",   # same creator as 1
        "at://did:plc:b/app.bsky.feed.generator/3",   # near-duplicate of 1
        "at://did:plc:c/app.bsky.feed.generator/4",
        "at://did:plc:d/app.bsky.feed.generator/5",
    ]
    emb = np.array([[1, 0, 0], [1, 0, 0], [0.99, 0.14, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    scores = np.array([1.0, 0.95, 0.9, 0.5, 0.1])
    return uris, scores, emb


def test_creator_of_reads_the_did():
    assert creator_of("at://did:plc:a/app.bsky.feed.generator/1") == "did:plc:a"


def test_mmr_caps_creators_and_skips_near_duplicates():
    uris, scores, emb = _slate_inputs()
    order = mmr_order(uris, scores, emb, k=3, lam=0.3, explore_slots=0, rng=np.random.default_rng(0))
    assert order[:2] == [uris[0], uris[3]]  # skips same-creator 2 and near-duplicate 3
    assert uris[1] not in order[:3]
    assert sorted(order) == sorted(uris)


def test_mmr_with_lambda_one_is_score_order_within_creator_cap():
    uris, scores, emb = _slate_inputs()
    order = mmr_order(uris, scores, emb, k=3, lam=1.0, explore_slots=0, rng=np.random.default_rng(0))
    assert order[:3] == [uris[0], uris[2], uris[3]]


@pytest.mark.parametrize("seed", range(5))
def test_explore_slot_picks_an_eligible_feed(seed):
    uris, scores, emb = _slate_inputs()
    order = mmr_order(uris, scores, emb, k=3, lam=0.7, explore_slots=1, rng=np.random.default_rng(seed))
    top = order[:3]
    assert len(set(top)) == 3
    assert len({creator_of(u) for u in top}) == 3
