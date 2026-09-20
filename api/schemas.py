from datetime import datetime
from pydantic import BaseModel


# --- Feeds ---

class FeedOut(BaseModel):
    feed_uri: str
    display_name: str
    description: str
    topic_tags: str | None = None
    added_at: datetime

    model_config = {"from_attributes": True}


# --- Chunks ---

class PostOut(BaseModel):
    uri: str
    cid: str
    author_did: str
    author_handle: str
    text: str
    created_at: datetime
    age_seconds: int
    like_count: int
    repost_count: int
    reply_count: int


class ChunkOut(BaseModel):
    impression_id: int
    feed_uri: str
    display_name: str
    posts: list[PostOut]


class SlateOut(BaseModel):
    user_did: str
    chunks: list[ChunkOut]


# --- Interactions ---

class InteractionIn(BaseModel):
    impression_id: int
    post_uri: str
    user_did: str
    action: str   # like | repost | reply | profile_click | feed_subscribe


class InteractionOut(BaseModel):
    id: int
    impression_id: int
    post_uri: str
    action: str
    occurred_at: datetime

    model_config = {"from_attributes": True}


# --- Health ---

class HealthOut(BaseModel):
    status: str
    atproto_did: str
