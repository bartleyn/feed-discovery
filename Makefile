.PHONY: up dev down logs migrate seed test test-chunk sync-feeds

# Start all services (hardened: code baked into the image, no bind mount)
up:
	docker compose up --build -d

# Start with the checkout bind-mounted and live reload, for local hacking
dev:
	docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build -d

# Run the unit tests inside the image against the current checkout
test:
	docker compose run --rm --no-deps -v "$(CURDIR):/app" -e DATABASE_URL=postgresql://x:x@localhost:1/x api python -m pytest tests -q

# Stop all services
down:
	docker compose down

# Tail API logs
logs:
	docker compose logs -f api

# Generate a new migration (usage: make migrate msg="add index")
migrate:
	docker compose exec api alembic revision --autogenerate -m "$(msg)"

# Apply all pending migrations
upgrade:
	docker compose exec api alembic upgrade head

# Seed feed registry
seed:
	docker compose exec api python -m store.seed

# Quick smoke test — fetch a raw chunk from What's Hot
test-chunk:
	curl -s "http://localhost:8391/feeds/at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot/chunk" | python3 -m json.tool

# Sync feeds from jetstream-activity.db (MIN_LIKES=5 by default)
sync-feeds:
	docker compose exec -e MIN_LIKES=$(or $(MIN_LIKES),5) api python -m scripts.sync_feeds
