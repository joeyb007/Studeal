"""Per-marketplace URL canonicalization for dedup.

Two listings on the same marketplace are "the same" if their canonical URLs
match. Canonicalizers strip tracking params, session tokens, and other
non-identifying URL noise so redirect / share / referral variants dedupe
against the underlying listing.

Adding a new marketplace canonicalizer:
    1. Write a function `_<marketplace>_canonicalize(url) -> str`.
    2. Register it in `_CANONICALIZERS`.
Sites without a registered canonicalizer use `_generic_canonicalize` — strip
query + fragment, lowercase host. That's the right default for most
marketplaces we care about.
"""

from __future__ import annotations

from typing import Callable
from urllib.parse import urlparse, urlunparse


def canonicalize_url(url: str, marketplace: str) -> str:
    """Return the canonical dedup key for `url` on `marketplace`.

    Unregistered marketplaces fall back to a generic strip-query strategy.
    """
    canonicalizer = _CANONICALIZERS.get(marketplace, _generic_canonicalize)
    return canonicalizer(url)


# ---------------------------------------------------------------------------
# Canonicalizers
# ---------------------------------------------------------------------------

def _generic_canonicalize(url: str) -> str:
    """Strip query string + fragment; lowercase scheme + host. Preserves
    path — most marketplaces put the listing ID in the URL path."""
    parsed = urlparse(url)
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/") or "/",
        "",   # params
        "",   # query
        "",   # fragment
    ))


# All currently-supported marketplaces put the listing ID in the URL path
# and use query params only for tracking (ref, hash, source, etc.). Generic
# strategy is right for all of them. This registry exists so we can override
# per-site when we add marketplaces with weirder URL schemes (e.g. Amazon's
# /dp/ vs /gp/product/ duality, Etsy's language prefixes).
_CANONICALIZERS: dict[str, Callable[[str], str]] = {
    "kijiji": _generic_canonicalize,
    "fb_marketplace": _generic_canonicalize,
    "ebay": _generic_canonicalize,
    "craigslist": _generic_canonicalize,
}
