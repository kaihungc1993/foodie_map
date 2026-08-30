"""Turn raw IG posts into structured restaurant records.

Two-stage by design:

  1. Regex over the post header. His captions open with a rigid four-line block
     (name / price / visits+rating / tagline), so name, price range, visit count
     and rating are parsed deterministically for free. An LLM would give the same
     answer when it works and a hallucinated rating when it doesn't.

  2. Claude for the parts that need judgement: is this a restaurant post at all,
     and which categories from the closed vocabulary apply.

Results are cached in data/extracted.json keyed by post id, so re-runs cost
nothing. Use --force to re-extract (e.g. after editing config/categories.json).
"""
import argparse
import json
import re
import sys
import time

import anthropic

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import common
import parsers

MODEL = "claude-opus-5"
BATCH_THRESHOLD = 20  # above this, use the Batch API at 50% cost

# --- Stage 1: deterministic header parsing ------------------------------------
# Lives in scripts/parsers/, one module per account: the two authors' caption
# templates share nothing, and a single parser trying to serve both would apply
# one author's rules to the other's posts.


# --- Stage 2: Claude for classification ---------------------------------------

SYSTEM = """You classify Taiwanese food blog posts from Instagram.

Decide two things:
1. is_restaurant — true if the post reviews a specific restaurant, cafe, bar or
   food shop the author ate at. False for city exploration, travel, hotel stays,
   product promotions, giveaways, or general roundup posts that name no single venue.
2. categories — pick every category that applies from the allowed list. Pick the
   cuisine AND the format where both fit (a high-end sushi counter is 壽司 +
   日本料理 + fine dining; a Neapolitan pizzeria is 義式 + 披薩).

Rules:
- If a hashtag or dish name maps directly onto a category in the list, include that
  category. Do not stop at the broadest cuisine label when a specific one also fits.
- Use 米其林 only when the post says the venue holds a Michelin star, Bib Gourmand
  or selection.
- Use 酒吧 only for drinks-led venues. A place serving full meals with a drinks
  programme is 餐酒館.
- Never invent a category outside the list.

Judge from the venue name, hashtags, dish names and the author's one-line verdict."""


def build_schema(cats):
    return {
        "type": "object",
        "properties": {
            "is_restaurant": {"type": "boolean"},
            "categories": {
                "type": "array",
                "items": {"type": "string", "enum": cats},
            },
        },
        "required": ["is_restaurant", "categories"],
        "additionalProperties": False,
    }


def build_prompt(post, header):
    """Send the signal-dense parts only, not the full essay — the body prose adds
    ~1200 tokens per post and almost nothing to the classification."""
    body = re.sub(r"\s+", " ", (post.get("caption") or ""))[:300]
    return json.dumps({
        "venue_name": header["name"] or post.get("locationName"),
        "ig_location_tag": post.get("locationName"),
        "hashtags": post.get("hashtags") or [],
        "author_verdict": header["tagline"],
        "dishes": [d["name"] for d in header["dishes"]][:12],
        "caption_excerpt": body,
    }, ensure_ascii=False)


def request_params(post, header, schema):
    return {
        "model": MODEL,
        "max_tokens": 2000,
        "system": SYSTEM,
        "messages": [{"role": "user", "content": build_prompt(post, header)}],
        # Classification is simple; low effort keeps thinking tokens down.
        "output_config": {"effort": "low", "format": {"type": "json_schema", "schema": schema}},
    }


def read_json_result(msg):
    text = next((b.text for b in msg.content if b.type == "text"), None)
    if not text:
        return None
    return json.loads(text)


def classify_sync(client, todo, schema):
    out = {}
    for i, (pid, post, header) in enumerate(todo, 1):
        try:
            msg = client.messages.create(**request_params(post, header, schema))
            out[pid] = read_json_result(msg)
            print(f"  [{i}/{len(todo)}] {header['name'] or pid}: {out[pid]['categories']}")
        except anthropic.APIStatusError as e:
            print(f"  [{i}/{len(todo)}] {pid} failed: {e.status_code} {e.message}", file=sys.stderr)
    return out


def classify_batch(client, todo, schema):
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    reqs = [
        Request(custom_id=pid, params=MessageCreateParamsNonStreaming(
            **request_params(post, header, schema)))
        for pid, post, header in todo
    ]
    print(f"  submitting batch of {len(reqs)} (50% pricing)...")
    batch = client.messages.batches.create(requests=reqs)
    print(f"  batch {batch.id} submitted; polling")

    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        print(f"  {b.processing_status}: {b.request_counts.processing} processing, "
              f"{b.request_counts.succeeded} done")
        time.sleep(30)

    out = {}
    for res in client.messages.batches.results(batch.id):
        if res.result.type == "succeeded":
            try:
                out[res.custom_id] = read_json_result(res.result.message)
            except (json.JSONDecodeError, StopIteration):
                print(f"  {res.custom_id}: unparseable result", file=sys.stderr)
        else:
            print(f"  {res.custom_id}: {res.result.type}", file=sys.stderr)
    return out


VENUE_SYSTEM = """You classify a single restaurant mentioned inside a Taiwanese
food blogger's roundup post ("five best burger joints in Taipei").

Decide two things:
1. is_restaurant — true if this is a restaurant, cafe, bar or food shop. False for
   sights, hotels, shops that sell no food.
2. categories — pick every category that applies from the allowed list, judging
   this venue specifically. The roundup's own theme is a strong hint but not a
   rule: a Fukuoka food roundup names ramen shops AND sushi counters, and each
   venue takes only what fits it. Never invent a category outside the list."""


def classify_venues(client, posts, extracted, cats, cache, force):
    """Roundup venues get their own classification. Inheriting the post's
    categories would tag every venue in a Fukuoka roundup as 壽司 + 拉麵 +
    燒肉 at once."""
    schema = build_schema(cats)
    todo = {}
    for pid, post in posts.items():
        ex = extracted.get(pid)
        if not ex or (ex.get("pin_count") or 0) <= 1:
            continue
        theme = (post.get("caption") or "").splitlines()[0][:60]
        for v in ex.get("venues") or []:
            key = venue_key(v["name"])
            if not key or (key in cache and not force):
                continue
            todo[key] = {
                "venue_name": v["name"],
                "region": v.get("region"),
                "the_blogger_says": v.get("blurb") or None,
                "roundup_theme": theme,
                "roundup_hashtags": (post.get("hashtags") or [])[:8],
            }
    if not todo:
        return cache

    print(f"  classifying {len(todo)} roundup venues")
    items = list(todo.items())
    if len(items) > BATCH_THRESHOLD:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request
        # custom_id only accepts [a-zA-Z0-9_-], and venue keys are Chinese, so
        # the batch is indexed and mapped back afterwards.
        ids = {f"v{i}": k for i, (k, _) in enumerate(items)}
        reqs = [Request(custom_id=f"v{i}", params=MessageCreateParamsNonStreaming(
                    model=MODEL, max_tokens=2000, system=VENUE_SYSTEM,
                    messages=[{"role": "user", "content": json.dumps(v, ensure_ascii=False)}],
                    output_config={"effort": "low",
                                   "format": {"type": "json_schema", "schema": schema}}))
                for i, (_, v) in enumerate(items)]
        batch = client.messages.batches.create(requests=reqs)
        print(f"  venue batch {batch.id} submitted; polling")
        while True:
            b = client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            time.sleep(30)
        for res in client.messages.batches.results(batch.id):
            if res.result.type == "succeeded":
                cache[ids[res.custom_id]] = read_json_result(res.result.message)
            else:
                print(f"  {ids.get(res.custom_id)}: {res.result.type}", file=sys.stderr)
    else:
        for k, v in items:
            msg = client.messages.create(
                model=MODEL, max_tokens=2000, system=VENUE_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(v, ensure_ascii=False)}],
                output_config={"effort": "low",
                               "format": {"type": "json_schema", "schema": schema}})
            cache[k] = read_json_result(msg)
    return cache


def venue_key(name):
    return "rv:" + re.sub(r"[\s·•・.,、，\-_()（）｜|/]+", "", (name or "").lower())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-extract already-cached posts")
    ap.add_argument("--sync", action="store_true", help="force sync API even for large runs")
    ap.add_argument("--limit", type=int, help="only process N posts (for testing)")
    args = ap.parse_args()

    common.require_env("ANTHROPIC_API_KEY")
    posts = common.read_json(common.RAW_POSTS, {})
    cache = {} if args.force else common.read_json(common.EXTRACTED, {})
    cats = common.categories()
    schema = build_schema(cats)

    todo = []
    headers = {}
    unparsed = {}
    for pid, post in posts.items():
        try:
            header = parsers.parse(post)
        except KeyError:
            # A post from an account with no registered parser is skipped loudly.
            # Running it through another account's parser would yield a record
            # that looks well-formed and is entirely empty.
            unparsed.setdefault(post.get("ownerUsername"), 0)
            unparsed[post.get("ownerUsername")] += 1
            continue
        headers[pid] = header
        if pid not in cache:
            todo.append((pid, post, header))
    for account, n in sorted(unparsed.items()):
        print(f"WARNING: {n} posts from @{account} skipped — no parser registered",
              file=sys.stderr)
    if args.limit:
        todo = todo[: args.limit]

    print(f"{len(posts)} posts, {len(todo)} need classification")

    if todo:
        client = anthropic.Anthropic()
        use_batch = len(todo) > BATCH_THRESHOLD and not args.sync
        results = (classify_batch if use_batch else classify_sync)(client, todo, schema)
    else:
        results = {}

    # Merge: header fields are recomputed every run (free, and picks up regex
    # fixes); the LLM verdict is sticky once cached.
    merged = {}
    for pid, post in posts.items():
        verdict = results.get(pid) or cache.get(pid, {}).get("llm")
        if verdict is None or pid not in headers:
            continue
        merged[pid] = {"llm": verdict, **headers[pid]}

    common.write_json(common.EXTRACTED, merged)

    vcache = {} if args.force else common.read_json(common.VENUES, {})
    if any((v.get("pin_count") or 0) > 1 for v in merged.values()):
        client = client if "client" in dir() else anthropic.Anthropic()
        vcache = classify_venues(client, posts, merged, cats, vcache, args.force)
        common.write_json(common.VENUES, vcache)
    restaurants = sum(1 for v in merged.values() if v["llm"]["is_restaurant"])
    print(f"extracted {len(merged)} posts, {restaurants} are restaurant posts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
