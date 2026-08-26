"""Shared paths, env loading, and JSON helpers."""
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS_DATA = ROOT / "docs" / "data"
CONFIG = ROOT / "config"

RAW_POSTS = DATA / "raw_posts.json"
EXTRACTED = DATA / "extracted.json"
GEOCACHE = DATA / "geocode_cache.json"
MERGES = DATA / "merges.json"
RESTAURANTS = DOCS_DATA / "restaurants.json"
NOTES = DOCS_DATA / "notes.json"

IG_ACCOUNT = "born2eat_taiwan"


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


def categories():
    return read_json(CONFIG / "categories.json", {"categories": []})["categories"]
