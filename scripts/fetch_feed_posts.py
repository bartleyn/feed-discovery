"""
Sample recent posts from each feed generator to get a CONTENT signal (topic/theme)
independent of the feed's own name/description — which is empty or <15 chars for
~32% of feeds. Feeds the content-based path of cluster_feeds_embed.py.

Source:  feed_generators.jsonl (deduped by URI) — or --uris-file.
Method:  public AppView app.bsky.feed.getFeed (unauthenticated) — returns hydrated
         posts with text. Personalized feeds return 0 posts unauthed (recorded empty).
Output:  FEEDS_DIR/feed_posts.jsonl — {"uri":..., "posts":[text,...], "n":N, "fetched_at":...}
Resume:  URIs already present in the output file are skipped (no separate cursor).

FEEDS_DIR defaults to /Volumes/miniext/feeds.

Usage:
  python -m scripts.fetch_feed_posts                       # all feeds, resumable
  python -m scripts.fetch_feed_posts --limit 5000          # sample N (proof of concept)
  python -m scripts.fetch_feed_posts --posts 25 --concurrency 20
"""

import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

FEEDS_DIR   = Path(os.environ.get("FEEDS_DIR", "/Volumes/miniext/feeds"))
JSONL_FILE  = FEEDS_DIR / "feed_generators.jsonl"
OUTPUT_FILE = FEEDS_DIR / "feed_posts.jsonl"
LOCK_FILE   = FEEDS_DIR / "fetch_feed_posts.lock"

GETFEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed"

DEFAULT_CONCURRENCY = 20
DEFAULT_POSTS       = 25
REQUEST_TIMEOUT     = int(os.environ.get("REQUEST_TIMEOUT", "12"))
MAX_RETRIES         = int(os.environ.get("MAX_RETRIES", "2"))
RETRY_DELAY         = 3


def _s(x): return x.strip() if isinstance(x, str) else ""


def acquire_lock():
    fh = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another fetch_feed_posts holds {LOCK_FILE} — refusing to double-run.")
    fh.write(f"{os.getpid()}\n"); fh.flush()
    return fh


def load_feed_uris(path: Path) -> list[str]:
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                uri = json.loads(line).get("uri", "")
            except json.JSONDecodeError:
                continue
            if uri:
                seen.add(uri)
    return sorted(seen)


def load_done(path: Path) -> set[str]:
    done = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    u = json.loads(line).get("uri")
                except json.JSONDecodeError:
                    continue
                if u:
                    done.add(u)
    return done


async def fetch_posts(session, uri: str, n: int) -> dict:
    url = f"{GETFEED}?feed={urllib.parse.quote(uri)}&limit={n}"
    for attempt in range(MAX_RETRIES):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1)); continue
                if resp.status != 200:
                    return {"uri": uri, "posts": [], "n": 0, "http": resp.status}
                data = await resp.json(content_type=None)
                texts = []
                latest = ""          # newest post the feed currently serves (appview indexedAt)
                for it in data.get("feed", []):
                    post = it.get("post", {}) or {}
                    rec = post.get("record", {}) or {}
                    t = _s(rec.get("text"))
                    if t:
                        texts.append(t)
                    # indexedAt is appview-assigned (harder to spoof than record.createdAt);
                    # max across the returned window = freshness of what the feed is emitting.
                    ix = _s(post.get("indexedAt"))
                    if ix > latest:
                        latest = ix
                return {"uri": uri, "posts": texts, "n": len(texts), "latest": latest,
                        "fetched_at": datetime.now(timezone.utc).isoformat()}
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if attempt == MAX_RETRIES - 1:
                return {"uri": uri, "posts": [], "n": 0, "err": "timeout"}
            await asyncio.sleep(RETRY_DELAY)
    return {"uri": uri, "posts": [], "n": 0, "err": "exhausted"}


async def main(args):
    lock = acquire_lock()  # noqa: F841
    all_uris = load_feed_uris(JSONL_FILE)
    done = load_done(OUTPUT_FILE)
    todo = [u for u in all_uris if u not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(all_uris):,} feeds total | {len(done):,} already fetched | {len(todo):,} to do this run")
    if not todo:
        print("Nothing to do."); return

    out = open(OUTPUT_FILE, "a", encoding="utf-8")
    sem = asyncio.Semaphore(args.concurrency)
    start = time.monotonic()
    n_done = with_text = 0

    async def worker(uri):
        nonlocal n_done, with_text
        async with sem:
            rec = await fetch_posts(session, uri, args.posts)
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        n_done += 1
        if rec["n"] > 0:
            with_text += 1
        if n_done % 500 == 0:
            out.flush()
            el = time.monotonic() - start
            rate = n_done / el if el else 0
            eta = (len(todo) - n_done) / rate / 60 if rate else 0
            print(f"  {n_done:,}/{len(todo):,} | {with_text:,} w/ posts | {rate:.0f} feed/s | ETA ~{eta:.0f}m")

    connector = aiohttp.TCPConnector(limit=args.concurrency, limit_per_host=args.concurrency, ssl=False)
    async with aiohttp.ClientSession(connector=connector,
            headers={"User-Agent": "atproto-health/feed-post-sample ntbartley@gmail.com"}) as session:
        # bounded gather in chunks to keep memory flat over 100k+ feeds
        CHUNK = 2000
        for i in range(0, len(todo), CHUNK):
            await asyncio.gather(*(worker(u) for u in todo[i:i+CHUNK]))
            out.flush()

    out.close()
    print(f"\nDone. Fetched {n_done:,} feeds, {with_text:,} had post text -> {OUTPUT_FILE}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit",       type=int, help="Only fetch N feeds this run (sample / proof of concept)")
    p.add_argument("--posts",       type=int, default=DEFAULT_POSTS, help="Posts to sample per feed")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    asyncio.run(main(p.parse_args()))
