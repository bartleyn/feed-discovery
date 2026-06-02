import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_database_url = os.environ.get(
    "DATABASE_URL",
    "postgresql://feed:feed@localhost:5432/feed_discovery"
)

engine = create_engine(_database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI dependency — yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
