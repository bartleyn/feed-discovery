"""
Bot account that posts section tweets between feed chunks in the slate.

Each registered feed gets exactly one section post, created on first serve
and reused forever. The post uses an up-arrow (↑) to indicate the posts
immediately above it in the feed came from that source.

Post format:
    ↑ [Feed Name]
    [Description, trimmed to 200 chars]

    Subscribe: https://bsky.app/profile/{did}/feed/{rkey}

    #tag1 #tag2  (if topic_tags set)
"""

import logging
import re
from functools import lru_cache

from atproto import Client

from api.config import settings
from store.models import Feed

logger = logging.getLogger(__name__)

_BSKY_APP_FEED_BASE = "https://bsky.app/profile/{did}/feed/{rkey}"
_MAX_DESC = 200


def _feed_subscribe_url(feed_uri: str) -> str:
    """Convert at://did:.../app.bsky.feed.generator/rkey → bsky.app URL."""
    # feed_uri: at://<did>/app.bsky.feed.generator/<rkey>
    parts = feed_uri.removeprefix("at://").split("/")
    if len(parts) < 3:
        return ""
    did, _, rkey = parts[0], parts[1], parts[2]
    return _BSKY_APP_FEED_BASE.format(did=did, rkey=rkey)


def _compose_section_text(feed: Feed) -> str:
    lines: list[str] = []

    name = feed.display_name or "Unknown Feed"
    lines.append(f"↑ {name}")

    if feed.description:
        desc = feed.description.strip()
        if len(desc) > _MAX_DESC:
            desc = desc[:_MAX_DESC].rstrip() + "…"
        lines.append(desc)

    url = _feed_subscribe_url(feed.feed_uri)
    if url:
        lines.append(f"\nSubscribe: {url}")

    if feed.topic_tags:
        tags = [t.strip() for t in feed.topic_tags.split(",") if t.strip()]
        if tags:
            hashtags = " ".join(f"#{re.sub(r'[^a-zA-Z0-9]', '', t)}" for t in tags)
            lines.append(f"\n{hashtags}")

    return "\n".join(lines)


@lru_cache(maxsize=1)
def _get_bot_client() -> Client:
    if not settings.bot_handle or not settings.bot_password:
        raise RuntimeError("BOT_HANDLE and BOT_PASSWORD must be set in .env")
    client = Client()
    client.login(settings.bot_handle, settings.bot_password)
    logger.info("Bot logged in as %s", settings.bot_handle)
    return client


def create_section_post(feed: Feed) -> str:
    """Post a section tweet for this feed and return its AT URI."""
    client = _get_bot_client()
    text = _compose_section_text(feed)
    response = client.send_post(text=text)
    uri = response.uri
    logger.info("Created section post for feed %s → %s", feed.feed_uri, uri)
    return uri


def ensure_section_post(feed: Feed, db) -> str | None:
    """
    Return the section post URI for this feed, creating it if missing.

    Returns None if bot credentials are not configured (non-fatal).
    """
    if feed.section_post_uri:
        return feed.section_post_uri

    if not settings.bot_handle or not settings.bot_password:
        logger.debug("Bot credentials not set — skipping section post for %s", feed.feed_uri)
        return None

    try:
        uri = create_section_post(feed)
        feed.section_post_uri = uri
        db.add(feed)
        db.commit()
        return uri
    except Exception as exc:
        logger.warning("Failed to create section post for %s: %s", feed.feed_uri, exc)
        return None
