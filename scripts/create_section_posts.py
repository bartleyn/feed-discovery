"""
One-time (and idempotent) script to create section posts for all registered
feeds that don't already have one.

Usage:
    python -m scripts.create_section_posts
"""

from store import SessionLocal
from store.models import Feed
from bot.section_poster import ensure_section_post


def main():
    db = SessionLocal()
    try:
        feeds = db.query(Feed).all()
        print(f"Found {len(feeds)} feeds.")
        for feed in feeds:
            if feed.section_post_uri:
                print(f"  skip  {feed.display_name} (already has post)")
                continue
            uri = ensure_section_post(feed, db)
            if uri:
                print(f"  ok    {feed.display_name} → {uri}")
            else:
                print(f"  fail  {feed.display_name} (check logs)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
