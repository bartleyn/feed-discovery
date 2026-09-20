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

# Unauth reads hit the public appview; authed reads go through the PDS, which proxies to the
# appview WITH the viewer context so personalized feeds resolve (they return 0 unauthed).
PUBLIC_GETFEED = "https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed"
PDS_HOST       = os.environ.get("PDS_HOST", "https://bsky.social")
AUTH_GETFEED   = f"{PDS_HOST}/xrpc/app.bsky.feed.getFeed"
CREATE_SESSION = f"{PDS_HOST}/xrpc/com.atproto.server.createSession"
REFRESH_SESSION= f"{PDS_HOST}/xrpc/com.atproto.server.refreshSession"

DEFAULT_CONCURRENCY = 20
DEFAULT_POSTS       = 25
REQUEST_TIMEOUT     = int(os.environ.get("REQUEST_TIMEOUT", "12"))
MAX_RETRIES         = int(os.environ.get("MAX_RETRIES", "2"))
RETRY_DELAY         = 3


def _s(x): return x.strip() if isinstance(x, str) else ""


def _load_dotenv(path: Path) -> dict:
    """Minimal KEY=VALUE .env parser — avoids a pydantic-settings dependency in this
    plain-python crawl script (the project's api.config isn't importable here)."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Auth:
    """App-password session with lazy, single-flight token refresh.

    accessJwt is short-lived (~hours) and a 57K-feed crawl outlives it, so on an
    ExpiredToken we refresh once — the lock ensures concurrent workers that all
    hit the expired token trigger exactly one refresh, then reuse the new token.
    """
    def __init__(self, handle: str, password: str):
        self.handle = handle
        self.password = password
        self.access = self.refresh = self.did = None
        self.lock = asyncio.Lock()

    async def login(self, session):
        async with session.post(CREATE_SESSION,
                                 json={"identifier": self.handle, "password": self.password}) as r:
            if r.status != 200:
                raise SystemExit(f"createSession failed ({r.status}): {await r.text()}")
            d = await r.json()
            self.access, self.refresh, self.did = d["accessJwt"], d["refreshJwt"], d["did"]
            print(f"Authenticated as {self.handle} ({self.did})")

    async def refresh_token(self, session, stale_access):
        async with self.lock:
            if self.access != stale_access:      # another worker already refreshed
                return
            async with session.post(REFRESH_SESSION,
                                     headers={"Authorization": f"Bearer {self.refresh}"}) as r:
                if r.status == 200:
                    d = await r.json()
                    self.access, self.refresh = d["accessJwt"], d["refreshJwt"]
                else:                             # refresh token dead too -> full re-login
                    await self.login(session)

    def header(self):
        return {"Authorization": f"Bearer {self.access}"} if self.access else {}


def acquire_lock(path=LOCK_FILE):
    fh = open(path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"Another fetch_feed_posts holds {path} — refusing to double-run.")
    fh.write(f"{os.getpid()}\n"); fh.flush()
    return fh


def load_empty(path: Path) -> list[str]:
    """URIs from a prior output file that returned NO posts (n==0) — the personalized/empty
    feeds the auth pass should retry with a viewer context."""
    empty = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("uri") and r.get("n", 0) == 0:
                empty.add(r["uri"])
    return sorted(empty)


def load_uris_plain(path: Path) -> list[str]:
    """One URI per line, no JSON — what the posts-refresh DAG hands in via --uris-file so
    the fetch always targets the CURRENT online set instead of the frozen feed_generators.jsonl
    snapshot (which was ~11k feeds behind the live registry as of 2026-08)."""
    seen = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            uri = line.strip()
            if uri:
                seen.add(uri)
    return sorted(seen)


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


async def fetch_posts(session, uri: str, n: int, auth: "Auth | None" = None) -> dict:
    base = AUTH_GETFEED if auth else PUBLIC_GETFEED
    url = f"{base}?feed={urllib.parse.quote(uri)}&limit={n}"
    for attempt in range(MAX_RETRIES):
        try:
            headers = auth.header() if auth else {}
            async with session.get(url, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1)); continue
                # authed access token expired mid-crawl -> refresh once, retry same attempt budget
                if auth and resp.status in (400, 401):
                    body = await resp.json(content_type=None)
                    if isinstance(body, dict) and body.get("error") in ("ExpiredToken", "InvalidToken"):
                        await auth.refresh_token(session, headers.get("Authorization", "").removeprefix("Bearer "))
                        continue
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
    output_file = Path(args.output) if args.output else OUTPUT_FILE
    lock = acquire_lock(output_file.with_suffix(".lock"))  # noqa: F841

    if args.empty_from:                       # auth pass: retry only the feeds that came back empty
        source = load_empty(Path(args.empty_from))
        src_label = f"{len(source):,} empty feeds from {args.empty_from}"
    elif args.uris_file:
        source = load_uris_plain(Path(args.uris_file))
        src_label = f"{len(source):,} feeds from {args.uris_file}"
    else:
        source = load_feed_uris(JSONL_FILE)
        src_label = f"{len(source):,} feeds total"
    # --refresh treats `output_file` as a rewrite target, not an append-forever log: a URI
    # already in it is due for a NEW sample, not a skip. Without this, a scheduled refresh
    # would fetch each feed exactly once, ever, then no-op on every later run — the same
    # staleness bug just fixed for feed_generators, reproduced here for post content.
    done = set() if args.refresh else load_done(output_file)
    todo = [u for u in source if u not in done]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{src_label} | {len(done):,} already fetched | {len(todo):,} to do this run")
    if not todo:
        print("Nothing to do."); return

    auth = None
    if args.auth:
        env = _load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        handle = os.environ.get("ATPROTO_HANDLE") or env.get("ATPROTO_HANDLE")
        password = os.environ.get("ATPROTO_PASSWORD") or env.get("ATPROTO_PASSWORD")
        if not handle or not password:
            sys.exit("--auth needs ATPROTO_HANDLE/ATPROTO_PASSWORD (env or feed-discovery/.env).")
        auth = Auth(handle, password)

    # --refresh writes to a sibling .tmp file and only replaces output_file once every feed
    # is done, so a crash or interrupt mid-run (this is a multi-hour job) leaves the previous
    # cycle's data intact instead of a half-refreshed file — a partial rewrite would silently
    # blank out every feed that hadn't been reached yet.
    write_target = output_file.with_suffix(output_file.suffix + ".tmp") if args.refresh else output_file
    out = open(write_target, "w" if args.refresh else "a", encoding="utf-8")
    sem = asyncio.Semaphore(args.concurrency)
    start = time.monotonic()
    n_done = with_text = 0

    async def worker(uri):
        nonlocal n_done, with_text
        async with sem:
            rec = await fetch_posts(session, uri, args.posts, auth)
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
        if auth:
            await auth.login(session)
        # bounded gather in chunks to keep memory flat over 100k+ feeds
        CHUNK = 2000
        for i in range(0, len(todo), CHUNK):
            await asyncio.gather(*(worker(u) for u in todo[i:i+CHUNK]))
            out.flush()

    out.close()
    if args.refresh:
        write_target.replace(output_file)
    print(f"\nDone. Fetched {n_done:,} feeds, {with_text:,} had post text -> {output_file}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit",       type=int, help="Only fetch N feeds this run (sample / proof of concept)")
    p.add_argument("--posts",       type=int, default=DEFAULT_POSTS, help="Posts to sample per feed")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    p.add_argument("--auth",        action="store_true",
                   help="Authenticate (app password from ATPROTO_HANDLE/ATPROTO_PASSWORD) so personalized feeds resolve")
    p.add_argument("--empty-from",  help="Only retry URIs that returned 0 posts in this prior output file")
    p.add_argument("--uris-file",   help="Plain text, one URI per line — overrides the feed_generators.jsonl universe")
    p.add_argument("--refresh",     action="store_true",
                   help="Re-fetch every URI in the source (not just ones missing from output), "
                        "rewriting output_file atomically on completion instead of skip-if-present")
    p.add_argument("--output",      help="Output JSONL (default feed_posts.jsonl); auth pass should use a separate file")
    asyncio.run(main(p.parse_args()))
