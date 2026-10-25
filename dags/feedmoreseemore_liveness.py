"""
feedmoreseemore — feed-explorer liveness refresh DAG.

Runs on the existing Airflow (~/airflow) alongside the atproto-health DAGs. Re-probes
every feed in the current explorer layout, persists health to Postgres, regenerates the
liveness allow-list the SPA reads, and deploys it — so the map only shows feeds verified
live recently (offline/decayed feeds stop rendering without a full re-cluster).

Deploy: symlink or copy this file into ~/airflow/dags/ (matches how atproto-health does it).
Set FEEDMORESEEMORE_DIR if the repo isn't at the default path below.

Pipeline (linear):
    sync_registry -> reprobe_liveness -> append_layout_incremental -> build_health_json
      -> embed_all_feeds -> topics_live -> build_topic_layout -> build_serve_data -> upload_data_r2

The site serves the TOPIC layout, derived from the cluster layout in public/data on every
run, minus feeds that must not be served (unresponsive, archived, empty, tombstoned, or from
an inactive account) in public/data_serve. A failed step skips the upload and leaves the last
good layout live.

append_layout_incremental is what keeps the MAP current, not just the allow-list: it puts
newly-live feeds onto the layout by nearest-existing-cluster, without re-clustering. The
periodic re-carve that incremental growth eventually needs lives in a separate, much less
frequent DAG — feedmoreseemore_layout_rebuild.
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
DATA_DIR = os.path.join(REPO, "feed-explorer", "public", "data")
TOPICS_DIR = os.path.join(REPO, "feed-explorer", "public", "data_topics")
SERVE_DIR = os.path.join(REPO, "feed-explorer", "public", "data_serve")
LABELS = os.path.join(REPO, "feed-explorer", "labels")   # paid label files are symlinks into FEEDS_DIR/topic_labels
FEEDS_DIR = os.environ.get("FEEDS_DIR", "/Volumes/miniext/feeds")
OLD_META = os.path.join(FEEDS_DIR, "relabel_2026-09-18", "feeds_meta.json")  # text the first paid labels saw

# The pipeline scripts need numpy/psycopg2/anthropic, which live in the system Python's
# user site-packages — NOT Airflow's venv. Pin an interpreter that has them (override with
# FEEDMORESEEMORE_PYTHON) so tasks don't run under Airflow's dependency-less venv.
PYTHON = os.environ.get("FEEDMORESEEMORE_PYTHON", "/Library/Developer/CommandLineTools/usr/bin/python3")

default_args = {"retries": 1, "retry_delay": timedelta(minutes=10)}


def py(module: str, *args: str, cwd: str = FEED_DISCOVERY) -> None:
    """Run a scripts.* module, streaming output (no pipe deadlock). cwd defaults to
    feed-discovery (registry/data-collection scripts); pass cwd=FEED_EXPLORER for the
    layout/presentation scripts that now live under feed-explorer/scripts."""
    cmd = [PYTHON, "-W", "ignore", "-m", module, *args]
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} exited {r.returncode}")


with DAG(
    "feedmoreseemore_liveness",
    description="Re-probe explorer feeds, persist health to PG, refresh the SPA allow-list",
    start_date=datetime(2026, 1, 1),
    schedule="0 2 */3 * *",          # 2am every 3 days — liveness decays slowly. Low concurrency (4
                                   # workers, gentle on the residential uplink) makes the probe
                                   # ~15-20h, so a run can outlast a day: max_active_runs=1 below
                                   # prevents overlap (the next trigger waits, no pileup).
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["feedmoreseemore", "liveness"],
) as dag:

    @task()
    def sync_registry():
        # Mirror atproto-health's feed-generator registry into feedmoreseemore's own database
        # BEFORE anything reads it. Downstream tasks queried activity.feed_generators across
        # the database boundary, which made the whole pipeline depend on a service that OOMs
        # every day or two (collect-activity, exit 134) and blocked moving feedmoreseemore off
        # this machine. Now that read happens exactly once, in a task that can retry on its
        # own without re-probing 148k feeds. Full-scan upsert, ~6s, idempotent.
        py("scripts.sync_registry")

    @task()
    def reprobe():
        # Probe the full registry, not just the layout — so newly-discovered feeds get
        # liveness and can enter future rebuilds. SINGLE SOURCE = atproto-health's live
        # Postgres activity.feed_generators (minus tombstones, which are recorded dead for
        # free): the collector upserts it from jetstream continuously, and the old discovery
        # pool jsonl was backfilled into it on 2026-08-05, so --pool is no longer passed.
        # Incremental: --refresh-older-than skips feeds probed in the last 3 days (PG
        # probed_at), so each run does new + stale only. Resumable across days; PG feed_health
        # is upserted continuously.
        py("scripts.reprobe_liveness", "--data", DATA_DIR,
           "--source", "registry", "--refresh-older-than", "3", cwd=FEED_EXPLORER)

    @task()
    def append_layout():
        # Put newly-live feeds ON THE MAP. Liveness alone never did this: the layout only
        # ever grew via one-off scripts, so the map was frozen at its last rebuild while the
        # registry kept growing (this is why no new trending feeds appeared). Assigns each
        # new feed to its nearest existing micro cluster — existing rows, cluster ids, labels
        # and hand-curation are untouched, so no frontend change is needed. Idempotent:
        # a re-run with nothing new is a no-op, so task retries are safe.
        # Must run BEFORE build_allowlist: it changes n, and feeds_health.json is index-aligned.
        py("scripts.append_layout_incremental", "--data", DATA_DIR, cwd=FEED_EXPLORER)

    @task()
    def build_allowlist():
        py("scripts.build_health_json", "--data", DATA_DIR, cwd=FEED_EXPLORER)

    @task()
    def embed_new():
        # Topic features for feeds the URI-keyed store has not seen. Local model, no API
        # calls, incremental; a feed without an embedding would land in Unsorted.
        py("scripts.embed_all_feeds", cwd=FEED_EXPLORER)

    @task()
    def topics():
        # Retrain the local classifier on the saved paid labels and label every row, then
        # merge: corrections > answer key > labeller (+ second-model agreement) > classifier.
        # Spends nothing: feeds it is unsure of stay Unsorted until a paid batch is run by hand.
        args = ("--data", DATA_DIR, "--labels", LABELS, "--old-meta", OLD_META)
        py("scripts.topics_live", *args, cwd=FEED_EXPLORER)
        py("scripts.topics_live", *args, "--merge", cwd=FEED_EXPLORER)

    @task()
    def topic_layout():
        # Topic groups over the cluster layout's positions; trending.bsky.app feeds forced
        # into Trending (Bluesky). Copies the health file built above.
        py("scripts.build_topic_layout", "--src", DATA_DIR, "--out", TOPICS_DIR,
           "--labels", LABELS, "--topics", "feed_topics_live.jsonl", cwd=FEED_EXPLORER)

    @task()
    def serve_data():
        # Drop feeds that must not be served; account status comes from sync_registry above.
        py("scripts.build_serve_data", "--src", TOPICS_DIR, "--out", SERVE_DIR, cwd=FEED_EXPLORER)

    @task()
    def publish_r2():
        # Push the served layout to Cloudflare R2, which the Pages-hosted explorer
        # (explorer.barn.city) reads from; Pages caps assets at 25 MiB, so data can't ship
        # with the bundle. Credentials come from feed-discovery/.env, which the script loads
        # itself. A missing token now fails the task: R2 is the only place the site reads.
        py("scripts.upload_data_r2", "--data", SERVE_DIR, cwd=FEED_EXPLORER)

    (sync_registry() >> reprobe() >> append_layout() >> build_allowlist()
     >> embed_new() >> topics() >> topic_layout() >> serve_data() >> publish_r2())
