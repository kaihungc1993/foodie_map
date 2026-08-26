"""Aggregate posts into one record per restaurant for the website.

Grouping is keyed on the Instagram location id. That id is stable across posts,
so repeat visits collapse correctly even when the caption spells the name
differently from the location tag (post DcN40K1DwpE writes
"德榮軒脆皮鰻魚飯專賣店" while its tag reads "德榮軒 初•一脆皮鰻魚飯專門店" —
name matching would have split that into two pins).

Posts with no location tag fall back to a normalised caption name. Venues that
land within ~50m of each other are reported as merge suggestions in
data/merges.json rather than merged automatically, because department store
food halls legitimately share coordinates.
"""
import math
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common

NEAR_METRES = 50


def norm_name(s):
    return re.sub(r"[\s·•・.,、，\-_()（）]+", "", (s or "").lower())


def group_key(post, ex):
    lid = post.get("locationId")
    if lid:
        return f"loc:{lid}"
    n = norm_name(ex.get("name") or post.get("locationName"))
    return f"name:{n}" if n else None


def haversine(a, b):
    R = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def main():
    posts = common.read_json(common.RAW_POSTS, {})
    extracted = common.read_json(common.EXTRACTED, {})
    geo = common.read_json(common.GEOCACHE, {})
    merges = common.read_json(common.MERGES, {"merge": [], "split": [], "suggestions": []})

    # Manual overrides: map any key in a merge cluster onto the cluster's first key.
    alias = {}
    for cluster in merges.get("merge", []):
        for k in cluster[1:]:
            alias[k] = cluster[0]

    groups = {}
    skipped_no_location = 0
    for pid, post in posts.items():
        ex = extracted.get(pid)
        if not ex or not ex["llm"]["is_restaurant"]:
            continue
        gk = group_key(post, ex)
        if not gk:
            skipped_no_location += 1
            continue
        gk = alias.get(gk, gk)
        groups.setdefault(gk, []).append((pid, post, ex))

    restaurants = []
    missing_geo = []
    for gk, entries in groups.items():
        # Newest first — "newest rating" and "# of visited" come from the top entry.
        entries.sort(key=lambda e: e[1].get("timestamp") or "", reverse=True)
        newest_post, newest_ex = entries[0][1], entries[0][2]

        geo_key = str(newest_post.get("locationId") or newest_ex.get("name") or "")
        g = geo.get(geo_key) or {}
        if g.get("status") != "OK":
            missing_geo.append(gk)

        rating = next((e[2]["rating"] for e in entries if e[2]["rating"] is not None), None)
        price = next(
            ((e[2]["price_min"], e[2]["price_max"]) for e in entries
             if e[2]["price_min"] is not None), (None, None))

        # Prefer his stated visit count; fall back to how many posts we grouped,
        # which undercounts when several visits share one post.
        stated = [e[2]["visits"] for e in entries if e[2]["visits"] is not None]
        visits = max(stated) if stated else len(entries)

        cats = []
        for e in entries:
            for c in e[2]["llm"]["categories"]:
                if c not in cats:
                    cats.append(c)

        restaurants.append({
            "id": gk,
            "name": newest_ex.get("name") or newest_post.get("locationName"),
            "location_tag": newest_post.get("locationName"),
            "address": g.get("formatted_address"),
            "lat": g.get("lat"),
            "lng": g.get("lng"),
            "place_id": g.get("place_id"),
            "categories": cats,
            "rating": rating,
            "visits": visits,
            "price_min": price[0],
            "price_max": price[1],
            "tagline": newest_ex.get("tagline"),
            "last_visit": newest_post.get("timestamp"),
            "post_count": len(entries),
            "posts": [{
                "id": pid,
                "url": p.get("url"),
                "shortcode": p.get("shortCode"),
                "timestamp": p.get("timestamp"),
                "caption": p.get("caption"),
                "tagline": x.get("tagline"),
                "rating": x.get("rating"),
                "visits": x.get("visits"),
                "dishes": x.get("dishes"),
                "likes": p.get("likesCount"),
                "source_account": p.get("ownerUsername"),
            } for pid, p, x in entries],
        })

    restaurants.sort(key=lambda r: r["last_visit"] or "", reverse=True)

    # Proximity suggestions for human review — never applied automatically.
    located = [r for r in restaurants if r["lat"] is not None]
    suggestions = []
    for i in range(len(located)):
        for j in range(i + 1, len(located)):
            a, b = located[i], located[j]
            if {a["id"], b["id"]} & set(merges.get("split", [])):
                continue
            if any({a["id"], b["id"]} <= set(c) for c in merges.get("merge", [])):
                continue
            d = haversine((a["lat"], a["lng"]), (b["lat"], b["lng"]))
            if d <= NEAR_METRES:
                suggestions.append({
                    "distance_m": round(d, 1),
                    "a": {"id": a["id"], "name": a["name"]},
                    "b": {"id": b["id"], "name": b["name"]},
                })
    merges["suggestions"] = sorted(suggestions, key=lambda s: s["distance_m"])
    common.write_json(common.MERGES, merges)

    all_cats = sorted({c for r in restaurants for c in r["categories"]})
    common.write_json(common.RESTAURANTS, {
        "generated_from_posts": len(posts),
        "restaurant_count": len(restaurants),
        "categories": all_cats,
        "restaurants": restaurants,
    })

    if not common.NOTES.exists():
        common.write_json(common.NOTES, {})

    key = common.require_env("GOOGLE_MAPS_BROWSER_KEY")
    # Browser keys are public by design; this one is referrer-restricted to the
    # Pages domain and capped by a daily quota (see SETUP.md).
    common.write_json(common.DOCS_DATA / "config.json", {
        "mapsBrowserKey": key,
        "repo": {
            "owner": "kaihungc1993",
            "name": "foodie_map",
            "branch": "main",
            "notesPath": "docs/data/notes.json",
        },
    })

    print(f"{len(restaurants)} restaurants from {len(groups)} groups")
    print(f"  {len(missing_geo)} without coordinates, "
          f"{skipped_no_location} posts skipped (no location tag or name)")
    print(f"  {len(merges['suggestions'])} proximity merge suggestions -> data/merges.json")
    print(f"  categories in use: {len(all_cats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
