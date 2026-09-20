from datetime import datetime, timedelta, timezone

import pytest

from bandit.reward import reward_from_actions
from ingestion.feed_health import is_quarantined, record_fetch_outcome
from store.models import Feed


# --- reward_from_actions ---

@pytest.mark.parametrize("actions", [
    ["like"], ["repost"], ["reply"], ["quote"], ["request_more"],
    ["seen", "like"], ["seen", "seen", "repost"],
])
def test_engagement_is_a_success(actions):
    assert reward_from_actions(actions) == 1.0


def test_seen_without_engagement_is_a_failure():
    assert reward_from_actions(["seen"]) == 0.0
    assert reward_from_actions(["seen", "seen", "seen"]) == 0.0


def test_request_less_is_a_failure():
    assert reward_from_actions(["request_less"]) == 0.0
    assert reward_from_actions(["seen", "request_less"]) == 0.0


def test_engagement_beats_request_less():
    """Mixed signals on one chunk: a like is a stronger statement than a dismiss."""
    assert reward_from_actions(["request_less", "like"]) == 1.0


def test_never_seen_is_censored():
    assert reward_from_actions([]) is None


def test_unknown_actions_ignored():
    assert reward_from_actions(["profile_click"]) is None
    assert reward_from_actions(["profile_click", "seen"]) == 0.0


def test_accepts_generator():
    assert reward_from_actions(a for a in ["seen", "like"]) == 1.0


# --- liveness bookkeeping / quarantine ---

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
THRESHOLD = 3
RETRY_H = 24.0


def _feed(failures=0, last_checked=None) -> Feed:
    f = Feed()
    f.feed_uri = "at://did:plc:x/app.bsky.feed.generator/f"
    f.display_name = "F"
    f.consecutive_failures = failures
    f.last_checked_at = last_checked
    return f


def test_record_success_resets_counter():
    f = _feed(failures=5)
    record_fetch_outcome(f, True, NOW)
    assert f.consecutive_failures == 0
    assert f.last_checked_at == NOW


def test_record_failure_increments():
    f = _feed(failures=None)  # column default may not be applied on a bare instance
    record_fetch_outcome(f, False, NOW)
    assert f.consecutive_failures == 1
    record_fetch_outcome(f, False, NOW)
    assert f.consecutive_failures == 2


def test_under_threshold_not_quarantined():
    f = _feed(failures=THRESHOLD - 1, last_checked=NOW)
    assert not is_quarantined(f, NOW, THRESHOLD, RETRY_H)


def test_at_threshold_quarantined_within_window():
    f = _feed(failures=THRESHOLD, last_checked=NOW - timedelta(hours=1))
    assert is_quarantined(f, NOW, THRESHOLD, RETRY_H)


def test_quarantine_lifts_after_retry_window():
    f = _feed(failures=THRESHOLD, last_checked=NOW - timedelta(hours=RETRY_H + 1))
    assert not is_quarantined(f, NOW, THRESHOLD, RETRY_H)


def test_naive_timestamp_from_db_is_treated_as_utc():
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    f = _feed(failures=THRESHOLD, last_checked=naive)
    assert is_quarantined(f, NOW, THRESHOLD, RETRY_H)


def test_never_checked_is_not_quarantined():
    f = _feed(failures=THRESHOLD, last_checked=None)
    assert not is_quarantined(f, NOW, THRESHOLD, RETRY_H)
