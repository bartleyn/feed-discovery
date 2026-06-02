import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from store import get_db, Feed
from ingestion import atproto_client
from api.schemas import FeedOut, ChunkOut, PostOut

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/feeds", tags=["feeds"])


@router.get("/", response_model=list[FeedOut])
def list_feeds(db: Session = Depends(get_db)):
    """Return all feeds in the registry."""
    return db.query(Feed).order_by(Feed.display_name).all()


@router.get("/{feed_uri:path}/chunk", response_model=ChunkOut)
def preview_chunk(feed_uri: str, db: Session = Depends(get_db)):
    """
    Fetch a raw chunk from a feed without logging an impression.
    Useful for previewing a feed during development.
    """
    feed = db.get(Feed, feed_uri)
    if not feed:
        raise HTTPException(status_code=404, detail="Feed not found in registry")

    chunk = atproto_client.get_chunk(feed_uri)

    posts = [
        PostOut(
            uri=p.uri,
            cid=p.cid,
            author_did=p.author_did,
            author_handle=p.author_handle,
            text=p.text,
            created_at=p.created_at,
            age_seconds=p.age_seconds,
            like_count=p.like_count,
            repost_count=p.repost_count,
            reply_count=p.reply_count,
        )
        for p in chunk.posts
    ]

    return ChunkOut(
        impression_id=-1,           # -1 = not logged
        feed_uri=feed_uri,
        display_name=feed.display_name,
        posts=posts,
    )
