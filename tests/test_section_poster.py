from bot.section_poster import _compose_section_text, _feed_subscribe_url
from store.models import Feed


def _feed(
    feed_uri="at://did:plc:abc123/app.bsky.feed.generator/my-feed",
    display_name="My Feed",
    description="A great feed about things.",
    topic_tags="tech,science",
    section_post_uri=None,
) -> Feed:
    f = Feed()
    f.feed_uri = feed_uri
    f.display_name = display_name
    f.description = description
    f.topic_tags = topic_tags
    f.section_post_uri = section_post_uri
    return f


# --- _feed_subscribe_url ---

def test_subscribe_url_format():
    url = _feed_subscribe_url("at://did:plc:abc123/app.bsky.feed.generator/my-feed")
    assert url == "https://bsky.app/profile/did:plc:abc123/feed/my-feed"


def test_subscribe_url_short_uri_returns_empty():
    assert _feed_subscribe_url("at://did:plc:abc123") == ""


# --- _compose_section_text ---

def test_full_feed_contains_all_parts():
    text = _compose_section_text(_feed())
    assert "⬆  My Feed" in text
    assert "━" in text
    assert "A great feed about things." in text
    assert "https://bsky.app/profile/did:plc:abc123/feed/my-feed" in text
    assert "#tech" in text
    assert "#science" in text


def test_separator_wraps_name():
    text = _compose_section_text(_feed())
    lines = text.splitlines()
    name_idx = next(i for i, l in enumerate(lines) if "My Feed" in l)
    assert "━" in lines[name_idx - 1]
    assert "━" in lines[name_idx + 1]


def test_no_description():
    text = _compose_section_text(_feed(description=""))
    assert "⬆  My Feed" in text
    assert "Subscribe:" in text


def test_no_tags():
    text = _compose_section_text(_feed(topic_tags=""))
    assert "#" not in text


def test_long_description_is_truncated():
    long_desc = "x" * 300
    text = _compose_section_text(_feed(description=long_desc))
    lines = text.splitlines()
    desc_line = next(l for l in lines if l.startswith("x"))
    assert len(desc_line) <= 201  # 200 chars + "…"
    assert desc_line.endswith("…")


def test_tags_stripped_of_special_chars():
    text = _compose_section_text(_feed(topic_tags="sci-fi, tech news, 100x"))
    assert "#scifi" in text
    assert "#technews" in text
    assert "#100x" in text


def test_no_display_name_fallback():
    text = _compose_section_text(_feed(display_name=""))
    assert "⬆  Unknown Feed" in text


def test_total_length_never_exceeds_300_graphemes():
    long_desc = "A " * 150  # 300 chars on its own
    text = _compose_section_text(_feed(description=long_desc))
    assert len(text) <= 299


def test_subscribe_url_survives_long_description():
    long_desc = "A " * 150
    text = _compose_section_text(_feed(description=long_desc))
    assert "https://bsky.app/profile/did:plc:abc123/feed/my-feed" in text
