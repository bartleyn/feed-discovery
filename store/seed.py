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
from store.models import Feed, ArmState

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

# Your DID for the single-user POC arm bootstrapping
# Set via env or replace directly
DEFAULT_USER_DID = os.environ.get("DEFAULT_USER_DID", "")


def seed():
    db = SessionLocal()
    try:
        added_feeds = 0
        added_arms = 0

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

        # Bootstrap arm state for default user if DID is known
        if DEFAULT_USER_DID:
            for (uri, name, _, _) in SEED_FEEDS:
                existing_arm = (
                    db.query(ArmState)
                    .filter_by(user_did=DEFAULT_USER_DID, feed_uri=uri)
                    .first()
                )
                if not existing_arm:
                    db.add(ArmState(
                        user_did=DEFAULT_USER_DID,
                        feed_uri=uri,
                        alpha=1.0,
                        beta=1.0,
                        pulls=0,
                    ))
                    added_arms += 1

            db.commit()
            print(f"\nBootstrapped {added_arms} arm(s) for {DEFAULT_USER_DID}")
        else:
            print("\nNo DEFAULT_USER_DID set — skipping arm bootstrap.")
            print("Arms are created on first /slate request per user.")

        print(f"\nDone. Added {added_feeds} feed(s).")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
