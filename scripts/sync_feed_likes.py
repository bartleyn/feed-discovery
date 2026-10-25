"""
Mirror per-feed like counts out of atproto-health into feedmoreseemore.

atproto-health is the WRITER: feeds.feed_likes holds one row per (feed, liker), backfilled
from getLikes and kept current by the jetstream collector (likes and unlikes). The count of
those rows is the like count; it runs ~97% of bsky.app's number for the biggest feeds, since
getLikes hides likes from deleted/suspended accounts. feedmoreseemore keeps a read copy so
the explorer pipeline never reads another project's database except here (as sync_registry).

Full-scan upsert: one GROUP BY over ~1.6M edges, ~70k result rows. synced_at on a row moves
only when its count changes; sync_runs records when the sync itself last ran.

    python3 -m scripts.sync_feed_likes --dry-run
    python3 -m scripts.sync_feed_likes
"""

from __future__ import annotations  # pinned interpreter is 3.9

import argparse
import os

import psycopg2
import psycopg2.extras

SRC_DSN = os.environ.get(
    "ATPROTO_HEALTH_DATABASE_URL", "postgresql://nathanbartley@localhost/atproto_health"
)
DST_DSN = os.environ.get(
    "HEALTH_DATABASE_URL", "postgresql://nathanbartley@localhost/feedmoreseemore"
)
BATCH = 5000

DDL = """
CREATE TABLE IF NOT EXISTS feed_like_counts (
    feed_uri   text PRIMARY KEY,
    like_count integer     NOT NULL,
    synced_at  timestamptz NOT NULL DEFAULT now()   -- when like_count last changed
);
CREATE TABLE IF NOT EXISTS sync_runs (
    name      text PRIMARY KEY,
    synced_at timestamptz NOT NULL,
    rows      integer     NOT NULL
);
"""

UPSERT = """
INSERT INTO feed_like_counts (feed_uri, like_count) VALUES %s
ON CONFLICT (feed_uri) DO UPDATE SET like_count = EXCLUDED.like_count, synced_at = now()
WHERE feed_like_counts.like_count IS DISTINCT FROM EXCLUDED.like_count
RETURNING (xmax = 0) AS inserted
"""


def sync(dry_run: bool = False) -> None:
    with psycopg2.connect(SRC_DSN, connect_timeout=10) as src, src.cursor() as scur:
        scur.execute("SELECT feed_uri, count(*)::int FROM feeds.feed_likes GROUP BY feed_uri")
        rows = scur.fetchall()
    total = sum(c for _, c in rows)
    if dry_run:
        print(f"--dry-run: {len(rows):,} feeds, {total:,} likes would be synced")
        return

    with psycopg2.connect(DST_DSN, connect_timeout=10) as dst, dst.cursor() as cur:
        cur.execute(DDL)
        ins = upd = 0
        for i in range(0, len(rows), BATCH):
            got = psycopg2.extras.execute_values(cur, UPSERT, rows[i:i + BATCH], page_size=BATCH, fetch=True)
            ins += sum(1 for r in got if r[0])
            upd += sum(1 for r in got if not r[0])
        # feeds that lost every like no longer appear in the GROUP BY
        cur.execute("CREATE TEMP TABLE src_uris (feed_uri text PRIMARY KEY) ON COMMIT DROP")
        psycopg2.extras.execute_values(cur, "INSERT INTO src_uris VALUES %s", [(u,) for u, _ in rows], page_size=BATCH)
        cur.execute("""UPDATE feed_like_counts f SET like_count = 0, synced_at = now()
                       WHERE f.like_count <> 0 AND NOT EXISTS (SELECT 1 FROM src_uris s WHERE s.feed_uri = f.feed_uri)""")
        zeroed = cur.rowcount
        cur.execute("""INSERT INTO sync_runs (name, synced_at, rows) VALUES ('feed_like_counts', now(), %s)
                       ON CONFLICT (name) DO UPDATE SET synced_at = EXCLUDED.synced_at, rows = EXCLUDED.rows""",
                    (len(rows),))
    print(f"feeds {len(rows):,} | likes {total:,} | inserted {ins:,} | updated {upd:,} | zeroed {zeroed:,}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    sync(dry_run=p.parse_args().dry_run)
