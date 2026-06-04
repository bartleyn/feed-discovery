"""
Import feeds from popular_feeds.csv into the feed registry.

Reads the CSV produced by the sqlite dump and upserts into Postgres.
Run on the host (not in the container):

    DATABASE_URL=postgresql://feed:changeme@localhost:5432/feed_discovery \
        python scripts/import_from_csv.py
"""

import csv
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from store.database import SessionLocal
from store.models import Feed, ArmState

CSV_PATH = os.path.join(os.path.dirname(__file__), "popular_feeds.csv")
DEFAULT_USER_DID = os.environ.get("DEFAULT_USER_DID", "")


def import_feeds():
    db = SessionLocal()
    try:
        added = 0
        skipped = 0

        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                uri = row["uri"].strip()
                name = row["display_name"].strip() or uri
                desc = row["description"].strip()
                likes = int(row["total_likes"])

                existing = db.get(Feed, uri)
                if existing:
                    skipped += 1
                    continue

                db.add(Feed(
                    feed_uri=uri,
                    display_name=name,
                    description=desc,
                    topic_tags="",
                ))
                added += 1
                print(f"  + [{likes:>5} likes] {name}")

        db.commit()
        print(f"\nAdded {added} feed(s), skipped {skipped} already in registry.")

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
            print(f"Bootstrapped {arms_added} new arm(s) for {DEFAULT_USER_DID}.")
        else:
            print("DEFAULT_USER_DID not set — skipping arm bootstrap.")

    finally:
        db.close()


if __name__ == "__main__":
    import_feeds()
