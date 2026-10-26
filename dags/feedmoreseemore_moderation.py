"""
feedmoreseemore — hourly moderation refresh of the served explorer data.

Takes a feed off the site within the hour once its creator is taken down, suspended or hidden by
Bluesky moderation (or the feed is tombstoned / the account goes inactive), and puts it back once
that is reversed. The label collectors run under launchd at :00 (atproto-health launchd/); this
runs at :20, after they finish.

Deploy: symlink this file into ~/airflow/dags/, and create the shared pool once:
    airflow pools set feedmoreseemore_publish 1 "serialises data_topics/data_serve writers and R2 uploads"

Pipeline (linear):
    sync_registry -> build_serve_data -> upload_data_r2 --if-changed

Every task uses the feedmoreseemore_publish pool (1 slot), as do the liveness DAG's sync_registry,
topic_layout, serve_data and publish_r2: data_topics' four files are replaced one at a time, and
a serve build reading between those writes would misalign rows. Nothing is uploaded unless the
served files differ from the last successful upload, and upload_data_r2's build gate still runs.
"""
import os
import subprocess
from datetime import datetime

from airflow.sdk import DAG, task

REPO = os.environ.get("FEEDMORESEEMORE_DIR", "/Users/nathanbartley/srv/feedmoreseemore")
FEED_DISCOVERY = os.path.join(REPO, "feed-discovery")
FEED_EXPLORER = os.path.join(REPO, "feed-explorer")
TOPICS_DIR = os.path.join(FEED_EXPLORER, "public", "data_topics")
SERVE_DIR = os.path.join(FEED_EXPLORER, "public", "data_serve")
PYTHON = os.environ.get("FEEDMORESEEMORE_PYTHON", "/Library/Developer/CommandLineTools/usr/bin/python3")
PUBLISH_POOL = "feedmoreseemore_publish"


def py(module: str, *args: str, cwd: str = FEED_DISCOVERY) -> None:
    cmd = [PYTHON, "-W", "ignore", "-m", module, *args]
    r = subprocess.run(cmd, cwd=cwd)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} exited {r.returncode}")


with DAG(
    "feedmoreseemore_moderation",
    description="Hourly: drop feeds of moderated/inactive creators from the served explorer data",
    start_date=datetime(2026, 1, 1),
    schedule="20 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},   # the next hourly run is the retry
    tags=["feedmoreseemore", "moderation"],
) as dag:

    @task(pool=PUBLISH_POOL)
    def sync_registry():
        py("scripts.sync_registry")

    @task(pool=PUBLISH_POOL)
    def serve_data():
        py("scripts.build_serve_data", "--src", TOPICS_DIR, "--out", SERVE_DIR, cwd=FEED_EXPLORER)

    @task(pool=PUBLISH_POOL)
    def publish_r2():
        py("scripts.upload_data_r2", "--data", SERVE_DIR, "--if-changed", cwd=FEED_EXPLORER)

    sync_registry() >> serve_data() >> publish_r2()
