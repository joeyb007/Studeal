"""Tests for URL canonicalization."""

from __future__ import annotations

import pytest

from dealbot.persistence.canonicalize import canonicalize_url


@pytest.mark.parametrize("raw,expected", [
    (
        "https://www.kijiji.ca/v-office-chair/toronto/aeron/1234567?abc=1",
        "https://www.kijiji.ca/v-office-chair/toronto/aeron/1234567",
    ),
    (
        "HTTPS://WWW.KIJIJI.CA/v-office/aeron/999",
        "https://www.kijiji.ca/v-office/aeron/999",
    ),
    (
        "https://www.kijiji.ca/v-office/aeron/1234#reviews",
        "https://www.kijiji.ca/v-office/aeron/1234",
    ),
    (
        "https://www.kijiji.ca/v-office/aeron/1234/",
        "https://www.kijiji.ca/v-office/aeron/1234",
    ),
])
def test_kijiji_strips_query_fragment_and_trailing_slash(raw, expected):
    assert canonicalize_url(raw, "kijiji") == expected


def test_fb_marketplace_strips_ref_query_param():
    raw = "https://www.facebook.com/marketplace/item/1234567890?ref=search_promoted&_rdr"
    expected = "https://www.facebook.com/marketplace/item/1234567890"
    assert canonicalize_url(raw, "fb_marketplace") == expected


def test_ebay_strips_hash_and_trkparms():
    raw = "https://www.ebay.ca/itm/1234567890?_trkparms=abc&hash=xyz"
    expected = "https://www.ebay.ca/itm/1234567890"
    assert canonicalize_url(raw, "ebay") == expected


def test_craigslist_strips_query():
    raw = "https://toronto.craigslist.org/tor/fua/d/aeron-chair/7654321.html?utm_source=share"
    expected = "https://toronto.craigslist.org/tor/fua/d/aeron-chair/7654321.html"
    assert canonicalize_url(raw, "craigslist") == expected


def test_unknown_marketplace_falls_back_to_generic():
    raw = "https://unknown.example.com/path/1234?ref=x#frag"
    expected = "https://unknown.example.com/path/1234"
    assert canonicalize_url(raw, "notreal") == expected


def test_duplicate_variants_all_canonicalize_to_same_key():
    """Different URL variants for the same listing → same canonical key."""
    variants = [
        "https://www.kijiji.ca/v-office/aeron/1234",
        "https://www.kijiji.ca/v-office/aeron/1234/",
        "https://www.kijiji.ca/v-office/aeron/1234?utm_source=x",
        "https://WWW.KIJIJI.CA/v-office/aeron/1234#reviews",
    ]
    canons = {canonicalize_url(v, "kijiji") for v in variants}
    assert len(canons) == 1, f"expected 1 canonical form, got {canons}"
