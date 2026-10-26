# feed-discovery

An AT Protocol **feed generator that recommends other people's feeds**. Instead of
serving posts on a topic, it serves a *feed of feeds*: each response is a handful of
short chunks pulled live from candidate feed generators, so scrolling the feed is a
tour of the Bluesky feed ecosystem rather than of one timeline.

Live as a custom feed on Bluesky — `did:web:feeds.barn.city`

Currently it's implemented as a multi-armed bandit over existing feeds, which is good
for cold start exploration, but we have since gathered additional content-related information
suggesting a hybrid content/CF approach optimized towards novelty is more rewarding.

## How a request works

`app.bsky.feed.getFeedSkeleton` is the whole surface:

```
bsky.app ──► GET /xrpc/app.bsky.feed.getFeedSkeleton   (service JWT verified)
                │
                ├─ 1. select N feeds from the registry          (feeds_per_slate = 4)
                ├─ 2. fetch a chunk from each, in parallel      (chunk_size = 8 posts)
                │      hard wall-clock budget of 2.0s — a feed that misses the
                │      deadline is abandoned and treated as empty, never blocking
                │      the response past bsky.app's own timeout
                ├─ 3. filter harmful posts, assemble chunks atomically
                ├─ 4. log one impression per feed shown
                └─► skeleton of post URIs, each tagged
                      feedContext = "<feed_uri>|<impression_id>"
```

`feedContext` allows the API to attribute an interaction to a specific feed via `appbsky.feed.sendInteractions`

### Endpoints

| route | purpose |
|---|---|
| `GET /.well-known/did.json` | did:web document (identity) |
| `GET /xrpc/app.bsky.feed.describeFeedGenerator` | feed generator descriptor |
| `GET /xrpc/app.bsky.feed.getFeedSkeleton` | **the feed** (service JWT → user; no token → anonymous slate, nothing logged) |
| `POST /xrpc/app.bsky.feed.sendInteractions` | interaction ingest (service JWT required; user taken from `iss`) |
| `GET /feeds/`, `GET /feeds/{uri}/chunk` | registry inspection / raw chunk (no impression) |
| `GET /status` | admin page (HTTP Basic; `STATUS_PASSWORD`) |
| `GET /health` | liveness |


## Setup

```bash
cp .env.example .env        # ATPROTO_HANDLE, ATPROTO_PASSWORD (app password),
                            # FEED_GENERATOR_HOSTNAME, FEED_GENERATOR_SIGNING_KEY
make up                     # docker compose up (code baked into the image, runs as non-root)
make upgrade                # alembic migrations
make seed                   # seed the feed registry
```

For local hacking, `make dev` adds a bind mount and live reload (see
`docker-compose.dev.yml`). `make test` runs the unit tests inside the image.
The api container gets an explicit allowlist of variables from `.env`; the
file itself is never mounted or copied in.


Verify:

```bash
curl http://localhost:8391/health
curl http://localhost:8391/feeds/
curl http://localhost:8391/.well-known/did.json
make test-chunk             # raw chunk, no impression logged
```

API docs: http://localhost:8391/docs

## Project structure

```
feed-discovery/
├── api/
│   ├── main.py                  # FastAPI app, lifespan, scheduler
│   ├── config.py                # settings (pydantic-settings)
│   └── routers/
│       ├── feed_generator.py    # getFeedSkeleton — slate assembly, deadlines, filtering
│       ├── well_known.py        # did:web document, describeFeedGenerator
│       ├── interactions.py      # sendInteractions ingest
│       ├── feeds.py             # registry inspection
│       └── status.py            # admin page
├── bandit/
│   ├── thompson.py              # Beta–Bernoulli sampling, arm updates
│   └── reward.py                # interaction → reward mapping
├── ingestion/
│   ├── caller.py                # ATProtoClient — auth, upstream feed calls
│   ├── feed_health.py           # liveness probing
│   └── feed_likes.py            # AppView like-count refresh
├── bot/                         # intro post + section posts between chunks
├── dags/                        # Airflow: liveness refresh, layout rebuild
├── scripts/                     # sync, clustering (BERTopic/kmeans), layout, backfills
└── store/                       # SQLAlchemy models, migrations
```

## Schema

| Table | Purpose |
|---|---|
| `feeds` | Candidate registry — URI, creator, like count, liveness, cluster, priority boost |
| `feed_clusters` | Theme assignment for feeds |
| `impressions` | One row per feed shown in a response (one bandit pull) |
| `chunk_posts` | Individual posts within a shown chunk |
| `interactions` | User actions attributed via `feedContext` |
| `arm_state` | Thompson α/β per `(user_did, feed_uri)` |

All tables carry `user_did` — multi-user at the schema level, single-user in practice today.

### Known gaps

