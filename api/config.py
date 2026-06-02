from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "postgresql://feed:feed@localhost:5432/feed_discovery"
    atproto_handle: str = ""
    atproto_password: str = ""

    # Default user DID for single-user POC.
    # Once resolved after login this is cached here at startup.
    default_user_did: str = ""

    # Chunk size — number of posts to pull from each feed
    chunk_size: int = 5

    # Reward window in minutes — how long after impression to collect interactions
    reward_window_minutes: int = 30


settings = Settings()
