from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://feed:feed@localhost:5432/feed_discovery"
    atproto_handle: str = ""
    atproto_password: str = ""

    # Default user DID for single-user POC.
    # Once resolved after login this is cached here at startup.
    default_user_did: str = ""

    # Feed generator identity — must match the public hostname exactly.
    # did:web is derived from the hostname.
    feed_generator_hostname: str = "feeds.barn.city"
    feed_generator_did: str = "did:web:feeds.barn.city"

    # Bot account — posts section tweets between feed chunks
    bot_handle: str = ""
    bot_password: str = ""

    # Signing key for service-to-service JWTs (hex-encoded secp256k1 private key)
    feed_generator_signing_key: str = ""
    # Corresponding public key in multibase format — added to the DID document
    feed_generator_public_key_multibase: str = ""

    # How many feeds to call per getFeedSkeleton request.
    feeds_per_slate: int = 4

    # Chunk size — number of posts to pull from each feed.e
    chunk_size: int = 8

    # Hard wall-clock budget (seconds) for upstream feed fetches within one
    # batch. Kept under bsky.app's getFeedSkeleton timeout so we always return
    # in time — feeds that don't respond by the deadline are abandoned and
    # treated as empty (penalised) rather than blocking the whole response.
    upstream_deadline_seconds: float = 2.5

    # Reward window in minutes — how long after impression to collect interactions
    reward_window_minutes: int = 5

    # How often to health-check feeds (hours); 0 disables
    feed_health_interval_hours: int = 0

    # How often to refresh served-feed like counts from the AppView (hours); 0 disables
    feed_likes_interval_hours: int = 0

    # Consecutive failures before a feed is flagged as unhealthy in status
    feed_health_failure_threshold: int = 3

    # Password for the /status admin page (HTTP Basic Auth). Empty = no auth.
    status_password: str = ""


settings = Settings()
