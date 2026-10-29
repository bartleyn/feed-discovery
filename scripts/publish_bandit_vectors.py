"""
Publish linear-TS features: feed_vectors for the registry and user_priors for served users.

Feed vector = [PCA-32 of the feed-explorer embedding (unit), z(log1p like_count), 1].
User prior  = [mean of their liked feeds' PCA vectors / std of its cosine over the registry, 0, 0],
so the prior's embedding term is in the same z units as the offline feed_likes gate.

The PCA basis is saved beside the embedding store and reused: bandit_heads hold statistics in
its coordinates, so --refit is refused while any head exists.

    python3 -m scripts.publish_bandit_vectors --dry-run
    python3 -m scripts.publish_bandit_vectors
"""

import argparse
import json
import os
from pathlib import Path

import numpy as np
import psycopg2
import psycopg2.extras

from bandit.linear_ts import EMB_DIM

SERVING_DSN = os.environ.get("DATABASE_URL", "postgresql://feed:feed@localhost:5433/feed_discovery")
WAREHOUSE_DSN = os.environ.get(
    "ATPROTO_HEALTH_DATABASE_URL", "postgresql://nathanbartley@localhost/atproto_health"
)
STORE = Path(os.environ.get("FEEDS_DIR", "/Volumes/miniext/feeds")) / ".layout_cache" / "emb_posts_all.npy"
BASIS = STORE.with_name(f"bandit_pca{EMB_DIM}.npz")
MIN_COVERAGE = 0.9
PCA_SAMPLE = 20000


def load_store():
    uris = json.loads(STORE.with_suffix(".uris.json").read_text())
    return {u: i for i, u in enumerate(uris)}, np.load(STORE, mmap_mode="r")


def fit_basis(emb, rng):
    rows = np.sort(rng.choice(emb.shape[0], min(PCA_SAMPLE, emb.shape[0]), replace=False))
    sample = np.asarray(emb[rows], dtype=np.float64)
    sample = sample[np.abs(sample).sum(1) > 0]
    mean = sample.mean(0)
    _, _, vt = np.linalg.svd(sample - mean, full_matrices=False)
    return mean, vt[:EMB_DIM].T


def project(emb_rows, mean, basis):
    z = (np.asarray(emb_rows, dtype=np.float64) - mean) @ basis
    return z / (np.linalg.norm(z, axis=1, keepdims=True) + 1e-9)


def main(args):
    rng = np.random.default_rng(0)
    serving = psycopg2.connect(SERVING_DSN)
    cur = serving.cursor()
    cur.execute("SELECT to_regclass('bandit_heads') IS NOT NULL")
    if cur.fetchone()[0]:
        cur.execute("SELECT count(*) FROM bandit_heads")
        heads = cur.fetchone()[0]
    elif not args.dry_run:
        raise SystemExit("bandit tables missing: restart the api so create_all adds them")
    else:
        heads = 0

    row, emb = load_store()
    reuse = BASIS.exists() and not args.refit
    if reuse:
        saved = np.load(BASIS)
        mean, basis = saved["mean"], saved["basis"]
    else:
        if heads:
            raise SystemExit(f"--refit with {heads} bandit_heads rows would invalidate them")
        mean, basis = fit_basis(emb, rng)
        if not args.dry_run:
            np.savez(BASIS, mean=mean, basis=basis)

    cur.execute("SELECT feed_uri, coalesce(like_count, 0) FROM feeds")
    registry = [(u, n) for u, n in cur.fetchall() if u in row]
    cur.execute("SELECT count(*) FROM feeds")
    total = cur.fetchone()[0]
    if len(registry) < MIN_COVERAGE * total:
        raise SystemExit(f"only {len(registry)}/{total} registry feeds in {STORE.name}")

    feed_uris = [u for u, _ in registry]
    feed_emb = project(emb[[row[u] for u in feed_uris]], mean, basis)
    log_likes = np.log1p([n for _, n in registry])
    pop = (log_likes - log_likes.mean()) / (log_likes.std() + 1e-9)
    feed_x = np.column_stack([feed_emb, pop, np.ones(len(feed_uris))])

    cur.execute("SELECT DISTINCT user_did FROM impressions")
    users = [r[0] for r in cur.fetchall()]
    with psycopg2.connect(WAREHOUSE_DSN) as wh, wh.cursor() as wcur:
        wcur.execute(
            "SELECT liker_did, feed_uri FROM feeds.feed_likes WHERE liker_did = ANY(%s)", (users,)
        )
        likes: dict[str, list[str]] = {}
        for did, uri in wcur.fetchall():
            if uri in row:
                likes.setdefault(did, []).append(uri)

    priors = []
    for did, uris in likes.items():
        direction = project(emb[sorted(row[u] for u in uris)], mean, basis).mean(0)
        scaled = direction / (np.std(feed_emb @ direction) + 1e-9)
        priors.append((did, np.concatenate([scaled, [0.0, 0.0]]).tolist(), len(uris)))

    if not (np.isfinite(feed_x).all() and all(np.isfinite(v).all() for _, v, _ in priors)):
        raise SystemExit("non-finite feature values; check the embedding store and basis")
    print(f"feed vectors: {len(feed_uris)}/{total} registry feeds, dim {feed_x.shape[1]}")
    print(f"user priors: {len(priors)}/{len(users)} served users have liked feeds in the store")
    print(f"bandit_heads rows: {heads}; basis {'reused' if reuse else 'fitted'}")
    if args.dry_run:
        return

    cur.execute("DELETE FROM feed_vectors")
    psycopg2.extras.execute_values(
        cur, "INSERT INTO feed_vectors (feed_uri, vec, published_at) VALUES %s",
        [(u, x.tolist()) for u, x in zip(feed_uris, feed_x)], template="(%s, %s, now())",
    )
    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO user_priors (user_did, vec, n_likes, published_at) VALUES %s
           ON CONFLICT (user_did) DO UPDATE
           SET vec = EXCLUDED.vec, n_likes = EXCLUDED.n_likes, published_at = now()""",
        priors, template="(%s, %s, %s, now())",
    )
    serving.commit()
    print("published")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="compute and report, write nothing")
    parser.add_argument("--refit", action="store_true", help="refit the PCA basis (needs empty bandit_heads)")
    main(parser.parse_args())
