.PHONY: up down logs migrate seed test-chunk

# Start all services
up:
	docker compose up --build -d

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
	curl -s "http://localhost:8000/feeds/at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot/chunk" | python3 -m json.tool
