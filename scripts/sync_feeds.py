"""
Sync feeds from the jetstream-activity database into the feed registry.

Reads feed_generators + feed_generator_likes_daily from the mounted sqlite db,
adds any feeds not already in the registry that meet the minimum likes threshold,
and bootstraps arm states for the default user.

    make sync-feeds                        # default: min 5 total likes
    make sync-feeds MIN_LIKES=20           # raise the bar
"""

import os
import sqlite3
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scripts.feed_utils import compute_priority_boost
from store.database import SessionLocal
from store.models import ArmState, Feed

JETSTREAM_DB = os.environ.get(
    "JETSTREAM_DB", "/data/jetstream-activity.db"
)
MIN_LIKES = int(os.environ.get("MIN_LIKES", "5"))
DEFAULT_USER_DID = os.environ.get("DEFAULT_USER_DID", "")


def sync():
    if not os.path.exists(JETSTREAM_DB):
        print(f"Jetstream db not found at {JETSTREAM_DB}")
        sys.exit(1)

    js = sqlite3.connect(f"file:{JETSTREAM_DB}?mode=ro", uri=True)
    js.row_factory = sqlite3.Row

    candidates = js.execute("""
        SELECT
            fg.uri,
            fg.display_name,
            fg.description,
            COALESCE(SUM(fl.likes), 0) AS total_likes
        FROM feed_generators fg
        LEFT JOIN feed_generator_likes_daily fl ON fg.uri = fl.feed_uri
        WHERE fg.deleted_at IS NULL
        GROUP BY fg.uri
        HAVING total_likes >= ?
        ORDER BY total_likes DESC
    """, (MIN_LIKES,)).fetchall()

    js.close()
    print(f"Found {len(candidates)} feed(s) with >= {MIN_LIKES} likes in jetstream db.")

    db = SessionLocal()
    try:
        added = skipped = downranked = 0

        for row in candidates:
            name = row["display_name"] or row["uri"]
            desc = row["description"] or ""
            boost = compute_priority_boost(name, desc)

            existing = db.get(Feed, row["uri"])
            if existing:
                if existing.priority_boost != boost:
                    existing.priority_boost = boost
                    downranked += 1
                skipped += 1
                continue

            db.add(Feed(
                feed_uri=row["uri"],
                display_name=name,
                description=desc,
                topic_tags="",
                priority_boost=boost,
            ))
            added += 1
            flag = " [DOWNRANKED]" if boost < 1.0 else ""
            print(f"  + [{row['total_likes']:>5} likes] {name}{flag}")

        db.commit()
        print(f"\nAdded {added} new feed(s), skipped {skipped} already in registry, updated priority_boost on {downranked}.")

        if DEFAULT_USER_DID:
            all_feeds = db.query(Feed).all()
            arms_added = 0
            for feed in all_feeds:
                exists = (
                    db.query(ArmState)
                    .filter_by(user_did=DEFAULT_USER_DID, feed_uri=feed.feed_uri)
                    .first()
                )
                if not exists:
                    db.add(ArmState(
                        user_did=DEFAULT_USER_DID,
                        feed_uri=feed.feed_uri,
                        alpha=1.0,
                        beta=1.0,
                        pulls=0,
                    ))
                    arms_added += 1
            db.commit()
            if arms_added:
                print(f"Bootstrapped {arms_added} new arm(s) for {DEFAULT_USER_DID}.")
        else:
            print("DEFAULT_USER_DID not set — skipping arm bootstrap.")

    finally:
        db.close()


if __name__ == "__main__":
    sync()
