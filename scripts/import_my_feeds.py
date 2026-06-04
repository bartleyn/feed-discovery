"""
Import the authenticated user's saved/pinned Bluesky feeds into the registry.

Reads app.bsky.actor.getPreferences to find saved and pinned feed URIs,
fetches their generator metadata, and upserts into the feeds table.

    docker compose exec api python -m scripts.import_my_feeds
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from atproto import Client
from api.config import settings
from store.database import SessionLocal
from store.models import Feed, ArmState


def extract_feed_uris(preferences: list[dict]) -> list[str]:
    uris: list[str] = []
    for pref in preferences:
        ptype = pref.get("$type", "")
        if ptype == "app.bsky.actor.defs#savedFeedsPrefV2":
            for item in pref.get("items", []):
                if item.get("type") == "feed":
                    uris.append(item["value"])
        elif ptype == "app.bsky.actor.defs#savedFeedsPref":
            for uri in pref.get("pinned", []):
                uris.append(uri)
            for uri in pref.get("saved", []):
                uris.append(uri)
    # deduplicate, preserve order
    seen: set[str] = set()
    result = []
    for uri in uris:
        if uri not in seen and uri.startswith("at://"):
            seen.add(uri)
            result.append(uri)
    return result


def import_feeds():
    client = Client()
    profile = client.login(settings.atproto_handle, settings.atproto_password)
    user_did = profile.did
    print(f"Logged in as {settings.atproto_handle} ({user_did})\n")

    # Use raw HTTP to avoid strict model validation failing on unknown pref types
    # (SDK 0.0.55 doesn't know app.bsky.actor.defs#declaredAgePref etc.)
    import httpx
    resp = httpx.get(
        "https://bsky.social/xrpc/app.bsky.actor.getPreferences",
        headers={"Authorization": f"Bearer {client._session.access_jwt}"},
    )
    resp.raise_for_status()
    feed_uris = extract_feed_uris(resp.json().get("preferences", []))

    if not feed_uris:
        print("No saved/pinned feeds found in preferences.")
        return

    print(f"Found {len(feed_uris)} feed URI(s) in preferences.")

    # Fetch generator metadata in one batch (max 25 per call)
    generators = []
    for i in range(0, len(feed_uris), 25):
        batch = feed_uris[i:i + 25]
        try:
            resp = client.app.bsky.feed.get_feed_generators({"feeds": batch})
            generators.extend(resp.feeds)
        except Exception as exc:
            print(f"  Warning: failed to fetch batch {i//25 + 1}: {exc}")

    db = SessionLocal()
    try:
        added = 0
        skipped = 0
        for gen in generators:
            existing = db.get(Feed, gen.uri)
            if existing:
                skipped += 1
                print(f"  ~ already in registry: {gen.display_name}")
                continue

            db.add(Feed(
                feed_uri=gen.uri,
                display_name=gen.display_name or gen.uri,
                description=getattr(gen, "description", "") or "",
                topic_tags="",
            ))
            added += 1
            print(f"  + {gen.display_name}")

        db.commit()
        print(f"\nAdded {added} feed(s), skipped {skipped} already in registry.")

        # Bootstrap arm state for the authenticated user
        all_feeds = db.query(Feed).all()
        arms_added = 0
        for feed in all_feeds:
            existing_arm = (
                db.query(ArmState)
                .filter_by(user_did=user_did, feed_uri=feed.feed_uri)
                .first()
            )
            if not existing_arm:
                db.add(ArmState(
                    user_did=user_did,
                    feed_uri=feed.feed_uri,
                    alpha=1.0,
                    beta=1.0,
                    pulls=0,
                ))
                arms_added += 1

        db.commit()
        if arms_added:
            print(f"Bootstrapped {arms_added} new arm(s) for {user_did}.")

    finally:
        db.close()


if __name__ == "__main__":
    import_feeds()
