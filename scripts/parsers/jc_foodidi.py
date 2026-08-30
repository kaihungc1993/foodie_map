"""Caption parser for @jc_foodidi.

Nothing about this format overlaps with born2eat_taiwan's — no 📍, no 人均
range, and the score is written in moon phases:

    花滔廚房 23訪                     <- name and his own visit count, line 1
    熟悉的義式餐桌 😀                  <- one-line verdict, line 2
    ...
    雞肝慕斯💰280                     <- per-dish prices, never a per-head average
    美味程度：🌕🌕🌕🌕🌗
    再訪意願：🌕🌕🌕🌕🌗
    🏪台北市松山區光復北路120巷31號
    ☎️02-25772445
    ⏰週三到週日 18:00-21:30
    🍴2026.08.12到訪用餐              <- the actual visit, not the post date

He also publishes the address, hours and phone as text. Those are kept in
`extras` for cross-checking Google's answer rather than replacing it: the map
needs coordinates, and the displayed address has to stay tied to the place_id
that drives the Google Maps link, the hours lookup and the cross-account dedupe.
"""
import re

ACCOUNT = "jc_foodidi"
PARSER_VERSION = 1

# 🌕 full through 🌑 new, used as quarter-step fractions of one star. His posts
# up to early 2023 used filled/empty circles instead — the same five positions,
# but only whole units, so 🔴🔴🔴🔴⚪ is a flat 4.0.
MOON = {"🌕": 1.0, "🌖": 0.75, "🌗": 0.5, "🌘": 0.25, "🌑": 0.0,
        "🌔": 0.75, "🌓": 0.5, "🌒": 0.25,
        "🔴": 1.0, "⚪": 0.0}
# Each glyph may carry a trailing variation selector (⚪️), so the run is a
# repetition of "glyph + optional VS16", not a bare character class.
MOON_RUN = "(?:[" + "".join(MOON) + "]\ufe0f?)+"
RE_TASTE = re.compile(r"美味程度[：:]\s*(" + MOON_RUN + ")")
RE_REVISIT = re.compile(r"再訪意願[：:]\s*(" + MOON_RUN + ")")
MOON_GLYPHS = 5          # every observed run is exactly five; see rating()

# Line 1 is "店名 23訪" / "店名 初訪" / "店名 六訪", sometimes with trailing tags.
RE_HEAD = re.compile(
    r"^(?P<name>.+?)[\s　]*"
    r"(?P<visits>\d+|[初首]|[一二三四五六七八九十]+)[\s　]*訪"
    r"[\s　]*(?:#\S+[\s　]*)*$")
RE_TRAILING_TAGS = re.compile(r"(?:[\s　]*#\S+)+[\s　]*$")

RE_ADDRESS = re.compile(r"^[\s　]*🏪[\s　]*(\S.*?)[\s　]*$", re.M)
RE_HOURS = re.compile(r"^[\s　]*⏰[\s　]*(\S.*?)[\s　]*$", re.M)
RE_PHONE = re.compile(r"^[\s　]*☎️?[\s　]*(\S.*?)[\s　]*$", re.M)
RE_VISITED = re.compile(r"🍴[\s　]*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")
# "雞肝慕斯💰280" — a dish and what it cost, one per line.
RE_DISH_PRICE = re.compile(r"^[\s　]*(?P<name>[^\n💰]{1,60}?)[\s　]*💰[\s　]*(?P<price>\d+)",
                           re.M)
# jc uses 📍 for a street address, not a venue heading — 丰丹嚴選本舖 lists seven
# branch addresses that way. He writes no roundups, so pin_count stays 0 and the
# builder never diverts his posts down the multi-venue path.

CJK_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def cjk_number(text):
    """一 → 1, 十 → 10, 十一 → 11, 二十三 → 23. Returns None if unparseable."""
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text in ("初", "首"):
        return 1
    if "十" not in text:
        return CJK_DIGITS.get(text)
    head, _, tail = text.partition("十")
    tens = CJK_DIGITS.get(head, 1) if head else 1
    ones = CJK_DIGITS.get(tail, 0) if tail else 0
    if (head and head not in CJK_DIGITS) or (tail and tail not in CJK_DIGITS):
        return None
    return tens * 10 + ones


def moon_score(caption, pattern):
    """Sum a moon run into a 0-5 score.

    A run that is not exactly five glyphs is refused rather than rescaled:
    scaling a four-glyph run to /5 would invent a score he never wrote.
    """
    m = pattern.search(caption or "")
    if not m:
        return None
    run = [c for c in m.group(1) if c in MOON]
    if len(run) != MOON_GLYPHS:
        return None
    return round(sum(MOON[c] for c in run), 2)


def parse_header(caption):
    caption = caption or ""
    out = {
        "name": None, "price_min": None, "price_max": None,
        "visits": None, "rating": None, "tagline": None, "dishes": [],
        "pin_count": 0,
        "rating_axes": {},
        "extras": {},
    }
    lines = [ln for ln in caption.splitlines()]
    non_empty = [ln.strip() for ln in lines if ln.strip()]

    if non_empty:
        head = non_empty[0]
        m = RE_HEAD.match(head)
        if m:
            out["name"] = m.group("name").strip(" 　·、,-—") or None
            out["visits"] = cjk_number(m.group("visits"))
        else:
            # Plenty of posts carry no visit count at all; the whole first line
            # is then the name, minus any hashtags he tacked on.
            out["name"] = RE_TRAILING_TAGS.sub("", head).strip(" 　·、,-—") or None

    # Line 2 is his one-line verdict, the counterpart of born2eat's line 4.
    # It feeds the classifier's author_verdict, so it matters for category quality.
    if len(non_empty) > 1:
        out["tagline"] = non_empty[1][:220]

    taste = moon_score(caption, RE_TASTE)
    revisit = moon_score(caption, RE_REVISIT)
    out["rating"] = taste
    out["rating_axes"] = {"taste": taste, "revisit": revisit}

    # Per-dish prices are NOT summed into a per-head range: the party size is
    # unknown, so any 人均 derived from them would be fiction.
    for m in RE_DISH_PRICE.finditer(caption):
        name = m.group("name").strip(" 　·、,-—🍑🍋✨")
        if name:
            out["dishes"].append({"name": name, "favourite": False,
                                  "price": int(m.group("price"))})

    addr = RE_ADDRESS.search(caption)
    hours = RE_HOURS.search(caption)
    phone = RE_PHONE.search(caption)
    visited = RE_VISITED.search(caption)
    out["extras"] = {
        "address_text": addr.group(1) if addr else None,
        "hours_text": hours.group(1) if hours else None,
        "phone": phone.group(1) if phone else None,
        "visited_on": (f"{visited.group(1)}-{int(visited.group(2)):02d}-"
                       f"{int(visited.group(3)):02d}") if visited else None,
    }
    return out


def parse(post):
    return parse_header(post.get("caption"))
