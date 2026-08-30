"""Per-account caption parsers.

Each Instagram account writes its captions to its own template, and those
templates share nothing: born2eat_taiwan opens with 📍 and scores out of 5 stars,
jc_foodidi puts the name on line 1 and scores with moon phases. Keeping one
parser per account stops either author's quirks leaking into the other's data.

A parser exposes `parse(post) -> dict` returning the shared schema below. Fields
it cannot find are None or [] — never invented.

    name, visits, rating, tagline, price_min, price_max, dishes, pin_count
    venues      only for roundup posts (pin_count > 1)
    extras      account-specific fields the builder reads by explicit name
    parser, parser_version
"""
from . import born2eat_taiwan, jc_foodidi

_MODULES = (born2eat_taiwan, jc_foodidi)
REGISTRY = {m.ACCOUNT: m for m in _MODULES}


def get(account):
    """Return the parser module for an account, or None if unregistered.

    Callers must treat None as a hard error. Falling back to another account's
    parser would produce a record that looks well-formed but is entirely None —
    the hardest kind of data loss to notice.
    """
    return REGISTRY.get(account)


def parse(post):
    """Parse a post with its own account's parser. Raises on unknown accounts."""
    account = post.get("ownerUsername")
    mod = get(account)
    if mod is None:
        raise KeyError(f"no parser registered for account {account!r}")
    out = mod.parse(post)
    out.setdefault("extras", {})
    out["parser"] = account
    out["parser_version"] = mod.PARSER_VERSION
    return out


def accounts():
    return sorted(REGISTRY)
