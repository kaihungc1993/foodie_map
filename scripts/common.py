"""Shared paths, env loading, and JSON helpers."""
import json
import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS_DATA = ROOT / "docs" / "data"
CONFIG = ROOT / "config"

RAW_POSTS = DATA / "raw_posts.json"
EXTRACTED = DATA / "extracted.json"
GEOCACHE = DATA / "geocode_cache.json"
VENUES = DATA / "roundup_venues.json"
HOURS = DATA / "hours_cache.json"
MERGES = DATA / "merges.json"
ID_LEDGER = DATA / "id_ledger.json"
RESTAURANTS = DOCS_DATA / "restaurants.json"
NOTES = DOCS_DATA / "notes.json"

ACCOUNTS_CONFIG = CONFIG / "accounts.json"


def accounts():
    """The crawled Instagram accounts, in config order."""
    return [a for a in read_json(ACCOUNTS_CONFIG, {}).get("accounts", [])
            if not a.get("username", "").startswith("_")]


def account_names():
    return [a["username"] for a in accounts()]


def primary_account():
    """Whose spelling of a venue name wins, and which account the site opens on.

    Load-bearing for id stability: the display name and the dedupe keeper are
    both anchored to the primary account so a second reviewer's newer post
    cannot silently rename or re-key an existing restaurant.
    """
    accs = accounts()
    primary = [a["username"] for a in accs if a.get("primary")]
    if len(primary) != 1:
        raise SystemExit(
            f"config/accounts.json must mark exactly one account primary, found {primary}")
    return primary[0]


def load_env():
    """Load .env.local if present. CI supplies these as real env vars instead."""
    envfile = ROOT / ".env.local"
    if not envfile.exists():
        return
    for line in envfile.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def require_env(name):
    load_env()
    val = os.environ.get(name)
    if not val:
        raise SystemExit(
            f"Missing {name}. Set it in .env.local (local) or as a GitHub Actions secret (CI). "
            f"See SETUP.md."
        )
    return val


def read_json(path, default):
    p = pathlib.Path(path)
    if not p.exists():
        return default
    txt = p.read_text(encoding="utf-8").strip()
    if not txt:
        return default
    return json.loads(txt)


def write_json(path, obj):
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # sort_keys keeps diffs small and stable so the commit-back step only
    # shows genuine data changes.
    p.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# An IG location tag is sometimes a landmark rather than the venue — an MRT
# station, a road, a monument. Two different restaurants tagged 信義安和站 were
# merged into one entry, and 國父紀念館 pinned a monument instead of the
# restaurant beside it. Such tags must not drive grouping or geocoding.
RE_LANDMARK = re.compile(
    r"(捷運|車站|夜市$|商圈$|[^\s]站$|大道$|紀念館$|美術館$|博物館$|公園$|廣場$"
    r"|機場$|體育館$|大學$|醫院$|[縣市區]$"
    # Street names get used as tags too — 永康街 stood in for niche taipei and
    # 敦化北路 for 三點三 DIM SUM TIME.
    r"|[^\s]{2,}[街路巷弄]$)"
)


def is_landmark(tag):
    return bool(tag) and bool(RE_LANDMARK.search(tag.strip()))


def geo_key(post, extracted):
    """Cache key for a venue's location. Must be identical in geocode.py and
    build.py — when they disagreed, a landmark-tagged post was stored under its
    caption name but looked up by its location id, and picked up the monument's
    coordinates."""
    lid = post.get("locationId")
    if lid and is_landmark(post.get("locationName")):
        lid = None
    return str(lid or (extracted or {}).get("name") or "")


def categories():
    return read_json(CONFIG / "categories.json", {"categories": []})["categories"]
