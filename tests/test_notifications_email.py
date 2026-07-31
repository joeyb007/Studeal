"""Tests for the shared email notification module (Resend REST)."""

from __future__ import annotations

import httpx
import pytest
import respx

from dealbot.db.models import Listing, ListingAlert
from dealbot.notifications.email import build_alert_email, send_email

RESEND_URL = "https://api.resend.com/emails"


@pytest.mark.asyncio
@respx.mock
async def test_send_email_posts_to_resend(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    route = respx.post(RESEND_URL).mock(
        return_value=httpx.Response(200, json={"id": "email_1"}),
    )
    ok = await send_email("user@example.com", "subject line", "body text")
    assert ok is True
    assert route.called
    payload = route.calls[0].request.read()
    assert b"user@example.com" in payload
    assert b"subject line" in payload


@pytest.mark.asyncio
@respx.mock
async def test_send_email_returns_false_on_error(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    respx.post(RESEND_URL).mock(return_value=httpx.Response(500, text="boom"))
    assert await send_email("user@example.com", "s", "b") is False


@pytest.mark.asyncio
async def test_send_email_skips_without_key(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    assert await send_email("user@example.com", "s", "b") is False


def test_build_alert_email_format():
    listing = Listing(
        canonical_url="https://k.ca/1", raw_url="https://k.ca/1?utm=x",
        marketplace="kijiji", title="Herman Miller Aeron", price=420.0,
        currency="CAD",
    )
    alert = ListingAlert(
        user_id=1, watchlist_id=1, listing_id=1, hunt_id=1, score=0.91,
    )
    subject, body = build_alert_email("Aeron watch", [(alert, listing)])
    assert subject == "Studeal: 1 new match for Aeron watch"
    assert "Herman Miller Aeron" in body
    assert "$420.00 CAD" in body
    assert "https://k.ca/1?utm=x" in body
    assert "studeal.site" in body


def test_build_alert_email_pluralizes():
    listings = [
        Listing(canonical_url=f"c{i}", raw_url=f"c{i}", marketplace="kijiji",
                title=f"item {i}", price=float(i), currency="CAD")
        for i in range(2)
    ]
    alerts = [
        (ListingAlert(user_id=1, watchlist_id=1, listing_id=i, hunt_id=1, score=0.5), listing)
        for i, listing in enumerate(listings)
    ]
    subject, _ = build_alert_email("wl", alerts)
    assert subject == "Studeal: 2 new matches for wl"


def test_alert_email_renders_the_ranker_reason():
    """The reason is why the alert is worth opening. The UI renders it; the
    email must too, or the most persuasive part stays behind a login."""
    listing = Listing(
        canonical_url="https://k.ca/2", raw_url="https://k.ca/2",
        marketplace="kijiji", title="Sony WH-1000XM4", price=180.0,
        currency="CAD",
    )
    alert = ListingAlert(
        user_id=1, watchlist_id=1, listing_id=2, hunt_id=1, score=0.88,
        reason="Over-ear ANC, well under your budget.",
    )
    _subject, body = build_alert_email("Headphones", [(alert, listing)])
    assert "Over-ear ANC, well under your budget." in body


def test_alert_email_omits_a_missing_reason_cleanly():
    """Ranking degrades to retrieval order with no reasons; the email must not
    print a dangling label or the string None."""
    listing = Listing(
        canonical_url="https://k.ca/3", raw_url="https://k.ca/3",
        marketplace="kijiji", title="Aeron chair", price=400.0, currency="CAD",
    )
    alert = ListingAlert(
        user_id=1, watchlist_id=1, listing_id=3, hunt_id=1, score=0.0, reason=None,
    )
    _subject, body = build_alert_email("Chairs", [(alert, listing)])
    assert "None" not in body
    assert "Aeron chair" in body
