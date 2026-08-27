# Foodie Map

A map of Taipei restaurants reviewed by [@born2eat_taiwan](https://www.instagram.com/born2eat_taiwan/),
built as a static GitHub Pages site and refreshed monthly by GitHub Actions.

Account setup and API keys: **[SETUP.md](SETUP.md)**.

## Pipeline

    crawl.py → extract.py → geocode.py → hours.py → build.py → docs/
    (Apify)    (regex+Opus)   (Places)      (Places)   (aggregate) (Pages)

| Step | Input | Output | Notes |
|---|---|---|---|
| `crawl.py` | Instagram via Apify | `data/raw_posts.json` | Incremental; `--backfill` for full history |
| `extract.py` | raw posts | `data/extracted.json` | Regex header + Claude classification, cached per post |
| `geocode.py` | IG location tags | `data/geocode_cache.json` | One lookup per venue, cached forever |
| `hours.py` | place ids | `data/hours_cache.json` | Lunch / dinner / closed days, Enterprise SKU |
| `build.py` | all of the above | `docs/data/restaurants.json` | Groups posts into restaurants |

Every intermediate file is committed, so a rerun costs nothing for work already done.

### Why the split in `extract.py`

His captions open with a rigid header:

    📍GiraPizza 旋轉披薩
    💰人均400-600元
    2訪/4.6⭐️（滿分5⭐️)
    目前心中台北最愛的義式披薩，快去訂位！

Name, price, visit count and rating are parsed by regex — deterministic and free.
Claude (`claude-opus-5`) handles only what needs judgement: whether the post is a
restaurant review at all, and which categories from `config/categories.json` apply.
Runs of more than 20 new posts go through the Batch API at 50% cost.

### Roundup posts

About 2% of posts are roundups listing several restaurants (「五家台北滷肉飯私心推薦」).
Each is split per venue rather than attributed to its first 📍. Venues are
classified individually — inheriting the post's categories would tag every venue
in a Fukuoka roundup as 壽司 + 拉麵 + 燒肉 at once.

Two rating notations appear in them. Fukuoka posts score each venue
「推薦指數：4.75⭐️」 in quarter-star steps on the usual 0-5 scale. The Tainan post
grades Michelin-style with the rubric printed in the caption, so it is stored as
`guide_stars` and mapped to 4.3 / 4.4 / 4.6 — ⭐⭐ there means「值得繞道前往」, not a
2.0. A rating he stated himself always wins over that mapping.

### Grouping

Restaurants are keyed on the **Instagram location id**, which is stable across
posts. Name matching alone would split venues — post `DcN40K1DwpE` writes
「德榮軒脆皮鰻魚飯專賣店」 in the caption while its location tag reads
「德榮軒 初•一脆皮鰻魚飯專門店」.

Rating and visit count come from the newest post in each group; categories are the
union across all its posts. Venues closer than 50m are written to
`data/merges.json` as suggestions for review — never merged automatically, since
department store food halls legitimately share coordinates.

Two entries sharing a Places id are merged automatically — one business, one id,
however differently he spelled the name. Verified against the whole dataset before
switching this on: all 54 shared ids were genuine duplicates, and distinct
restaurants at one address hold different ids.

An IG location tag is sometimes a landmark rather than the venue — 「信義安和站」
is an MRT station two different restaurants were tagged with, and grouping on it
merged them into one entry. Landmark tags no longer drive grouping or geocoding.

To force a merge, add a cluster to `merges.json` (first id wins):

```json
{ "merge": [["loc:111", "loc:222"]], "split": ["loc:333"] }
```

## Personal notes

Notes are edited in the page and committed to `docs/data/notes.json` through the
GitHub Contents API, using a fine-grained PAT you paste once (stored in that
browser's `localStorage`, never sent anywhere else). Unsaved edits are kept as
local drafts. Saving re-reads the remote file first, so a note saved on your phone
is not clobbered by one saved on your laptop.

**This repo is public, so notes are publicly readable.** Keep them to
「點鴨胸」-grade content.

## Running locally

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/crawl.py --limit 40
./.venv/bin/python scripts/extract.py
./.venv/bin/python scripts/geocode.py
./.venv/bin/python scripts/build.py
cd docs && python3 -m http.server 8000     # http://localhost:8000
```

`http://localhost:8000/*` must be in the browser key's referrer allowlist or the
map will not load.

## Refresh job

`.github/workflows/refresh.yml` runs on the 1st of each month at 10:00 Taipei, and
on demand from the Actions tab with three inputs: `limit`, `backfill`, and
`force_extract` (re-classify everything after editing the category list).

The job commits directly to `main`; Pages serves `/docs` from that branch.
Pushes retry with rebase because the browser writes notes to the same branch.
