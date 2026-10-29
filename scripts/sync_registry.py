"""
Mirror the feed-generator registry out of atproto-health into feedmoreseemore.

The map pipeline currently reads its candidate set straight out of ANOTHER project's
database: append_layout_incremental and reprobe_liveness both open ATPROTO_HEALTH_DATABASE_URL
and query activity.feed_generators. That coupling is the thing blocking containerization —
feedmoreseemore can't move off this machine while a required table lives in a database owned
by a service that OOMs every day or two (collect-activity, exit 134). It also means a
collector restart mid-DAG can fail a task that has nothing to do with the collector.

So: keep atproto-health as the WRITER (the jetstream collector upserts it continuously) and
give feedmoreseemore its own read replica of that one table. Every consumer then points at
feedmoreseemore.feed_generators and the cross-database read happens exactly once, here, in a
task that can be retried on its own.

Full-scan upsert rather than an incremental watermark. The source has no updated_at, and rows
mutate in place — a #putRecord edits display_name/description, a #deleteRecord sets deleted_at —
so a watermark over first_seen would copy new feeds and silently miss every edit and every
tombstone. At 148k rows a full pass costs a couple of seconds; incremental would buy nothing
and lose correctness.

Idempotent: synced_at only moves for rows that actually changed (the ON CONFLICT carries a
WHERE), so re-running is a no-op and reports 0/0.

    python3 -m scripts.sync_registry --dry-run
    python3 -m scripts.sync_registry
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

COLS = ("uri", "creator_did", "display_name", "description", "service_did", "first_seen", "deleted_at")
BATCH = 5000

# creator's account status from atproto-health's PDS scan; NULL = active (the scan lists only inactive repos).
# A status counts only if scanned on the creator's CURRENT PDS: migrated accounts read "deactivated" on the old one.
SOURCE = """
SELECT {}, s.status AS creator_status, f.skyfeed_builder::text AS skyfeed_builder,
       m.action AS creator_mod_action, fl.labels AS feed_labels
FROM activity.feed_generators f
LEFT JOIN plc.plc_did_pds p ON p.did = f.creator_did
LEFT JOIN plc.did_repo_status s
       ON s.did = f.creator_did AND rtrim(s.pds_url, '/') = rtrim(p.pds_url, '/')
-- creator's strongest live Bluesky moderation action (atproto-health 015); NULL = none
LEFT JOIN LATERAL (
    SELECT a.action FROM plc.bsky_mod_actions a
    WHERE a.did = f.creator_did AND (a.expires_at IS NULL OR a.expires_at > now())
    ORDER BY array_position(ARRAY['!takedown', '!suspend', '!hide'], a.action)
    LIMIT 1
) m ON true
-- live Bluesky moderation labels on the feed itself (porn, sexual, !takedown, ...; atproto-health 019); NULL = none
LEFT JOIN LATERAL (
    SELECT array_agg(l.label ORDER BY l.label) AS labels FROM plc.bsky_mod_feed_labels l
    WHERE l.uri = f.uri AND (l.expires_at IS NULL OR l.expires_at > now())
) fl ON true
""".format(", ".join("f." + c for c in COLS))

DDL = """
CREATE TABLE IF NOT EXISTS feed_generators (
    uri          text PRIMARY KEY,
    creator_did  text        NOT NULL,
    display_name text,
    description  text,
    first_seen   text        NOT NULL,
    deleted_at   timestamptz,
    synced_at    timestamptz NOT NULL DEFAULT now()
);
-- service_did arrived after the table did (see CONSTRUCTION-METHOD-PLAN.md's Layer A —
-- the serving DID is a builder-platform/engine signal, e.g. shared across every SkyFeed
-- feed) — added in place rather than forcing a migration step, same convention
-- reprobe_liveness.py already uses for latest_post_at on feed_health.
ALTER TABLE feed_generators ADD COLUMN IF NOT EXISTS service_did text;
-- deactivated / takendown / deleted / ...; NULL when the creator's account is active
ALTER TABLE feed_generators ADD COLUMN IF NOT EXISTS creator_status text;
-- SkyFeed recipe (inputs, regex filters, sort); JSON null = read, none; NULL = not read (atproto-health 014)
ALTER TABLE feed_generators ADD COLUMN IF NOT EXISTS skyfeed_builder jsonb;
-- !takedown / !suspend / !hide from mod.bsky.app; NULL when the creator has none
ALTER TABLE feed_generators ADD COLUMN IF NOT EXISTS creator_mod_action text;
-- mod.bsky.app labels on the feed itself (porn, sexual, nudity, !takedown, ...); NULL when it has none
ALTER TABLE feed_generators ADD COLUMN IF NOT EXISTS feed_labels text[];
CREATE INDEX IF NOT EXISTS idx_feed_generators_creator
    ON feed_generators (creator_did);
-- Every consumer filters `deleted_at IS NULL`; a partial index keeps that scan off the
-- ~2% of rows that are tombstones.
CREATE INDEX IF NOT EXISTS idx_feed_generators_live
    ON feed_generators (uri) WHERE deleted_at IS NULL;
-- Engine-lookup queries (GROUP BY service_did ORDER BY count(*) DESC) are the whole
-- point of mirroring this column — index it the same way creator_did already is.
CREATE INDEX IF NOT EXISTS idx_feed_generators_service
    ON feed_generators (service_did);
"""

# WHERE clause matters: without it every row rewrites on every run, synced_at loses all
# meaning as a change signal, and the table churns 148k dead tuples per sync for nothing.
UPSERT = """
INSERT INTO feed_generators
    (uri, creator_did, display_name, description, service_did, first_seen, deleted_at, creator_status, skyfeed_builder,
     creator_mod_action, feed_labels)
VALUES %s
ON CONFLICT (uri) DO UPDATE SET
    creator_did    = EXCLUDED.creator_did,
    display_name   = EXCLUDED.display_name,
    description    = EXCLUDED.description,
    service_did    = EXCLUDED.service_did,
    first_seen     = EXCLUDED.first_seen,
    deleted_at     = EXCLUDED.deleted_at,
    creator_status = EXCLUDED.creator_status,
    skyfeed_builder = EXCLUDED.skyfeed_builder,
    creator_mod_action = EXCLUDED.creator_mod_action,
    feed_labels    = EXCLUDED.feed_labels,
    synced_at      = now()
WHERE (feed_generators.creator_did, feed_generators.display_name, feed_generators.description,
       feed_generators.service_did, feed_generators.first_seen, feed_generators.deleted_at,
       feed_generators.creator_status, feed_generators.skyfeed_builder, feed_generators.creator_mod_action,
       feed_generators.feed_labels)
   IS DISTINCT FROM
      (EXCLUDED.creator_did, EXCLUDED.display_name, EXCLUDED.description,
       EXCLUDED.service_did, EXCLUDED.first_seen, EXCLUDED.deleted_at,
       EXCLUDED.creator_status, EXCLUDED.skyfeed_builder, EXCLUDED.creator_mod_action,
       EXCLUDED.feed_labels)
RETURNING (xmax = 0) AS inserted
"""


def sync(dry_run: bool = False, prune: bool = False) -> None:
    with psycopg2.connect(DST_DSN, connect_timeout=10) as dst:
        with dst.cursor() as cur:
            cur.execute(DDL)
            cur.execute("SELECT count(*) FROM feed_generators")
            before = cur.fetchone()[0]
        dst.commit()

        src = psycopg2.connect(SRC_DSN, connect_timeout=10)
        try:
            # Named cursor => server-side; streams in itersize chunks instead of
            # materializing 148k rows with full descriptions in this process.
            with src.cursor(name="registry_scan") as scur:
                scur.itersize = BATCH
                scur.execute(SOURCE)

                if dry_run:
                    n = sum(1 for _ in scur)
                    print("--dry-run: {:,} source rows would be upserted into {:,} existing"
                          .format(n, before))
                    return

                seen, ins, upd = 0, 0, 0
                with dst.cursor() as cur:
                    batch = []
                    for row in scur:
                        batch.append(row)
                        if len(batch) >= BATCH:
                            i, u = _flush(cur, batch)
                            ins += i
                            upd += u
                            seen += len(batch)
                            batch = []
                    if batch:
                        i, u = _flush(cur, batch)
                        ins += i
                        upd += u
                        seen += len(batch)
                dst.commit()
        finally:
            src.close()

        gone = 0
        if prune:
            # Hard deletes shouldn't happen — the collector tombstones via deleted_at rather
            # than DELETE — so this stays opt-in. If the source ever loses rows to a bad
            # migration, an unguarded prune would mirror that damage straight into the map's
            # candidate set.
            with dst.cursor() as cur:
                cur.execute("SELECT uri FROM feed_generators")
                local = {r[0] for r in cur}
            with src.cursor() as scur:
                scur.execute("SELECT uri FROM activity.feed_generators")
                remote = {r[0] for r in scur}
            missing = local - remote
            if missing:
                with dst.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur, "DELETE FROM feed_generators WHERE uri IN (VALUES %s)",
                        [(u,) for u in missing], page_size=BATCH)
                gone = len(missing)
            dst.commit()

        with dst.cursor() as cur:
            cur.execute("SELECT count(*) FILTER (WHERE deleted_at IS NULL), count(*), "
                        "count(*) FILTER (WHERE deleted_at IS NULL AND creator_status IS NOT NULL), "
                        "count(*) FILTER (WHERE deleted_at IS NULL AND creator_mod_action IS NOT NULL), "
                        "count(*) FILTER (WHERE deleted_at IS NULL AND feed_labels IS NOT NULL) "
                        "FROM feed_generators")
            live, total, inactive, moderated, labelled = cur.fetchone()

    print("scanned {:,} | inserted {:,} | updated {:,}{}".format(
        seen, ins, upd, " | pruned {:,}".format(gone) if prune else ""))
    print("feedmoreseemore.feed_generators: {:,} rows ({:,} live, {:,} tombstoned; "
          "{:,} live feeds from inactive accounts, {:,} from moderated accounts, {:,} with Bluesky feed labels)"
          .format(total, live, total - live, inactive, moderated, labelled))


def _flush(cur, batch):
    """Upsert one batch; returns (inserted, updated). Rows that didn't change return nothing."""
    rows = psycopg2.extras.execute_values(cur, UPSERT, batch, page_size=BATCH, fetch=True)
    ins = sum(1 for r in rows if r[0])
    return ins, len(rows) - ins


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--prune", action="store_true",
                   help="delete local rows absent from the source (see comment in sync(); "
                        "the collector tombstones rather than deletes, so this is rarely right)")
    args = p.parse_args()
    sync(dry_run=args.dry_run, prune=args.prune)
