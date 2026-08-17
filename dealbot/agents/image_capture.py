"""Deterministic listing-thumbnail capture.

One page.evaluate per settled snapshot walks gallery card anchors, climbs to
the single-card container, and reports image candidates. Python picks and
validates the URL (CDN whitelist + load check) and keys the map by canonical
listing URL. The LLM extractor never sees or copies an image URL; offers are
joined to images by canonical URL after parsing (attach_images).

Safety invariant: every failure here yields a missing image, never a wrong
one. Capture failures return {} and must not fail the snapshot or the turn.

Probe evidence: scripts/probe_listing_images.py (2026-08-02) — 94/94 cards
across kijiji/fb/ebay/craigslist had the img in the single-card container
with a whitelisted CDN URL available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from dealbot.agents.marketplace_router import CONFIG_BY_KEY
from dealbot.persistence.canonicalize import canonicalize_url

if TYPE_CHECKING:
    from playwright.async_api import Page

    from dealbot.agents.workers.extractor import Offer

logger = logging.getLogger(__name__)

# Icons, avatars, and lazy-load placeholder pixels are small; product thumbs
# are not. Below this, a LOADED image is rejected outright and an unloaded
# src attribute is still allowed (below-fold cards report naturalWidth 0).
_MIN_LOADED_WIDTH = 64

# Runaway guard only. Must exceed anything extraction can see: FB infinite
# scroll holds 100+ cards in one snapshot, and extraction reads them all —
# a cap below that silently strips images from the overflow (measured
# 2026-08-03: FB coverage 47.5% with a cap of 60).
_MAX_CARDS = 300


@dataclass(frozen=True)
class ImageCaptureSpec:
    href_pattern: str
    cdn_hosts: tuple[str, ...]
    href_exclude: str | None = None


def spec_for(marketplace: str) -> ImageCaptureSpec | None:
    """Capture spec for a marketplace, or None when the site is unprobed
    (retailer sites): capture is skipped entirely, not attempted generically."""
    cfg = CONFIG_BY_KEY.get(marketplace)
    if cfg is None or not cfg.listing_href_pattern:
        return None
    return ImageCaptureSpec(
        href_pattern=cfg.listing_href_pattern,
        cdn_hosts=cfg.image_cdn_hosts,
        href_exclude=cfg.listing_href_exclude,
    )


def _host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return False
    if not netloc:
        return False
    return any(
        netloc == h.lstrip(".") or netloc.endswith(h if h.startswith(".") else "." + h)
        for h in hosts
    )


def _usable(url: str, hosts: tuple[str, ...]) -> bool:
    return url.startswith(("http://", "https://")) and _host_allowed(url, hosts)


def _normalize_url(url: str) -> str:
    # Shopify (openbox) emits protocol-relative srcset URLs (//host/path).
    return "https:" + url if url.startswith("//") else url


def _first_srcset_url(srcset: str) -> str:
    # "url1 200w, url2 400w" → url1. Enough for a thumbnail.
    first = srcset.split(",")[0].strip()
    return _normalize_url(first.split()[0]) if first else ""


def pick_image_url(candidate: dict, hosts: tuple[str, ...]) -> str | None:
    """Choose the trustworthy URL from one card img candidate, or None.

    Priority per img: a genuinely LOADED currentSrc (resolves srcset/lazy),
    else the raw src attribute, else the first srcset URL (probe 2026-08-15:
    openbox product imgs carry empty src and real URLs only in srcset). All
    must pass the CDN whitelist — that is what rejects eBay's ebaystatic
    placeholder graphic.
    """
    current = _normalize_url(candidate.get("currentSrc") or "")
    src = _normalize_url(candidate.get("srcAttr") or "")
    srcset_url = _first_srcset_url(candidate.get("srcset") or "")
    width = candidate.get("naturalWidth") or 0
    if width >= _MIN_LOADED_WIDTH and _usable(current, hosts):
        return current
    if _usable(src, hosts):
        return src
    if _usable(srcset_url, hosts):
        return srcset_url
    return None


def pick_card_image(imgs: list[dict], hosts: tuple[str, ...]) -> str | None:
    """Best image among a card's candidates: largest rendered area first.

    Badges and icons render small (openbox 'refurbished' badge: 54px) while
    product shots dominate the card (300px) — area ordering keeps the wrong
    image out even when the badge is the only img with a src attribute.
    Document order breaks ties, preserving the old first-img behavior.
    """
    ordered = sorted(
        imgs, key=lambda im: -((im.get("w") or 0) * (im.get("h") or 0))
    )
    for im in ordered:
        url = pick_image_url(im, hosts)
        if url is not None:
            return url
    return None


# Climbs each card anchor to the smallest ancestor still holding exactly ONE
# distinct card-anchor href (never enters a multi-card region), then reports
# the container's img candidates (up to 4) with attrs + layout size. All
# policy stays Python-side. Mirrors scripts/probe_listing_images.py CARD_JS
# (validated 94/94); multi-img + srcset added after the 2026-08-15 probe
# showed openbox product shots live in srcset with an empty src.
_CAPTURE_JS = """
([sel, maxCards]) => {
  const seen = new Set();
  const out = [];
  for (const a of document.querySelectorAll(sel)) {
    const abs = a.href || '';
    if (!abs || seen.has(abs)) continue;
    seen.add(abs);

    let container = a;
    for (let i = 0; i < 6; i++) {
      const p = container.parentElement;
      if (!p) break;
      const hrefs = new Set(
        [...p.querySelectorAll(sel)].map(x => x.href || ''),
      );
      hrefs.delete('');
      if (hrefs.size > 1) break;
      container = p;
    }

    const imgs = [...container.querySelectorAll('img')].slice(0, 4).map(im => ({
      currentSrc: im.currentSrc || '',
      srcAttr: im.getAttribute('src') || '',
      srcset: im.getAttribute('srcset') || '',
      naturalWidth: im.naturalWidth || 0,
      w: im.offsetWidth || 0,
      h: im.offsetHeight || 0,
    }));
    if (imgs.length) {
      out.push({ href: abs, imgs });
    }
    if (out.length >= maxCards) break;
  }
  return out;
}
"""


async def capture_card_images(
    page: "Page",
    spec: ImageCaptureSpec,
    marketplace: str,
) -> dict[str, str]:
    """One evaluate over the live page: {canonical listing URL: image URL}.

    Any failure returns {} — a hunt must never degrade because thumbnails
    could not be captured.
    """
    selector = f'a[href*="{spec.href_pattern}"]'
    if spec.href_exclude:
        # Excluded up front so navigation links never consume the card budget.
        selector += f':not([href*="{spec.href_exclude}"])'
    try:
        candidates = await page.evaluate(_CAPTURE_JS, [selector, _MAX_CARDS])
    except Exception as exc:
        logger.info("capture_card_images: evaluate failed on %s: %s", marketplace, exc)
        return {}

    image_map: dict[str, str] = {}
    for cand in candidates or []:
        url = pick_card_image(cand.get("imgs") or [], spec.cdn_hosts)
        if url is None:
            continue
        try:
            key = canonicalize_url(cand["href"], marketplace)
        except Exception:
            continue
        image_map.setdefault(key, url)
    return image_map


def attach_images(
    offers: "list[Offer]",
    image_map: dict[str, str],
    marketplace: str,
) -> None:
    """Join extracted offers to captured images by canonical URL, in place.

    Unconditional assignment: an LLM-emitted image_url is never trusted, so
    a join miss clears it rather than keeping it.
    """
    for offer in offers:
        try:
            key = canonicalize_url(offer.url, marketplace)
        except Exception:
            offer.image_url = None
            continue
        offer.image_url = image_map.get(key)
