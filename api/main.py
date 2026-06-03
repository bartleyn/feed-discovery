"""
Feed Discovery API — Phase 1 skeleton.

Startup sequence:
1. Connect to Postgres, create tables if missing
2. Authenticate with AT Protocol
3. Mount routers

Phase 2 will add impression/interaction endpoints.
Phase 3 will add the /slate bandit endpoint.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import settings
from api.routers import feeds_router, well_known_router, feed_generator_router, interactions_router, status_router
from api.schemas import HealthOut
from bandit.reward import start_scheduler
from ingestion import atproto_client
from store import Base, engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("Creating database tables if they don't exist...")
    Base.metadata.create_all(bind=engine)

    logger.info("Authenticating with AT Protocol...")
    did = atproto_client.login()
    settings.default_user_did = did
    logger.info("Ready. User DID: %s", did)

    scheduler = start_scheduler()

    yield

    # --- Shutdown ---
    scheduler.shutdown(wait=False)
    logger.info("Shutting down.")


app = FastAPI(
    title="Feed Discovery",
    description="Contextual bandit feed-of-feeds for Bluesky",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(feeds_router)
app.include_router(well_known_router)
app.include_router(feed_generator_router)
app.include_router(interactions_router)
app.include_router(status_router)


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health():
    return HealthOut(
        status="ok",
        atproto_did=atproto_client.did,
    )


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "feed-discovery",
        "phase": 2,
        "docs": "/docs",
    }
