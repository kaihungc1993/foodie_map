"""Fetch opening hours and business status from Google Places.

Deliberately separate from geocode.py because these fields sit in the Places
**Enterprise** SKU, whose free allowance is 1,000 calls/month — a tenth of the
Essentials tier. Results are cached by place_id and never re-fetched unless
--refresh is passed, so the ~550-venue backfill is a one-time spend and monthly
runs only touch new places.

The raw weekly schedule is reduced to what is actually asked of it: does this
place serve lunch, does it serve dinner, and which days is it shut.
"""
import argparse
import sys
import time

import requests

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common

DETAILS_URL = "https://places.googleapis.com/v1/places/{}"
FIELDS = "id,businessStatus,regularOpeningHours"

LUNCH_MIN = 12 * 60          # noon
DINNER_MIN = 19 * 60         # 7pm


def fetch(key, place_id):
    r = requests.get(DETAILS_URL.format(place_id),
                     params={"fields": FIELDS, "languageCode": "zh-TW",
                             "regionCode": "TW"},
                     headers={"X-Goog-Api-Key": key,
                              "X-Goog-FieldMask": FIELDS},
                     timeout=30)
    if r.status_code == 403:
        raise SystemExit(
            "Places returned 403. The server key needs 'Places API (New)' enabled. "
            "See SETUP.md."
        )
    if r.status_code == 429:
        raise SystemExit("Places Enterprise quota exhausted; cached results are saved.")
    if r.status_code == 404:
        return {"status": "NOT_FOUND"}
    r.raise_for_status()
    d = r.json()
    return {
        "status": "OK",
        "business_status": d.get("businessStatus"),
        "periods": (d.get("regularOpeningHours") or {}).get("periods"),
        "weekday_text": (d.get("regularOpeningHours") or {}).get("weekdayDescriptions"),
    }


def covers(period, day, minute):
    """Does this opening period cover `minute` on weekday `day` (0=Sunday)?"""
    o, c = period.get("open"), period.get("close")
    if not o:
        return False
    if not c:                                   # open-ended entry means 24 hours
        return o.get("day") == day
    start = o["day"] * 1440 + o.get("hour", 0) * 60 + o.get("minute", 0)
    end = c["day"] * 1440 + c.get("hour", 0) * 60 + c.get("minute", 0)
    if end <= start:                            # wraps past midnight into next week
        end += 7 * 1440
    t = day * 1440 + minute
    return start <= t < end or start <= t + 7 * 1440 < end


def summarise(entry):
    """Reduce a week of periods to lunch / dinner / closed-day counts."""
    periods = entry.get("periods")
    if entry.get("status") != "OK" or not periods:
        return {"serves_lunch": None, "serves_dinner": None,
                "lunch_days": None, "dinner_days": None, "closed_days": None}

    lunch = sum(1 for d in range(7) if any(covers(p, d, LUNCH_MIN) for p in periods))
    dinner = sum(1 for d in range(7) if any(covers(p, d, DINNER_MIN) for p in periods))
    openday = sum(1 for d in range(7)
                  if any(covers(p, d, LUNCH_MIN) or covers(p, d, DINNER_MIN)
                         for p in periods))
    return {
        "serves_lunch": lunch > 0,
        "serves_dinner": dinner > 0,
        "lunch_days": lunch,
        "dinner_days": dinner,
        "closed_days": 7 - openday,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch every place (spends the Enterprise quota again)")
    ap.add_argument("--limit", type=int, help="cap how many places to fetch this run")
    args = ap.parse_args()

    key = common.require_env("GOOGLE_GEOCODING_KEY")
    geo = common.read_json(common.GEOCACHE, {})
    cache = common.read_json(common.HOURS, {})

    place_ids = sorted({v["place_id"] for v in geo.values()
                        if v.get("status") == "OK" and v.get("place_id")})
    todo = [p for p in place_ids if args.refresh or p not in cache]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(place_ids)} places, {len(todo)} to fetch "
          f"(Enterprise SKU — 1,000 free per month)")
    if len(todo) > 900:
        print("  refusing: that would blow the monthly free allowance in one run",
              file=sys.stderr)
        return 1

    ok = miss = 0
    for i, pid in enumerate(todo, 1):
        cache[pid] = fetch(key, pid)
        if cache[pid]["status"] == "OK" and cache[pid].get("periods"):
            ok += 1
        else:
            miss += 1
        if i % 50 == 0:
            common.write_json(common.HOURS, cache)
            print(f"  {i}/{len(todo)}")
        time.sleep(0.04)

    common.write_json(common.HOURS, cache)

    lunch = dinner = 0
    for v in cache.values():
        sm = summarise(v)
        lunch += 1 if sm["serves_lunch"] else 0
        dinner += 1 if sm["serves_dinner"] else 0
    print(f"fetched {ok} with hours, {miss} without; cache holds {len(cache)}")
    print(f"  serves lunch: {lunch}   serves dinner: {dinner}")
    closed = [p for p, v in cache.items() if v.get("business_status") == "CLOSED_PERMANENTLY"]
    if closed:
        print(f"  {len(closed)} marked CLOSED_PERMANENTLY by Google")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
