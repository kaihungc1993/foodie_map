"""Resolve restaurant locations to coordinates.

Uses the Places API (New) Text Search as the primary resolver, because the IG
location tag is a *business name*, not an address. The Geocoding API — which is
built for addresses — silently degrades to the city centroid when it cannot
match a business: an early run put 7 of 38 venues on the exact same point in the
middle of Taipei and reported "0 failed". Every result is now validated for
precision, and an imprecise one is a failure, not a pin.

Geocoding remains the fallback for the rare tag that is written as a real
address. Results are cached in data/geocode_cache.json (committed), so a monthly
run only looks up genuinely new venues.
"""
import argparse
import difflib
import re
import sys
import time

import requests

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Bump when the resolution logic changes so --revalidate knows what is stale.
CACHE_VERSION = 3

TAIPEI = {"latitude": 25.038, "longitude": 121.547}
BIAS_RADIUS_M = 30000.0

# A result carrying only these types is an area, not a venue.
AREA_TYPES = {
    "locality", "sublocality", "political", "country", "postal_code",
    "administrative_area_level_1", "administrative_area_level_2",
    "administrative_area_level_3", "neighborhood",
}


RE_REGION = re.compile(r"^(.{1,4}?)美食$")
TAIPEI_REGIONS = {"台北", "臺北", "新北", "北投", "士林", "內湖", "信義", "大安", "中山", "松山"}

# An IG location tag is sometimes a landmark rather than the venue: MRT stations,
# malls, districts. Those must not be used as the search text.
RE_NON_VENUE = re.compile(r"(捷運|車站|[^\s]站$|夜市$|商圈$|[縣市區]$)")


def region_hints(post):
    """He tags posts #台北美食 / #中壢美食 / #安坑美食. That suffix is the only
    reliable signal of where a venue is — he does not only cover Taipei, and a
    Taipei-biased search silently matched a Taoyuan restaurant to a Taipei one."""
    hints = []
    for tag in post.get("hashtags") or []:
        m = RE_REGION.match(tag)
        if m and m.group(1) not in ("人氣", "隱藏", "巷弄", "米其林"):
            hints.append(m.group(1))
    # Prefer the most specific hint: 安坑 localises better than 台北.
    hints.sort(key=lambda h: h in TAIPEI_REGIONS)
    return hints


def candidates_for(post, header):
    """Ordered search strings. The caption name comes first when the location tag
    is a landmark rather than a venue."""
    tag = (post.get("locationName") or "").strip()
    name = (header.get("name") or "").strip()
    hints = region_hints(post)
    region = hints[0] if hints else None

    bases = []
    if tag and RE_NON_VENUE.search(tag):
        bases = [name, tag]          # station-like tag: trust the caption
    else:
        bases = [tag, name]
    bases = [b for b in dict.fromkeys(bases) if b]
    if not bases:
        return [], None

    out = []
    for b in bases:
        if region and region not in b:
            out.append(f"{b} {region}")
        out.append(b)
    return list(dict.fromkeys(out)), (region if region in TAIPEI_REGIONS or not region else None)


def is_precise(types, formatted):
    """Reject area-level matches. A venue result always carries at least one
    non-area type (restaurant, food, point_of_interest, premise, street_address)."""
    if not formatted or len(formatted) < 10:
        return False
    t = set(types or [])
    return bool(t - AREA_TYPES)


def via_places(key, query, bias=True):
    body = {
        "textQuery": query,
        "languageCode": "zh-TW",
        "regionCode": "TW",
        "maxResultCount": 1,
    }
    # Only bias toward Taipei when the post itself points there; biasing a
    # Taoyuan venue into Taipei is how 樂魚料亭 matched the wrong restaurant.
    if bias:
        body["locationBias"] = {"circle": {"center": TAIPEI, "radius": BIAS_RADIUS_M}}
    r = requests.post(PLACES_URL, json=body, headers={
        "X-Goog-Api-Key": key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.types"
        ),
    }, timeout=30)

    if r.status_code == 403:
        raise SystemExit(
            "Places API returned 403. Enable 'Places API (New)' in the Cloud project "
            "and add it to the server key's API restrictions. See SETUP.md."
        )
    if r.status_code == 429:
        raise SystemExit("Places API quota exhausted; cached results are saved.")
    r.raise_for_status()

    places = r.json().get("places") or []
    if not places:
        return None
    p = places[0]
    loc = p.get("location") or {}
    formatted = p.get("formattedAddress")
    if not is_precise(p.get("types"), formatted):
        return None
    return {
        "status": "OK",
        "source": "places",
        "formatted_address": formatted,
        "matched_name": (p.get("displayName") or {}).get("text"),
        "lat": loc.get("latitude"),
        "lng": loc.get("longitude"),
        "place_id": p.get("id"),
    }


def via_geocoding(key, query):
    r = requests.get(GEOCODE_URL, params={
        "address": query, "language": "zh-TW", "region": "tw", "key": key,
    }, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("status") == "OVER_QUERY_LIMIT":
        raise SystemExit("Geocoding quota exhausted; cached results are saved.")
    if d.get("status") != "OK" or not d.get("results"):
        return None
    top = d["results"][0]
    formatted = top.get("formatted_address")
    if not is_precise(top.get("types"), formatted):
        return None  # the city-centroid case
    loc = top["geometry"]["location"]
    return {
        "status": "OK",
        "source": "geocoding",
        "formatted_address": formatted,
        "lat": loc["lat"],
        "lng": loc["lng"],
        "place_id": top.get("place_id"),
    }


def similarity(a, b):
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def score(matched, tag, name):
    """How well the matched venue name corresponds to what the post called it.
    Official listings are verbose (「翔鮨 Sushi Omakase 中山區無菜單日本料理」),
    so substring containment counts as a full match."""
    m = (matched or "").lower()
    if not m:
        return 0.0
    best = 0.0
    for want in (tag, name):
        w = (want or "").strip().lower()
        if not w:
            continue
        if w in m or m in w:
            return 1.0
        best = max(best, similarity(w, m))
    return best


ACCEPT = 0.55   # good enough to stop searching
REVIEW = 0.34   # below this, keep the pin but flag it for a human


def resolve(key, post, header):
    cands, bias_region = candidates_for(post, header)
    tag, name = post.get("locationName"), header.get("name")
    bias = bias_region is not None

    best = None
    for q in cands:
        res = via_places(key, q, bias=bias)
        if res is None:
            continue
        res["score"] = score(res.get("matched_name"), tag, name)
        res["query"] = q
        if best is None or res["score"] > best["score"]:
            best = res
        if res["score"] >= ACCEPT:
            break

    if best is None:
        for q in cands:
            g = via_geocoding(key, q)
            if g:
                g["score"] = 0.0
                g["query"] = q
                best = g
                break

    if best is None:
        return {"status": "NO_PRECISE_MATCH", "source": None, "score": 0.0,
                "query": cands[0] if cands else None}
    best["low_confidence"] = best["score"] < REVIEW
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-attempt venues that previously found no precise match")
    ap.add_argument("--revalidate", action="store_true",
                    help="re-resolve entries cached by an older version of this script")
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
        k = str(post.get("locationId") or ex.get("name") or "")
        if k:
            wanted.setdefault(k, (post, ex))

    def stale(k):
        if k not in cache:
            return True
        e = cache[k]
        if args.revalidate and e.get("version") != CACHE_VERSION:
            return True
        if args.retry_failed and e.get("status") != "OK":
            return True
        return False

    todo = [(k, v) for k, v in wanted.items() if stale(k)]
    print(f"{len(wanted)} distinct venues, {len(todo)} to resolve")

    ok = fail = flagged = 0
    for i, (k, (post, ex)) in enumerate(todo, 1):
        res = resolve(key, post, ex)
        res["version"] = CACHE_VERSION
        cache[k] = res
        if res["status"] == "OK":
            ok += 1
            mark = " ⚠ LOW CONFIDENCE" if res.get("low_confidence") else ""
            if res.get("low_confidence"):
                flagged += 1
            note = f" (matched 「{res['matched_name']}」)" if res.get("matched_name") else ""
            print(f"  [{i}/{len(todo)}] {res['query']} -> {res['formatted_address']}{note}{mark}")
        else:
            fail += 1
            print(f"  [{i}/{len(todo)}] {res.get('query')} -> NO PRECISE MATCH", file=sys.stderr)
        if i % 25 == 0:
            common.write_json(common.GEOCACHE, cache)  # checkpoint long runs
        time.sleep(0.05)

    common.write_json(common.GEOCACHE, cache)
    unresolved = [k for k in wanted if cache.get(k, {}).get("status") != "OK"]
    print(f"resolved {ok}, failed {fail}; cache holds {len(cache)}")
    if flagged:
        print(f"  {flagged} low-confidence matches — verify these before trusting the pin")
    if unresolved:
        print(f"  {len(unresolved)} venues without coordinates "
              f"(listed in the site but not pinned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
