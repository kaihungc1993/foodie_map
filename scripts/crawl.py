"""Fetch posts from Instagram via the Apify Instagram Scraper.

Incremental by default: pulls the newest `--limit` posts and merges them into
data/raw_posts.json, keyed by post id. Use --backfill for the full history.
"""
import argparse
import collections
import sys
import time

import requests

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common

ACTOR = "apify~instagram-scraper"
ENDPOINT = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"

# Fields we keep. The actor returns a lot more (image CDN urls that expire,
# comment threads); storing only these keeps raw_posts.json reviewable in a diff.
# `displayUrl` is deliberately absent: Instagram hands back a signed CDN URL whose
# host and tokens rotate on every fetch, so keeping it rewrote 40 lines of
# raw_posts.json on every run and produced a "refresh" commit with no real change.
# It also expires, so it was never usable on the site.
KEEP = [
    "id", "shortCode", "url", "caption", "timestamp", "type",
    "locationName", "locationId", "hashtags", "likesCount", "commentsCount",
    "ownerUsername",
]


def fetch(token, account, limit):
    """One actor run per account.

    Deliberately not one run with several directUrls: `resultsLimit` is a total
    for the run, so a combined call would spend the whole budget on whichever
    account the actor walked first and starve the rest.
    """
    payload = {
        "directUrls": [f"https://www.instagram.com/{account}/"],
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
    ap.add_argument("--account", action="append",
                    help="crawl only this account (repeatable; default: all)")
    args = ap.parse_args()

    token = common.require_env("APIFY_TOKEN")
    limit = 1000 if args.backfill else args.limit

    known = common.account_names()
    targets = args.account or known
    unknown = [a for a in targets if a not in known]
    if unknown:
        raise SystemExit(f"unknown account(s) {unknown}; known: {known}")

    existing = common.read_json(common.RAW_POSTS, {})
    total_new = 0
    for account in targets:
        print(f"fetching up to {limit} posts from @{account} ...")
        items = fetch(token, account, limit)
        print(f"  apify returned {len(items)} posts")

        new_ids = []
        for it in items:
            pid = it.get("id")
            if not pid:
                continue
            # Posts are keyed on the IG post id, which is globally unique, so
            # accounts share one file without namespacing. ownerUsername is the
            # only provenance and every downstream stage keys off it.
            if not it.get("ownerUsername"):
                print(f"  skipping {pid}: no ownerUsername", file=sys.stderr)
                continue
            slim = {k: it.get(k) for k in KEEP}
            if pid not in existing:
                new_ids.append(pid)
            existing[pid] = slim
        print(f"  {len(new_ids)} new from @{account}")
        total_new += len(new_ids)

    common.write_json(common.RAW_POSTS, existing)
    by_account = collections.Counter(p.get("ownerUsername") for p in existing.values())
    print(f"stored {len(existing)} posts total, {total_new} new")
    for account, n in sorted(by_account.items()):
        print(f"  @{account}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
