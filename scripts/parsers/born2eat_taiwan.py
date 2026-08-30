"""Caption parser for @born2eat_taiwan.

His posts open with a rigid four-line block, so name, price range, visit count
and rating are parsed deterministically. An LLM would give the same answer when
it works and a hallucinated rating when it doesn't.

    📍GiraPizza 旋轉披薩
    💰人均400-600元
    2訪/4.6⭐️（滿分5⭐️)
    目前心中台北最愛的義式披薩，快去訂位！

Roughly 2% of his posts are roundups naming several venues, which have their own
notation for scores; see parse_venues.
"""
import re

ACCOUNT = "born2eat_taiwan"
PARSER_VERSION = 1

RE_NAME = re.compile(r"^\s*📍\s*(.+?)\s*$", re.M)
RE_PRICE = re.compile(r"💰\s*人均\s*(\d+)\s*[-~至]\s*(\d+)\s*元")
RE_PRICE_SINGLE = re.compile(r"💰\s*人均\s*(\d+)\s*元")
RE_VISIT_NUM = re.compile(r"(\d+)\s*訪")
RE_VISIT_FIRST = re.compile(r"[首初]\s*訪")
RE_RATING = re.compile(r"([0-9](?:\.[0-9])?)\s*⭐")
RE_SCALE = re.compile(r"滿分\s*([0-9]+)\s*⭐")
RE_DISH = re.compile(r"^\s*✨\s*(.+?)\s*$", re.M)

# Roundup posts ("五家台北滷肉飯私心推薦") list several venues, each as a 📍 line
# followed by a one-line verdict. Some carry a visit count on the name line
# ("📍Birdy Yakitori 燒鳥狂想曲 / 5訪").
RE_VENUE_VISITS = re.compile(r"\s*/\s*(?:超過)?\s*(\d+)\s*訪\s*$")
# Newer roundups write "📍店名｜地區 @ig_handle". The handle wrecks a venue search
# (「暖燈｜台北 @dantou_tph」 matched an unrelated shop), and the region is a
# useful geocoding hint, so both are split off the name.
RE_VENUE_HANDLE = re.compile(r"\s*@[\w.]+\s*$")
# Some roundups score each venue: "推薦指數：4.75⭐️". The values observed are
# 3.25 / 3.75 / 4 / 4.25 / 4.5 / 4.75 / 5 — quarter-star steps on the same 0-5
# scale as his single-restaurant ratings, so they are directly comparable.
RE_VENUE_RATING = re.compile(
    r"(?:推薦指數[：:]\s*)?([0-5](?:\.\d{1,2})?)\s*⭐️?")
RE_VENUE_REGION = re.compile(r"^(?P<name>.+?)\s*[｜|]\s*(?P<region>[^｜|]+?)\s*$")
# A blurb ends at the hashtags, the shop-info block (📞 phone, 🏠 address), a
# ranking list, or the bracketed legend some roundups close with (「［ 關於⭐️ ］」).
RE_STOP = re.compile(r"^\s*[#🍚🍽️✨👉💰⭐📞🏠🕐［【\[]")
# Only a trailing handle is noise — one mid-sentence carries meaning
# ("@looking4goodfood 推薦的套裝行程") and must survive.
RE_BLURB_HANDLE = re.compile(r"\s*@[\w.]+\s*$")
# The Tainan roundup scores venues Michelin-style, with the rubric spelled out in
# the post: ⭐ 值得駐足 / ⭐⭐ 值得繞道前往 / ⭐⭐⭐ 值得專程造訪. That is a
# different scale from his usual 0-5 rating — 2 stars is high praise, not a 2.0 —
# so it is kept as its own field and never folded into `rating`.
RE_NAME_STARS = re.compile(r"[\s　]*((?:⭐️?)+)[\s　]*$")


def parse_venues(caption):
    """Split a roundup caption into one entry per 📍 heading."""
    venues, cur = [], None
    for line in (caption or "").splitlines():
        m = RE_NAME.match(line)
        if m:
            if cur:
                venues.append(cur)
            raw = m.group(1)
            vm = RE_VENUE_VISITS.search(raw)
            raw = RE_VENUE_VISITS.sub("", raw)
            handle = RE_VENUE_HANDLE.search(raw)
            raw = RE_VENUE_HANDLE.sub("", raw).strip()
            sm = RE_NAME_STARS.search(raw)
            guide_stars = sm.group(1).replace("️", "").count("⭐") if sm else None
            raw = RE_NAME_STARS.sub("", raw).strip()
            region = None
            rm = RE_VENUE_REGION.match(raw)
            if rm:
                raw, region = rm.group("name").strip(), rm.group("region").strip()
            cur = {
                "name": raw,
                "guide_stars": guide_stars,
                "region": region,
                "ig": handle.group(0).strip() if handle else None,
                "visits": int(vm.group(1)) if vm else None,
                "blurb": [],
            }
            continue
        if cur is None:
            continue
        text = line.strip()
        if not text:
            continue
        if RE_STOP.match(text):        # hashtags / ranking block ends the list
            venues.append(cur)
            cur = None
            continue
        cur["blurb"].append(text)
    if cur:
        venues.append(cur)
    for v in venues:
        text = " ".join(v["blurb"])
        m = RE_VENUE_RATING.search(text)
        v["rating"] = round(float(m.group(1)), 2) if m else None
        # Keep the score out of the blurb; it is surfaced as a rating instead.
        text = RE_VENUE_RATING.sub("", text).replace("推薦指數：", "")
        while RE_BLURB_HANDLE.search(text):
            text = RE_BLURB_HANDLE.sub("", text)
        v["blurb"] = text.strip(" ️​·、，,。.-—　")[:220]
    return venues


def parse_header(caption):
    """Extract the deterministic fields. Returns dict; values are None if absent."""
    caption = caption or ""
    out = {
        "name": None, "price_min": None, "price_max": None,
        "visits": None, "rating": None, "tagline": None, "dishes": [],
        # He usually posts one restaurant per post, but ~2% are roundups
        # ("五家台北漢堡店") listing several. Taking the first 📍 would invent a
        # restaurant from the roundup's own (absent) rating and silently drop the
        # rest, so count the pins and let the builder handle it.
        "pin_count": 0,
    }
    out["pin_count"] = len(RE_NAME.findall(caption))
    if out["pin_count"] > 1:
        out["venues"] = parse_venues(caption)

    m = RE_NAME.search(caption)
    if m:
        # He sometimes appends the venue's IG handle to the 📍 line. It is not
        # part of the name and wrecks a venue search, so it comes off here as
        # well as in the roundup parser. A trailing ｜ is just sloppy typing;
        # a ｜ mid-name usually carries a branch or a description, so it stays.
        name = RE_VENUE_HANDLE.sub("", m.group(1))
        out["name"] = name.strip(" 　｜|·、,-—") or None

    m = RE_PRICE.search(caption)
    if m:
        out["price_min"], out["price_max"] = int(m.group(1)), int(m.group(2))
    else:
        m = RE_PRICE_SINGLE.search(caption)
        if m:
            out["price_min"] = out["price_max"] = int(m.group(1))

    # Visits and rating share a line ("2訪/4.3⭐️（滿分5⭐️)"). Search only the head
    # of the caption so a "5訪" mentioned in the prose body cannot override it.
    head = "\n".join(caption.splitlines()[:6])
    m = RE_VISIT_NUM.search(head)
    if m:
        out["visits"] = int(m.group(1))
    elif RE_VISIT_FIRST.search(head):
        out["visits"] = 1

    m = RE_RATING.search(head)
    if m:
        rating = float(m.group(1))
        scale = RE_SCALE.search(head)
        # Normalise to a 5-point scale if he ever switches denominators.
        if scale and int(scale.group(1)) != 5:
            rating = rating * 5.0 / int(scale.group(1))
        out["rating"] = round(rating, 2)

    # The tagline is the first non-empty line after the visits/rating line.
    lines = caption.splitlines()
    for i, line in enumerate(lines):
        if RE_RATING.search(line) and ("訪" in line or "滿分" in line):
            for nxt in lines[i + 1:]:
                if nxt.strip():
                    out["tagline"] = nxt.strip()
                    break
            break

    for d in RE_DISH.findall(caption):
        out["dishes"].append({"name": d.replace("🧡", "").strip(), "favourite": "🧡" in d})

    return out


def parse(post):
    """Registry entry point. Takes the whole post so parsers that need more than
    the caption (jc_foodidi reads its structured footer) have it available."""
    return parse_header(post.get("caption"))
