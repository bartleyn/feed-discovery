"""
SQLAlchemy ORM models.

All tables carry user_did so the schema is multi-user ready
even though v1 is single-user.
"""

from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Text,
    CheckConstraint, ForeignKey, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Feed(Base):
    """Registry of known Bluesky feeds."""
    __tablename__ = "feeds"

    feed_uri    = Column(String, primary_key=True)   # at://did:.../app.bsky.feed.generator/...
    display_name = Column(String, nullable=False)
    description  = Column(Text, default="")
    topic_tags   = Column(String, default="")        # comma-separated; simple for now
    added_at     = Column(DateTime, default=datetime.utcnow)

    # AT URI of the bot's section post for this feed; populated on first serve
    section_post_uri = Column(String, nullable=True)

    # Multiplier applied to the Thompson sample before ranking (0–1).
    # Set < 1.0 for feeds marked Archived or Inactive.
    priority_boost = Column(Float, default=1.0, nullable=False, server_default="1.0")

    # When True, posts from this feed are hydrated and scanned for harmful
    # content before being added to the slate. Set manually after a bad post
    # is observed; adds one getPosts round-trip per 25 posts served.
    requires_filtering = Column(Integer, default=0, nullable=False, server_default="0")

    # Health tracking — updated by periodic feed health checks
    last_checked_at      = Column(DateTime, nullable=True)
    consecutive_failures = Column(Integer, default=0, nullable=False, server_default="0")

    impressions  = relationship("Impression", back_populates="feed")
    arm_states   = relationship("ArmState", back_populates="feed")


class Impression(Base):
    """One chunk shown to a user — the unit of a bandit pull."""
    __tablename__ = "impressions"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_did     = Column(String, nullable=False, index=True)
    feed_uri     = Column(String, ForeignKey("feeds.feed_uri"), nullable=False)
    shown_at     = Column(DateTime, default=datetime.utcnow)
    posts_shown  = Column(Integer, nullable=False)
    reward       = Column(Float, nullable=True)       # filled by reward job 30 min later
    rewarded_at  = Column(DateTime, nullable=True)

    feed         = relationship("Feed", back_populates="impressions")
    chunk_posts  = relationship("ChunkPost", back_populates="impression")
    interactions = relationship("Interaction", back_populates="impression")


class ChunkPost(Base):
    """Individual posts that were in a shown chunk."""
    __tablename__ = "chunk_posts"

    id               = Column(Integer, primary_key=True, autoincrement=True)
    impression_id    = Column(Integer, ForeignKey("impressions.id"), nullable=False, index=True)
    post_uri         = Column(String, nullable=False)
    post_age_seconds = Column(Integer, nullable=True)  # age at time of impression
    position         = Column(Integer, nullable=False)  # 0-indexed slot in chunk

    impression       = relationship("Impression", back_populates="chunk_posts")


class Interaction(Base):
    """User action on a post within a chunk."""
    __tablename__ = "interactions"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    impression_id = Column(Integer, ForeignKey("impressions.id"), nullable=False, index=True)
    post_uri      = Column(String, nullable=False)
    user_did      = Column(String, nullable=False, index=True)
    action        = Column(String, nullable=False)   # like | repost | reply | profile_click | feed_subscribe
    occurred_at   = Column(DateTime, default=datetime.utcnow)

    impression    = relationship("Impression", back_populates="interactions")


class ArmState(Base):
    """
    Thompson Sampling state per (user, feed) arm.

    alpha, beta are Beta distribution parameters.
    Start at (1, 1) = uniform prior (equal probability of reward).
    """
    __tablename__ = "arm_state"
    __table_args__ = (UniqueConstraint("user_did", "feed_uri"),)

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_did     = Column(String, nullable=False, index=True)
    feed_uri     = Column(String, ForeignKey("feeds.feed_uri"), nullable=False)
    alpha        = Column(Float, default=1.0, nullable=False)
    beta         = Column(Float, default=1.0, nullable=False)
    pulls        = Column(Integer, default=0, nullable=False)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    feed         = relationship("Feed", back_populates="arm_states")


class BotConfig(Base):
    """Single-row config for the bot account. id is always 1."""
    __tablename__ = "bot_config"
    __table_args__ = (CheckConstraint("id = 1"),)

    id             = Column(Integer, primary_key=True, default=1)
    intro_post_uri = Column(String, nullable=True)   # created once, reused forever
