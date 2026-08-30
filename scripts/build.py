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


# The Michelin-style roundup grades map onto his usual 0-5 scale so those venues
# can be filtered alongside everything else. Only ever applied when he never gave
# the place a real score — Kira bistro and AMA Labo are ★★★ and were later rated
# 4.8 and 4.7 in their own posts, and those stated numbers win.
GUIDE_STAR_RATING = {1: 4.3, 2: 4.4, 3: 4.6}


def venue_key(name):
    return "rv:" + re.sub(r"[\s·•・.,、，\-_()（）｜|/]+", "", (name or "").lower())


def norm_name(s):
    return re.sub(r"[\s·•・.,、，\-_()（）]+", "", (s or "").lower())


def group_key(post, ex):
    lid = post.get("locationId")
    # A landmark tag is shared by every restaurant near it, so grouping on it
    # merges unrelated venues — fall back to the name written in the caption.
    if lid and not common.is_landmark(post.get("locationName")):
        return f"loc:{lid}"
    n = norm_name(ex.get("name") or post.get("locationName"))
    return f"name:{n}" if n else None


def reviews_from_posts(post_list, primary):
    """Per-reviewer aggregates, and the top-level projection the map reads.

    Every subjective field lives under its author. Combining them across authors
    is what this exists to prevent: a visit count of 24 belongs to whoever went
    24 times, not to the restaurant, and two reviewers use differently calibrated
    rating scales. Venue facts (coordinates, hours, categories) stay top level.

    Derived from the posts list rather than computed in three places, so the
    initial build, the duplicate merge and the roundup attach cannot drift apart.
    """
    # A roundup mention is a name-check inside a post about five restaurants, not
    # a visit to this one. It must not supply the headline verdict or count as a
    # trip — but it is all a roundup-only restaurant has, hence the fallback.
    ordered = sorted(post_list, key=lambda x: x.get("timestamp") or "", reverse=True)
    reviews = {}
    for p in [x for x in ordered if not x.get("roundup")] + \
             [x for x in ordered if x.get("roundup")]:
        acc = p.get("source_account")
        if not acc:
            continue
        r = reviews.setdefault(acc, {
            "rating": None, "rating_axes": {}, "guide_stars": None, "visits": None,
            "tagline": None, "last_visit": None, "visited_on": None,
            "price_min": None, "price_max": None, "post_count": 0, "extras": {},
        })
        r["post_count"] += 1
        # Posts are newest-first, so the first non-null wins for "latest" fields.
        if r["last_visit"] is None:
            r["last_visit"] = p.get("timestamp")
        for field in ("rating", "tagline", "guide_stars"):
            if r[field] is None and p.get(field) is not None:
                r[field] = p[field]
        if not r["rating_axes"] and p.get("rating_axes"):
            r["rating_axes"] = p["rating_axes"]
        if r["price_min"] is None and p.get("price_min") is not None:
            r["price_min"], r["price_max"] = p["price_min"], p["price_max"]
        extras = p.get("extras") or {}
        for k, v in extras.items():
            if v is not None and r["extras"].get(k) is None:
                r["extras"][k] = v
        if r["visited_on"] is None:
            r["visited_on"] = extras.get("visited_on")

    for acc, r in reviews.items():
        mine = [p for p in post_list if p.get("source_account") == acc]
        stated = [p["visits"] for p in mine if p.get("visits") is not None]
        # Only ever within one account: two reviewers each saying 3訪 is not 6.
        # With nothing stated, count the posts he actually wrote about the place;
        # a restaurant known only from roundup mentions has no visit count at all.
        real = sum(1 for p in mine if not p.get("roundup"))
        r["visits"] = max(stated) if stated else (real or None)
    return reviews


def project(reviews, primary):
    """Flatten the primary reviewer's view up to the top level.

    The map, the filters and the sort still read flat fields; which reviewer they
    describe is recorded in `rating_source` so nothing reads as anonymous fact.
    """
    order = ([primary] if primary in reviews else []) + \
            [a for a in sorted(reviews) if a != primary]
    out = {"rating": None, "rating_source": None, "guide_stars": None,
           "visits": None, "tagline": None, "last_visit": None,
           "price_min": None, "price_max": None}
    for acc in order:
        r = reviews[acc]
        for field in ("rating", "guide_stars", "visits", "tagline", "last_visit"):
            if out[field] is None and r.get(field) is not None:
                out[field] = r[field]
                if field == "rating":
                    out["rating_source"] = acc
        if out["price_min"] is None and r.get("price_min") is not None:
            out["price_min"], out["price_max"] = r["price_min"], r["price_max"]
    return out


def apply_reviews(r, primary):
    """Recompute a restaurant's reviews block and flat projection from its posts."""
    r["reviews"] = reviews_from_posts(r["posts"], primary)
    r["accounts"] = sorted(r["reviews"])
    r["post_count"] = len(r["posts"])
    r.update(project(r["reviews"], primary))
    return r


def tier_of(rating, cuts):
    """Which tier a rating falls in, as an index from 0 (lowest). Tiers are
    per-account: the two reviewers' scales are not interchangeable, so a shared
    threshold would put one of them entirely in the bottom bands."""
    if rating is None:
        return None
    t = 0
    for c in cuts:
        if rating >= c:
            t += 1
    return t


def account_tiers(calibration, account):
    """Frozen cut points for an account. Absent means that reviewer has not been
    calibrated yet — his ratings still show, but ungraded, rather than being
    silently graded on someone else's scale."""
    entry = (calibration.get("accounts") or {}).get(account) or {}
    cuts = entry.get("cuts") or []
    labels = entry.get("labels") or []
    return cuts, labels, entry.get("top_label"), entry.get("hints") or []


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

    # Ids already on the published site. A restaurant id is the key for
    # docs/data/notes.json and for ?r= deep links, so once an id exists it must
    # survive every later build — including one where a second reviewer's newer
    # post would otherwise win the dedupe and re-key the restaurant.
    # Every id ever published, accumulated in a committed ledger. Reading them
    # back out of restaurants.json would not work: that file is overwritten each
    # build, so an id dropped once is invisible on the next run and can never be
    # redirected again.
    ledger = common.read_json(common.ID_LEDGER, {"ids": []})
    known_ids = set(ledger.get("ids", [])) | {
        r["id"] for r in common.read_json(common.RESTAURANTS, {}).get("restaurants", [])}
    primary = common.primary_account()

    calibration = common.read_json(common.CONFIG / "rating_calibration.json", {})

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
        # A roundup names several restaurants, so attributing the whole post to
        # the first 📍 would invent one restaurant and drop the rest. It is
        # skipped here and expanded venue-by-venue further down instead.
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

        g = geo.get(common.geo_key(newest_post, newest_ex)) or {}
        if g.get("status") != "OK":
            missing_geo.append(gk)

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
        # The name shown is the PRIMARY account's spelling. Taking it from
        # whoever posted most recently would rename a few hundred restaurants to
        # the second reviewer's spelling the moment his posts land.
        primary_entries = [e for e in entries
                           if e[1].get("ownerUsername") == primary] or entries
        name_post, name_ex = primary_entries[0][1], primary_entries[0][2]
        caption_name = name_ex.get("name")
        tag_name = name_post.get("locationName")
        display = caption_name or tag_name
        override = overrides.get(gk) or overrides.get(display)
        if override:
            display, caption_name = override, display

        aka = []
        for alt in (caption_name, tag_name,
                    newest_ex.get("name"), newest_post.get("locationName")):
            if alt and norm_name(alt) != norm_name(display) and alt not in aka:
                aka.append(alt)

        restaurants.append({
            "id": gk,
            "from_roundup": False,
            "guide_stars": None,
            "name": display,
            "aka": aka,
            "location_tag": tag_name,
            "address": g.get("formatted_address"),
            "lat": g.get("lat"),
            "lng": g.get("lng"),
            "place_id": g.get("place_id"),
            "categories": cats,
            "posts": [{
                "id": pid,
                "url": p.get("url"),
                "shortcode": p.get("shortCode"),
                "timestamp": p.get("timestamp"),
                "caption": p.get("caption"),
                "tagline": x.get("tagline"),
                "rating": x.get("rating"),
                "rating_axes": x.get("rating_axes") or {},
                "visits": x.get("visits"),
                "price_min": x.get("price_min"),
                "price_max": x.get("price_max"),
                "dishes": x.get("dishes"),
                "extras": x.get("extras") or {},
                "likes": p.get("likesCount"),
                "source_account": p.get("ownerUsername"),
            } for pid, p, x in entries],
        })
        apply_reviews(restaurants[-1], primary)

    # Instagram sometimes carries two location ids for one venue (users can each
    # create a place entry), which split 4 restaurants into duplicate pins. Same
    # name at the same coordinates is the same restaurant — merge without asking.
    # Distinct venues that share coordinates (food halls) keep different names,
    # so they are untouched and stay in the suggestion list.
    # Places assigns one id per business, so two entries sharing a place_id are
    # the same restaurant however differently he spelled it (巴黎廳1930 /
    # Paris 1930, 焼鳥まこ / Yakitori MAKO). Checked against the whole dataset:
    # all 54 shared ids were genuine duplicates, and distinct restaurants at one
    # address — 東京。烟火気 and 新美香咖哩 on 延吉街 — hold different ids.
    merged_dupes = []
    by_key = {}
    for r in restaurants:
        if r.get("place_id"):
            k = ("pid", r["place_id"])
        elif r["lat"] is not None:
            k = ("geo", round(r["lat"], 5), round(r["lng"], 5), norm_name(r["name"]))
        else:
            continue
        by_key.setdefault(k, []).append(r)

    def keeper_priority(r):
        """Which record survives a merge, and therefore which id the restaurant
        keeps. History first: an id already published must never change, because
        it is the key for notes.json and for ?r= deep links. Then the primary
        account, so a second reviewer cannot re-key an existing restaurant."""
        return (0 if r["id"] in known_ids else 1,
                0 if any(p.get("source_account") == primary for p in r["posts"]) else 1)

    for group in by_key.values():
        if len(group) < 2:
            continue
        # Two stable passes: recency (and id, to break exact ties deterministically)
        # first, then priority — so among equally-privileged records the newest
        # still wins, exactly as before.
        group.sort(key=lambda r: (r["last_visit"] or "", r["id"]), reverse=True)
        group.sort(key=keeper_priority)
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
        apply_reviews(keep, primary)

    # ---- roundup mentions -------------------------------------------------
    # Roundup posts ("五家台北漢堡店") name several venues with no per-venue IG
    # location tag, so they are matched to existing restaurants by Places id.
    # 20 of the 75 mentions are places that already have their own review post;
    # those become an extra post entry rather than a duplicate pin.
    venue_cls = common.read_json(common.VENUES, {})
    hours_cache = common.read_json(common.HOURS, {})
    by_place = {r["place_id"]: r for r in restaurants if r.get("place_id")}
    added, attached = 0, 0

    for pid, post in posts.items():
        ex = extracted.get(pid)
        if not ex or (ex.get("pin_count") or 0) <= 1:
            continue
        for v in ex.get("venues") or []:
            key = venue_key(v.get("name"))
            cls = venue_cls.get(key)
            if not cls or not cls.get("is_restaurant"):
                continue
            g = geo.get(key) or {}
            if g.get("status") != "OK":
                continue

            mention = {
                "id": f"{pid}:{key}",
                "url": post.get("url"),
                "shortcode": post.get("shortCode"),
                "timestamp": post.get("timestamp"),
                "caption": post.get("caption"),
                "tagline": v.get("blurb") or None,
                "rating": v.get("rating"),
                "guide_stars": v.get("guide_stars"),
                "rating_axes": {},
                "visits": v.get("visits"),
                "price_min": None,
                "price_max": None,
                "dishes": [],
                "extras": {},
                "likes": post.get("likesCount"),
                "source_account": post.get("ownerUsername"),
                # Flags the entry as a roundup mention: no rating, no dish list,
                # and the caption covers several restaurants, not just this one.
                "roundup": True,
                "roundup_title": (post.get("caption") or "").splitlines()[0][:60],
            }

            host = by_place.get(g.get("place_id"))
            if host:
                host["posts"].append(mention)
                host["posts"].sort(key=lambda x: x.get("timestamp") or "", reverse=True)
                apply_reviews(host, primary)
                attached += 1
                continue

            r = {
                "id": key,
                "name": v["name"],
                "aka": [],
                "location_tag": None,
                "address": g.get("formatted_address"),
                "lat": g.get("lat"),
                "lng": g.get("lng"),
                "place_id": g.get("place_id"),
                "categories": cls.get("categories") or [],
                # Whatever a roundup does not carry stays null rather than being
                # guessed; the site renders those as 未評分 / —.
                "from_roundup": True,
                "posts": [mention],
            }
            apply_reviews(r, primary)
            restaurants.append(r)
            if r["place_id"]:
                by_place[r["place_id"]] = r
            added += 1

    # The Michelin-style roundup grade is mapped onto the 0-5 scale *within the
    # reviewer who wrote it*, not at the top level: the site reads scores out of
    # each reviewer's own block, so a derivation that only lands on the flat
    # field leaves that restaurant showing as unrated.
    for r in restaurants:
        for acc, rev in r["reviews"].items():
            if rev.get("guide_stars") is None:
                rev["guide_stars"] = next(
                    (p["guide_stars"] for p in r["posts"]
                     if p.get("source_account") == acc and p.get("guide_stars")), None)
            if rev.get("rating") is None and rev.get("guide_stars"):
                rev["rating"] = GUIDE_STAR_RATING.get(rev["guide_stars"])
                rev["rating_derived"] = True
            else:
                rev.setdefault("rating_derived", False)
        r.update(project(r["reviews"], primary))
        r["guide_stars"] = next(
            (r["reviews"][a].get("guide_stars") for a in r["accounts"]
             if r["reviews"][a].get("guide_stars")), None)
        r["rating_derived"] = any(r["reviews"][a].get("rating_derived")
                                  for a in r["accounts"])

    # Opening hours reduced to the question actually asked of them: lunch, dinner,
    # days shut — plus whether Google thinks the place has closed for good.
    from hours import summarise
    for r in restaurants:
        h = hours_cache.get(r.get("place_id") or "") or {}
        r.update(summarise(h))
        r["business_status"] = h.get("business_status")

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

    for r in restaurants:
        for acc, rev in r["reviews"].items():
            cuts, _, _, _ = account_tiers(calibration, acc)
            rev["tier"] = tier_of(rev.get("rating"), cuts) if cuts else None
        primary_cuts, _, _, _ = account_tiers(calibration, r.get("rating_source") or primary)
        r["tier"] = tier_of(r["rating"], primary_cuts) if primary_cuts else None

    accounts_block = []
    for a in common.accounts():
        acc = a["username"]
        cuts, labels, top_label, hints = account_tiers(calibration, acc)
        rated = [rr["reviews"][acc]["rating"] for rr in restaurants
                 if acc in rr["reviews"] and rr["reviews"][acc].get("rating") is not None]
        counts = [0] * (len(cuts) + 1)
        for v in rated:
            counts[tier_of(v, cuts)] += 1
        accounts_block.append({
            "username": acc,
            "label": a.get("label") or acc,
            "url": a.get("url"),
            "primary": bool(a.get("primary")),
            "restaurant_count": sum(1 for rr in restaurants if acc in rr["reviews"]),
            "rated_count": len(rated),
            "cuts": cuts,
            "labels": labels,
            "top_label": top_label,
            "hints": hints,
            "tier_counts": counts,
        })

    # Ids that existed on the published site but no longer do — a roundup-only
    # venue absorbed into a real post's record, or a merge. Notes and ?r= links
    # are keyed on the id, so the site redirects through this map rather than
    # silently losing them.
    live_ids = {r["id"] for r in restaurants}
    id_aliases = {d["merged"]: d["kept"] for d in merged_dupes if d["merged"] not in live_ids}
    by_place_now = {r["place_id"]: r["id"] for r in restaurants if r.get("place_id")}
    for gone in known_ids - live_ids:
        if gone in id_aliases:
            continue
        # A roundup venue key resolves through its geocoded place.
        g = geo.get(gone) or {}
        target = by_place_now.get(g.get("place_id"))
        if target:
            id_aliases[gone] = target
    orphaned = sorted(known_ids - live_ids - set(id_aliases))
    common.write_json(common.ID_LEDGER,
                      {"_comment": "Every restaurant id ever published. Append-only: "
                                   "notes.json and ?r= links are keyed on these, so a "
                                   "retired id must stay resolvable through id_aliases.",
                       "ids": sorted(known_ids | live_ids)})

    # Cross-check: one reviewer types the street address into his caption, and
    # Google resolved the venue independently. Where the two disagree on the
    # house number, one of them is wrong — this is how the earlier landmark
    # mis-pins (信義安和站, 國父紀念館, BURGER OUT in California) surfaced.
    conflicts = []
    num = re.compile(r"(\d+)\s*(?:之\d+)?\s*號")
    for r in restaurants:
        said = next((v.get("extras", {}).get("address_text")
                     for v in r["reviews"].values()
                     if (v.get("extras") or {}).get("address_text")), None)
        got = r.get("address")
        if not said or not got or said.startswith("預約"):
            continue
        a, b = num.search(said), num.search(got)
        if a and b and a.group(1) != b.group(1):
            conflicts.append({"id": r["id"], "name": r["name"],
                              "caption_says": said, "google_says": got})
    common.write_json(common.DATA / "source_conflicts.json",
                      sorted(conflicts, key=lambda c: c["name"]))

    all_cats = sorted({c for r in restaurants for c in r["categories"]})
    closed = sum(1 for r in restaurants if r["business_status"] == "CLOSED_PERMANENTLY")
    # The heavy per-post fields — full captions, dish lists, the caption's own
    # address/hours/phone — are only ever read when a detail panel is open, and
    # together they are ~63% of the file. Splitting them out keeps the payload a
    # phone downloads and parses on first paint to what the map actually needs.
    heavy = {}
    for r in restaurants:
        for post in r["posts"]:
            detail = {k: post.pop(k) for k in ("caption", "dishes", "extras")
                      if k in post}
            if any(detail.values()):
                heavy[post["id"]] = detail
    common.write_json(common.DOCS_DATA / "posts.json", heavy)

    common.write_json(common.RESTAURANTS, {
        "generated_from_posts": len(posts),
        "restaurant_count": len(restaurants),
        "closed_count": closed,
        "primary_account": primary,
        "accounts": accounts_block,
        "id_aliases": id_aliases,
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
        common.write_json(common.DATA / "roundup_posts.json",
                          sorted(roundups, key=lambda r: r["date"]))

    # Rename *candidates*: the venue Places matched bears little resemblance to
    # what he called it. Usually a verbose listing, occasionally a real rename —
    # a human decides, and records it in config/name_overrides.json.
    candidates = []
    for gk, entries in groups.items():
        newest_post, newest_ex = entries[0][1], entries[0][2]
        gg = geo.get(common.geo_key(newest_post, newest_ex)) or {}
        if gg.get("low_confidence") and gg.get("matched_name"):
            candidates.append({"id": gk, "posted_as": newest_ex.get("name"),
                               "places_says": gg["matched_name"],
                               "address": gg.get("formatted_address")})
    common.write_json(common.DATA / "rename_candidates.json",
                      sorted(candidates, key=lambda c: c["posted_as"] or ""))

    applied = sum(1 for r in restaurants
                  if overrides.get(r["id"]) or any(overrides.get(a) for a in r["aka"]))
    print(f"{len(restaurants)} restaurants from {len(groups)} groups")
    if id_aliases:
        print(f"  {len(id_aliases)} retired ids redirected (notes and links follow)")
    if orphaned:
        print(f"  WARNING: {len(orphaned)} published ids vanished with no target: "
              f"{orphaned[:5]}")
    print(f"  roundups: {added} venues added, {attached} mentions attached to "
          f"restaurants that already had their own post")
    if merged_dupes:
        print(f"  {len(merged_dupes)} duplicate location ids merged: "
              + ", ".join(sorted({d['name'] for d in merged_dupes})))
    print(f"  {len(candidates)} rename candidates -> data/rename_candidates.json"
          f"  ({applied} renames applied from config/name_overrides.json)")
    if roundups:
        total = sum(r["venues"] for r in roundups)
        print(f"  {len(roundups)} roundup posts, {total} venue mentions "
              f"-> data/roundup_posts.json")
    print(f"  {len(missing_geo)} without coordinates, "
          f"{skipped_no_location} posts skipped (no location tag or name)")
    print(f"  {len(merges['suggestions'])} proximity merge suggestions -> data/merges.json")
    lunch = sum(1 for r in restaurants if r["serves_lunch"])
    dinner = sum(1 for r in restaurants if r["serves_dinner"])
    nohours = sum(1 for r in restaurants if r["serves_lunch"] is None)
    print(f"  hours: {lunch} serve lunch, {dinner} serve dinner, {nohours} unknown")
    print(f"  {closed} closed permanently")
    if conflicts:
        print(f"  {len(conflicts)} address disagreements between caption and Google "
              f"-> data/source_conflicts.json")
    print(f"  categories in use: {len(all_cats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
