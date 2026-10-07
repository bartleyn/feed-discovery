"""
feedmoreseemore — feed-explorer liveness refresh DAG.

Runs on the existing Airflow (~/airflow) alongside the atproto-health DAGs. Re-probes
every feed in the current explorer layout, persists health to Postgres, regenerates the
liveness allow-list the SPA reads, and deploys it — so the map only shows feeds verified
live recently (offline/decayed feeds stop rendering without a full re-cluster).

Deploy: symlink or copy this file into ~/airflow/dags/ (matches how atproto-health does it).
Set FEEDMORESEEMORE_DIR if the repo isn't at the default path below.

Pipeline (linear):
    sync_registry -> reprobe_liveness -> append_layout_incremental -> build_health_json -> deploy

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
DIST_DIR = os.path.join(REPO, "feed-explorer", "dist", "data")

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
    def deploy():
        # Static assets; copy into the built bundle the site serves (explorer.barn.city
        # serves dist/ via `vite preview` under launchd job city.barn.feedexplorer).
        # All four move now, not just the allow-list — append_layout rewrites the layout,
        # meta and index too, and they must stay index-aligned with each other.
        import gzip

        if not os.path.isdir(DIST_DIR):
            return
        for name in ("feeds_layout.bin", "feeds_meta.json",
                     "feeds_index.json", "feeds_health.json"):
            src = os.path.join(DATA_DIR, name)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(DIST_DIR, name)
            subprocess.run(["cp", src, dst], check=True)
            # The Vite build emits a .gz sibling for hosts serving precompressed assets
            # (nginx gzip_static / Caddy). Refresh it here too, or they'd keep serving
            # data from the last full build instead of this run's.
            with open(dst, "rb") as fh:
                raw = fh.read()
            with gzip.open(f"{dst}.gz", "wb", compresslevel=9) as fh:
                fh.write(raw)

    @task()
    def publish_r2():
        # Push the same four artifacts to Cloudflare R2, which is where the Pages-hosted
        # explorer reads them from (Pages caps assets at 25 MiB; feeds_meta.json is 23MB
        # and growing, so data can't ship with the bundle).
        #
        # Runs ALONGSIDE deploy() rather than replacing it, deliberately: until DNS moves
        # explorer.barn.city from the tunnel to Pages, the launchd `vite preview` job is
        # still the live site and must keep getting fresh data. Drop deploy() after cutover.
        #
        # Credentials come from feed-discovery/.env, which the script loads itself — the
        # same convention every other script here uses. Don't gate on os.environ from this
        # process: an Airflow task inherits the scheduler's environment, and the scheduler
        # is a launchd agent that has never read .env, so checking here would always skip.
        # --skip-if-unconfigured lets the script exit 0 when no token is present, so this
        # task stays green while the tunnel is still the live site.
        py("scripts.upload_data_r2", "--data", DATA_DIR, "--skip-if-unconfigured",
           cwd=FEED_EXPLORER)

    sync_registry() >> reprobe() >> append_layout() >> build_allowlist() >> deploy() >> publish_r2()
