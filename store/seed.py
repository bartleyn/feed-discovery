"""
Seed the feed registry with an initial set of Bluesky feeds.

Run once after migrations:
    python -m store.seed

Add or remove feeds from SEED_FEEDS as you like.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from store.database import SessionLocal
from store.models import Feed

# Curated starter feeds — edit freely
# Format: (feed_uri, display_name, description, topic_tags)
SEED_FEEDS = [
    (
        "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot",
        "What's Hot",
        "Trending content across Bluesky",
        "trending,general",
    ),
    (
        "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/with-friends",
        "Popular With Friends",
        "Content popular with people you follow",
        "social,general",
    ),
    (
        "at://did:plc:jfhpnnst6flqway4eaeqzj2a/app.bsky.feed.generator/for-science",
        "Science",
        "Science content from researchers and enthusiasts",
        "science",
    ),
    (
        "at://did:plc:wqowuobffl66jv3kpsvo7ak4/app.bsky.feed.generator/the-algorithm",
        "The Algorithm",
        "Engagement-ranked feed across Bluesky",
        "trending,general",
    ),
    (
        "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/bsky-team",
        "Bluesky Team",
        "Posts from the Bluesky team",
        "bluesky,meta",
    ),
]

def seed():
    db = SessionLocal()
    try:
        added_feeds = 0

        for (uri, name, desc, tags) in SEED_FEEDS:
            existing = db.get(Feed, uri)
            if not existing:
                db.add(Feed(
                    feed_uri=uri,
                    display_name=name,
                    description=desc,
                    topic_tags=tags,
                ))
                added_feeds += 1
                print(f"  + Feed: {name}")
            else:
                print(f"  ~ Feed already exists: {name}")

        db.commit()

        print(f"\nDone. Added {added_feeds} feed(s).")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
