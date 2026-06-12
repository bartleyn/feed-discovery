"""
One-off intro post that appears at the top of the feed on every first-page load.

Created once by the bot account and stored in bot_config.intro_post_uri.
Content is intentionally simple — just a brief note that this is the
discovery feed, not a per-feed section header.
"""

import logging
import time

from atproto import Client

from api.config import settings
from store.models import BotConfig

logger = logging.getLogger(__name__)

_INTRO_TEXT = (
    "✨ Feed Discovery\n\n"
    "What follows is a ranked slate of chunks — a few posts each from "
    "different feeds you might enjoy. Interact with what catches your eye "
    "and the ranker will learn which feeds to surface more.\n\n"
    "Note: NSFW feeds & posts are possible, and some info may render out of order."
)

_bot_client: Client | None = None


def _get_bot_client() -> Client:
    global _bot_client
    if _bot_client is None:
        if not settings.bot_handle or not settings.bot_password:
            raise RuntimeError("BOT_HANDLE and BOT_PASSWORD must be set in .env")
        _bot_client = Client()
        _bot_client.login(settings.bot_handle, settings.bot_password)
        logger.info("Intro-post bot logged in as %s", settings.bot_handle)
    return _bot_client


def ensure_intro_post(db) -> str | None:
    """
    Return the intro post URI, creating it if it hasn't been posted yet.
    Returns None if bot credentials are not configured.
    """
    if not settings.bot_handle or not settings.bot_password:
        return None

    row = db.get(BotConfig, 1)

    if row and row.intro_post_uri:
        return row.intro_post_uri

    try:
        client = _get_bot_client()
        response = client.send_post(text=_INTRO_TEXT)
        uri = response.uri

        # Wait briefly for the AppView to index the new post — a URI placed
        # in a skeleton before indexing completes is dropped during hydration.
        for _ in range(4):
            try:
                if client.app.bsky.feed.get_posts({"uris": [uri]}).posts:
                    break
            except Exception:
                break
            time.sleep(0.5)

        logger.info("Created intro post → %s", uri)
    except Exception as exc:
        logger.warning("Failed to create intro post: %s", exc)
        return None

    if row is None:
        row = BotConfig(id=1, intro_post_uri=uri)
        db.add(row)
    else:
        row.intro_post_uri = uri

    db.commit()
    return uri
