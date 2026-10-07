"""
feedmoreseemore — periodic re-sample of post content, for topic freshness.

Nothing scheduled this before now. scripts/fetch_feed_posts.py has existed and worked since
2026-07, but no DAG called it, so feed_posts.jsonl (the content input BOTH cluster_feeds_embed.py
and classify_feed_liveness.py read) was frozen at its Jul 18 snapshot indefinitely — every
monthly rebuild re-clustered the SAME stale post text instead of fresh samples. Feed content
drifts (a feed gets repurposed, an algorithmic feed's topic mix shifts) independently of the
name/description drift feedmoreseemore_liveness's reprobe now tracks, and independently of
liveness itself — a feed can stay reachable while what it actually posts changes completely.

Pipeline (linear):
    select_online -> fetch_posts -> classify_liveness

select_online reads the CURRENT online set from feedmoreseemore's own feed_health (kept fresh
every 3 days by feedmoreseemore_liveness), not the frozen feed_generators.jsonl snapshot
fetch_feed_posts defaults to — that file was ~11k feeds behind the live registry as of
2026-08. Restricting to online-only also skips the ~13% of the registry that's offline,
which would otherwise burn a third of an already multi-hour run on feeds guaranteed to
return nothing.

fetch_posts runs with --refresh, so every online feed gets a NEW sample each cycle instead of
being fetched once and skipped forever — that skip-if-already-fetched default is right for a
one-off backfill and wrong for a recurring refresh (it is exactly the bug just fixed for
feed_generators, reproduced here for post content, so --refresh exists to not repeat it). It
writes to a temp file and swaps it in atomically on completion, so a run that dies partway
through (this is a several-hour job) leaves the previous cycle's data intact.

classify_liveness regenerates feed_liveness.csv from the fresh posts — the richer LIVE / AGING
/ DEAD classification (corpse-placeholder text detection, branding-only detection) that
feedmoreseemore_liveness's lightweight limit=1 reprobe cannot do — so the layout build's CSV
fallback stays current too, not just PG feed_health.

Deliberately does NOT trigger a layout rebuild. feedmoreseemore_layout_rebuild is monthly,
expensive, and requires human review before promotion (see its own docstring) — this DAG's
job is only to make sure fresh content is WAITING for that DAG when it runs, not to trigger
it. Scheduled a week before the rebuild's 1st-of-month slot so the rebuild always consumes
this cycle's data, never the previous one's.

Deploy: symlink or copy this file into ~/airflow/dags/ (matches the other feedmoreseemore DAGs).
"""
import os
import subprocess
from datetime import datetime, timedelta

from airflow.sdk import DAG, task

REPO = os.environ.get(
    "FEEDMORESEEMORE_DIR",
    "/Users/nathanbartley/srv/feedmoreseemore",
)
FEED_DISCOVERY = os.path.join(REPO, "feed-discovery")
FEED_EXPLORER = os.path.join(REPO, "feed-explorer")
FEEDS_DIR = os.environ.get("FEEDS_DIR", "/Volumes/miniext/feeds")
URIS_FILE = os.path.join(FEEDS_DIR, "posts_refresh_online_uris.txt")

# Same pinned interpreter as the other feedmoreseemore DAGs — numpy/psycopg2/aiohttp live in
# the system Python's user site-packages, not Airflow's venv.
PYTHON = os.environ.get("FEEDMORESEEMORE_PYTHON", "/Library/Developer/CommandLineTools/usr/bin/python3")

# A half-finished multi-hour fetch should not silently retry and double the runtime; a
# transient failure gets caught by next month's run instead.
default_args = {"retries": 0}


def py(module: str, *args: str) -> None:
    """Run a feed-explorer script as a module, streaming output (no pipe deadlock)."""
    cmd = [PYTHON, "-W", "ignore", "-m", module, *args]
    r = subprocess.run(cmd, cwd=FEED_EXPLORER)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} exited {r.returncode}")


with DAG(
    "feedmoreseemore_posts_refresh",
    description="Re-sample 25 posts/feed for online feeds and re-derive liveness from them",
    start_date=datetime(2026, 1, 1),
    schedule="0 4 25 * *",   # 4am on the 25th: ~6.9h measured @ conc 20 for the current
                              # ~128k online feeds finishes overnight, and lands a week before
                              # the rebuild DAG's 1st-of-month slot so it always has fresh input.
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["feedmoreseemore", "posts", "liveness"],
) as dag:

    @task(execution_timeout=timedelta(minutes=5))
    def select_online():
        # Single source = feedmoreseemore's own feed_health (mirrors what
        # feedmoreseemore_liveness already probes every 3 days), not feed_generators.jsonl —
        # that snapshot only updates when someone runs sync_registry's upstream export by
        # hand and was found ~11k feeds stale. 'auth' feeds are included: they are valid,
        # reachable feeds that just need a login, same treatment as feedmoreseemore_liveness
        # gives them (auth -> counted online).
        import psycopg2

        env = os.path.join(FEED_DISCOVERY, ".env")
        if os.path.exists(env):
            for line in open(env):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
        dsn = os.environ.get("HEALTH_DATABASE_URL", "postgresql://nathanbartley@localhost/feedmoreseemore")
        # gssencmode=disable: libpq's Kerberos probe spins forever in a forked task on macOS.
        conn = psycopg2.connect(dsn, connect_timeout=5, gssencmode="disable")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT feed_uri FROM feed_health WHERE status IN ('online', 'auth')")
                uris = sorted(r[0] for r in cur.fetchall())
        finally:
            conn.close()
        if not uris:
            raise RuntimeError("feed_health returned zero online/auth feeds — refusing to "
                                "write an empty worklist (likely PG unreachable, not an "
                                "actual empty registry)")
        os.makedirs(FEEDS_DIR, exist_ok=True)
        with open(URIS_FILE, "w") as f:
            f.write("\n".join(uris) + "\n")
        print(f"[select_online] {len(uris):,} online/auth feeds -> {URIS_FILE}")

    @task(execution_timeout=timedelta(hours=10))
    def fetch_posts():
        # Measured 2026-08-25: 5.19 feeds/s @ concurrency 20 (2.0% error rate) against a
        # 300-feed sample -> ~6.9h for the current ~128,558 online feeds. --refresh makes
        # this a true re-sample every cycle (see module docstring) rather than a one-time
        # fetch that silently no-ops on every later run.
        # --auth: unauthenticated getFeed returns 0 posts for personalized feeds, which
        # both classify_liveness (below) and a full layout rebuild's PG-driven LIVE filter
        # then can't tell apart from a genuinely dead feed. A real viewer context resolves
        # that ambiguity for the ~17k feeds currently sitting in that empty-probe bucket.
        # Needs ATPROTO_HANDLE/ATPROTO_PASSWORD in feed-discovery/.env.
        py("scripts.fetch_feed_posts",
           "--uris-file", URIS_FILE, "--refresh", "--posts", "25", "--concurrency", "20", "--auth")

    @task()
    def sync_health():
        # Pushes the auth pass's real freshness into feed_health.latest_post_at directly —
        # without this, the richer signal only ever reached feed_liveness.csv, a fallback PG
        # already shadows for nearly every feed, so it never actually moved the layout
        # rebuild's strict LIVE filter. GREATEST-based and only touches feeds with a real
        # post (n>0), so it's safe to run every cycle.
        py("scripts.sync_feed_posts_health")

    @task()
    def classify_liveness():
        # Regenerates feed_liveness.csv from the fresh feed_posts.jsonl — the content-based
        # corpse/branding-placeholder detection that PG feed_health's lightweight probe
        # doesn't do, so build_feed_layout's CSV fallback path stays current alongside PG.
        py("scripts.classify_feed_liveness")

    select_online() >> fetch_posts() >> sync_health() >> classify_liveness()
