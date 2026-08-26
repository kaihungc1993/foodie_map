"""Resolve restaurant locations to coordinates via the Google Geocoding API.

Runs at build time, never in the browser: the results are baked into
docs/data/restaurants.json so the published page needs no geocoding key and
burns no geocoding quota when people browse it.

Every lookup is cached in data/geocode_cache.json (committed), so a monthly run
only geocodes genuinely new venues.
"""
import argparse
import sys
import time

import requests

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common

ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"


def query_for(post, header):
    """Prefer the IG location tag: it is the venue as Instagram knows it, which
    geocodes far better than the free-text name in the caption."""
    tag = post.get("locationName")
    name = header.get("name")
    base = tag or name
    if not base:
        return None
    # A bare shop name is ambiguous nationwide; the account is Taipei-focused.
    return base if "台" in base or "臺" in base else f"{base} 台北"


def geocode(key, query):
    r = requests.get(ENDPOINT, params={
        "address": query, "language": "zh-TW", "region": "tw", "key": key,
    }, timeout=30)
    r.raise_for_status()
    d = r.json()
    status = d.get("status")
    if status == "OVER_QUERY_LIMIT":
        raise SystemExit(
            "Geocoding quota exhausted. Raise the daily cap in Cloud Console or "
            "wait for the quota to reset; cached results are already saved."
        )
    if status != "OK" or not d.get("results"):
        return {"status": status, "error": d.get("error_message")}
    top = d["results"][0]
    loc = top["geometry"]["location"]
    return {
        "status": "OK",
        "formatted_address": top.get("formatted_address"),
        "lat": loc["lat"],
        "lng": loc["lng"],
        "place_id": top.get("place_id"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-attempt entries that previously returned no result")
    args = ap.parse_args()

    key = common.require_env("GOOGLE_GEOCODING_KEY")
    posts = common.read_json(common.RAW_POSTS, {})
    extracted = common.read_json(common.EXTRACTED, {})
    cache = common.read_json(common.GEOCACHE, {})

    # One lookup per distinct venue, not per post — repeat visits share a key.
    wanted = {}
    for pid, post in posts.items():
        ex = extracted.get(pid)
        if not ex or not ex["llm"]["is_restaurant"]:
            continue
        key_id = str(post.get("locationId") or ex.get("name") or "")
        q = query_for(post, ex)
        if key_id and q:
            wanted.setdefault(key_id, q)

    todo = [
        (k, q) for k, q in wanted.items()
        if k not in cache or (args.retry_failed and cache[k].get("status") != "OK")
    ]
    print(f"{len(wanted)} distinct venues, {len(todo)} to geocode")

    ok = fail = 0
    for i, (k, q) in enumerate(todo, 1):
        res = geocode(key, q)
        res["query"] = q
        cache[k] = res
        if res["status"] == "OK":
            ok += 1
            print(f"  [{i}/{len(todo)}] {q} -> {res['formatted_address']}")
        else:
            fail += 1
            print(f"  [{i}/{len(todo)}] {q} -> {res['status']}", file=sys.stderr)
        if i % 25 == 0:
            common.write_json(common.GEOCACHE, cache)  # checkpoint long runs
        time.sleep(0.05)

    common.write_json(common.GEOCACHE, cache)
    print(f"geocoded {ok} ok, {fail} failed; cache holds {len(cache)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
