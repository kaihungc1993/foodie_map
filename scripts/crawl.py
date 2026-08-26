"""Fetch posts from Instagram via the Apify Instagram Scraper.

Incremental by default: pulls the newest `--limit` posts and merges them into
data/raw_posts.json, keyed by post id. Use --backfill for the full history.
"""
import argparse
import sys
import time

import requests

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common

ACTOR = "apify~instagram-scraper"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"

# Fields we keep. The actor returns a lot more (image CDN urls that expire,
# comment threads); storing only these keeps raw_posts.json reviewable in a diff.
KEEP = [
    "id", "shortCode", "url", "caption", "timestamp", "type",
    "locationName", "locationId", "hashtags", "likesCount", "commentsCount",
    "ownerUsername", "displayUrl",
]


def fetch(token, limit):
    payload = {
        "directUrls": [f"https://www.instagram.com/{common.IG_ACCOUNT}/"],
        "resultsType": "posts",
        "resultsLimit": limit,
        "addParentData": False,
    }
    for attempt in range(3):
        r = requests.post(ENDPOINT, params={"token": token}, json=payload, timeout=900)
        if r.status_code < 400:
            return r.json()
        # 429/5xx from Apify are transient; a failed actor run is not billed for
        # results it did not return, so retrying is cheap.
        if r.status_code in (429, 500, 502, 503) and attempt < 2:
            wait = 30 * (attempt + 1)
            print(f"apify {r.status_code}, retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        raise SystemExit(f"Apify failed {r.status_code}: {r.text[:400]}")
    raise SystemExit("Apify failed after retries")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40,
                    help="how many recent posts to pull (default 40)")
    ap.add_argument("--backfill", action="store_true",
                    help="pull the full history instead of the recent window")
    args = ap.parse_args()

    token = common.require_env("APIFY_TOKEN")
    limit = 1000 if args.backfill else args.limit

    print(f"fetching up to {limit} posts from @{common.IG_ACCOUNT} ...")
    items = fetch(token, limit)
    print(f"apify returned {len(items)} posts")

    existing = common.read_json(common.RAW_POSTS, {})
    new_ids = []
    for it in items:
        pid = it.get("id")
        if not pid:
            continue
        slim = {k: it.get(k) for k in KEEP}
        if pid not in existing:
            new_ids.append(pid)
        existing[pid] = slim

    common.write_json(common.RAW_POSTS, existing)
    print(f"stored {len(existing)} posts total, {len(new_ids)} new")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
