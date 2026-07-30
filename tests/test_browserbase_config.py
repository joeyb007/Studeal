"""Browserbase session payload config.

Geography is load-bearing: Canadian marketplaces serve different inventory
(or none) to US exit IPs. Stealth knobs stay env-tunable because
advancedStealth is Scale-tier only.
"""

from __future__ import annotations

from dealbot.scrapers.browserbase_session import _session_payload


def test_proxies_pinned_to_canada_by_default(monkeypatch):
    monkeypatch.delenv("BROWSERBASE_PROXY_COUNTRY", raising=False)
    monkeypatch.delenv("BROWSERBASE_PROXY_CITY", raising=False)
    payload = _session_payload("proj", proxies=True)
    assert payload["proxies"] == [
        {"type": "browserbase", "geolocation": {"country": "CA", "city": "TORONTO"}}
    ]


def test_fingerprint_matches_proxy_geography(monkeypatch):
    """IP says Canada, browser must say Canada — a mismatch is a bot signal."""
    monkeypatch.delenv("BROWSERBASE_LOCALE", raising=False)
    fp = _session_payload("proj", proxies=True)["browserSettings"]["fingerprint"]
    assert fp["locales"] == ["en-CA"]
    assert fp["devices"] == ["desktop"]
    assert fp["screen"]["minWidth"] == 1920


def test_verified_is_opt_in_only(monkeypatch):
    """Enterprise-only; sending it below that 403s the entire session."""
    monkeypatch.delenv("BROWSERBASE_VERIFIED", raising=False)
    assert "verified" not in _session_payload("p", True)["browserSettings"]
    monkeypatch.setenv("BROWSERBASE_VERIFIED", "true")
    assert _session_payload("p", True)["browserSettings"]["verified"] is True


def test_proxy_country_override(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_PROXY_COUNTRY", "US")
    payload = _session_payload("proj", proxies=True)
    assert payload["proxies"][0]["geolocation"]["country"] == "US"


def test_no_proxy_key_when_disabled():
    assert "proxies" not in _session_payload("proj", proxies=False)


def test_desktop_viewport_and_ad_blocking():
    settings = _session_payload("proj", proxies=True)["browserSettings"]
    assert settings["viewport"] == {"width": 1920, "height": 1080}
    assert settings["blockAds"] is True
    assert settings["solveCaptchas"] is True



