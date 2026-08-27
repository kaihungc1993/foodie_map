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

    # {group id or old name: current name} — hand-maintained, verified renames only.
    overrides = common.read_json(common.CONFIG / "name_overrides.json", {})
    overrides = {k: v for k, v in overrides.items() if not k.startswith("_")}

    groups = {}
    skipped_no_location = 0
    roundups = []
    for pid, post in posts.items():
        ex = extracted.get(pid)
        if not ex or not ex["llm"]["is_restaurant"]:
            continue
        # Roundup posts name several restaurants; attributing the whole post to
        # the first one would be wrong. Held out until they are split properly.
        if (ex.get("pin_count") or 0) > 1:
            roundups.append({"id": pid, "url": post.get("url"),
                             "date": (post.get("timestamp") or "")[:10],
                             "venues": ex["pin_count"],
                             "title": (post.get("caption") or "").splitlines()[0][:60]})
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

        # Venues get renamed — 小隱茶庵 信義店 now trades as 木下庵 Kino at the same
        # address — but Places' registered name is usually just the caption name
        # plus SEO padding ("米釉麻辣鍋物｜大巨蛋美食｜大安區美食"). Adopting it
        # wholesale made 306 of 478 names worse, so renames are applied only from
        # config/name_overrides.json, and everything else keeps the name he wrote.
        caption_name = newest_ex.get("name")
        tag_name = newest_post.get("locationName")
        display = caption_name or tag_name
        override = overrides.get(gk) or overrides.get(display)
        if override:
            display, caption_name = override, display

        aka = []
        for alt in (caption_name, tag_name):
            if alt and norm_name(alt) != norm_name(display) and alt not in aka:
                aka.append(alt)

        restaurants.append({
            "id": gk,
            "name": display,
            "aka": aka,
            "location_tag": tag_name,
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

    # Instagram sometimes carries two location ids for one venue (users can each
    # create a place entry), which split 4 restaurants into duplicate pins. Same
    # name at the same coordinates is the same restaurant — merge without asking.
    # Distinct venues that share coordinates (food halls) keep different names,
    # so they are untouched and stay in the suggestion list.
    merged_dupes = []
    by_key = {}
    for r in restaurants:
        if r["lat"] is None:
            continue
        k = (round(r["lat"], 5), round(r["lng"], 5), norm_name(r["name"]))
        by_key.setdefault(k, []).append(r)

    for group in by_key.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: r["last_visit"] or "", reverse=True)
        keep, drop = group[0], group[1:]
        for d in drop:
            keep["posts"].extend(d["posts"])
            for c in d["categories"]:
                if c not in keep["categories"]:
                    keep["categories"].append(c)
            for a in d.get("aka", []):
                if a not in keep["aka"]:
                    keep["aka"].append(a)
            merged_dupes.append({"kept": keep["id"], "merged": d["id"], "name": keep["name"]})
            restaurants.remove(d)
        keep["posts"].sort(key=lambda p: p.get("timestamp") or "", reverse=True)
        keep["post_count"] = len(keep["posts"])
        stated = [p["visits"] for p in keep["posts"] if p.get("visits") is not None]
        keep["visits"] = max(stated) if stated else keep["post_count"]
        newest = keep["posts"][0]
        keep["last_visit"] = newest.get("timestamp")
        rating = next((p["rating"] for p in keep["posts"] if p.get("rating") is not None), None)
        keep["rating"] = rating
        keep["tagline"] = newest.get("tagline") or keep["tagline"]

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

    if roundups:
        common.write_json(common.DATA / "roundups_held_out.json",
                          sorted(roundups, key=lambda r: r["date"]))

    # Rename *candidates*: the venue Places matched bears little resemblance to
    # what he called it. Usually a verbose listing, occasionally a real rename —
    # a human decides, and records it in config/name_overrides.json.
    candidates = []
    for gk, entries in groups.items():
        newest_post, newest_ex = entries[0][1], entries[0][2]
        gkey = str(newest_post.get("locationId") or newest_ex.get("name") or "")
        gg = geo.get(gkey) or {}
        if gg.get("low_confidence") and gg.get("matched_name"):
            candidates.append({"id": gk, "posted_as": newest_ex.get("name"),
                               "places_says": gg["matched_name"],
                               "address": gg.get("formatted_address")})
    common.write_json(common.DATA / "rename_candidates.json",
                      sorted(candidates, key=lambda c: c["posted_as"] or ""))

    applied = sum(1 for r in restaurants
                  if overrides.get(r["id"]) or any(overrides.get(a) for a in r["aka"]))
    print(f"{len(restaurants)} restaurants from {len(groups)} groups")
    if merged_dupes:
        print(f"  {len(merged_dupes)} duplicate location ids merged: "
              + ", ".join(sorted({d['name'] for d in merged_dupes})))
    print(f"  {len(candidates)} rename candidates -> data/rename_candidates.json"
          f"  ({applied} renames applied from config/name_overrides.json)")
    if roundups:
        total = sum(r["venues"] for r in roundups)
        print(f"  {len(roundups)} roundup posts held out "
              f"({total} venue mentions) -> data/roundups_held_out.json")
    print(f"  {len(missing_geo)} without coordinates, "
          f"{skipped_no_location} posts skipped (no location tag or name)")
    print(f"  {len(merges['suggestions'])} proximity merge suggestions -> data/merges.json")
    print(f"  categories in use: {len(all_cats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
