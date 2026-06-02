# feed-discovery

Contextual multi-armed bandit for Bluesky feed-of-feeds discovery.

Each feed is an arm. Users are shown chunks (5-post previews) of feeds.
Interactions with chunks (likes, reposts, subscribes) update the bandit's
belief about each feed's value for that user.

## Phase status

- [x] Phase 1 — Data layer, AT Proto caller, FastAPI skeleton
- [ ] Phase 2 — Impression & interaction logging, reward computation
- [ ] Phase 3 — Thompson Sampling bandit, /slate endpoint
- [ ] Phase 4 — Frontend chunk display UI

## Setup

### 1. Configure credentials

```bash
cp .env.example .env
# Fill in ATPROTO_HANDLE and ATPROTO_PASSWORD (use an app password)
```

### 2. Start services

```bash
make up
```

### 3. Run migrations & seed

```bash
make upgrade
make seed
```

### 4. Verify

```bash
# Health check
curl http://localhost:8000/health

# List feeds in registry
curl http://localhost:8000/feeds/

# Fetch a raw chunk (no impression logged)
make test-chunk
```

API docs: http://localhost:8000/docs

## Project structure

```
feed-discovery/
├── api/
│   ├── main.py          # FastAPI app, startup, lifespan
│   ├── config.py        # Settings (pydantic-settings)
│   ├── schemas.py       # Pydantic request/response models
│   └── routers/
│       └── feeds.py     # GET /feeds/, GET /feeds/{uri}/chunk
├── ingestion/
│   └── caller.py        # ATProtoClient — authenticates, calls feed generators
├── store/
│   ├── models.py        # SQLAlchemy ORM models
│   ├── database.py      # Engine, SessionLocal, get_db dependency
│   ├── seed.py          # Feed registry seeding script
│   └── migrations/      # Alembic migrations
├── bandit/              # Phase 3 — Thompson Sampling (empty)
├── interaction/         # Phase 2 — interaction logging (empty)
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
└── Makefile
```

## Schema overview

| Table | Purpose |
|---|---|
| `feeds` | Registry of known Bluesky feed URIs |
| `impressions` | Each chunk shown to a user (one bandit pull) |
| `chunk_posts` | Individual posts in each shown chunk |
| `interactions` | User actions on chunk posts (like, repost, subscribe…) |
| `arm_state` | Thompson Sampling α/β per (user_did, feed_uri) |

All tables carry `user_did` — multi-user ready at the schema level.
