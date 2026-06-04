"""
One-time script: publish an app.bsky.feed.generator record to your Bluesky repo.

Run once after the service is publicly reachable at FEED_GENERATOR_HOSTNAME.

    python -m scripts.register_feed
"""

from datetime import datetime, timezone

from atproto import Client

from api.config import settings

RKEY = "feed-discovery"


def main():
    client = Client()
    profile = client.login(settings.atproto_handle, settings.atproto_password)
    user_did = profile.did

    feed_uri = f"at://{user_did}/app.bsky.feed.generator/{RKEY}"

    client.com.atproto.repo.put_record(
        data={
            "repo": user_did,
            "collection": "app.bsky.feed.generator",
            "rkey": RKEY,
            "record": {
                "$type": "app.bsky.feed.generator",
                "did": settings.feed_generator_did,
                "displayName": "Feed Discovery [TEST]",
                "description": "Experimental bandit-ranked feed-of-feeds",
                "acceptsInteractions": True,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            },
        }
    )

    print(f"Registered feed generator record.")
    print(f"Feed URI: {feed_uri}")
    print(f"Generator DID: {settings.feed_generator_did}")
    print()
    print("Subscribe in bsky.app by searching for the feed or visiting:")
    print(f"  https://bsky.app/profile/{user_did}/feed/{RKEY}")


if __name__ == "__main__":
    main()
