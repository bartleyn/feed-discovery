"""Shared utilities for feed backfill and import scripts."""

import re
from datetime import datetime, timezone

_CURRENT_YEAR = datetime.now(timezone.utc).year


_NSFW_TERMS = {
    "nsfw", "18+", "adults only", "adult only", "explicit", "porn",
    "pornography", "hentai", "lewd", "erotic", "xxx",
}

_HARMFUL_TERMS = {
    "rape", "rapefic", "noncon", "non-con", "dubcon", "dub-con",
}


def compute_priority_boost(display_name: str, description: str) -> float:
    """
    Returns a multiplier (0–1] applied to the Thompson sample before ranking.

    0.0 — harmful content signals (rape, noncon) in name/description
    0.1 — explicitly dead: "archived"/"inactive", or NSFW feed
    0.3 — likely stale: a past calendar year appears in name/description
    1.0 — normal
    """
    text = f"{display_name} {description}".lower()

    words = set(re.findall(r"[\w-]+", text))
    if words & _HARMFUL_TERMS:
        return 0.0

    if words & _NSFW_TERMS:
        return 0.1

    # "was archived on <date>" is an explicit inactivity marker written into
    # the description by feed owners; treat it the same as "archived"/"inactive".
    if "archived" in text or "inactive" in text or "was archived on" in text:
        return 0.1

    years = {int(y) for y in re.findall(r'\b(20\d{2})\b', text)}
    if any(y < _CURRENT_YEAR for y in years):
        return 0.3

    return 1.0
