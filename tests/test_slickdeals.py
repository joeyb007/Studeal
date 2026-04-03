from __future__ import annotations

import pytest
import respx
import httpx

from dealbot.scrapers.slickdeals import SlickdealsAdapter, SLICKDEALS_RSS

# Minimal valid RSS feed with two entries — one with two prices, one with one price
MOCK_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Slickdeals</title>
    <link>https://slickdeals.net</link>
    <item>
      <title>Sony WH-1000XM5 Headphones $174.99 (was $349.99)</title>
      <link>https://slickdeals.net/f/123-sony-headphones</link>
      <description>Great deal on Sony headphones. Was $349.99, now $174.99.</description>
    </item>
    <item>
      <title>Amazon Echo Dot $22.99</title>
      <link>https://slickdeals.net/f/456-echo-dot</link>
      <description>Echo Dot on sale for $22.99.</description>
    </item>
    <item>
      <title>No price deal here</title>
      <link>https://slickdeals.net/f/789-no-price</link>
      <description>This deal has no price information.</description>
    </item>
  </channel>
</rss>"""


@pytest.mark.asyncio
@respx.mock
async def test_fetch_parses_deals():
    """Adapter fetches RSS and returns normalised DealRaw list."""
    respx.get(SLICKDEALS_RSS).mock(return_value=httpx.Response(200, text=MOCK_RSS))

    adapter = SlickdealsAdapter()
    deals = await adapter.fetch()

    assert len(deals) == 3
    assert all(d.source == "slickdeals" for d in deals)


@pytest.mark.asyncio
@respx.mock
async def test_fetch_extracts_two_prices():
    """Entry with two prices: higher becomes listed, lower becomes sale."""
    respx.get(SLICKDEALS_RSS).mock(return_value=httpx.Response(200, text=MOCK_RSS))

    adapter = SlickdealsAdapter()
    deals = await adapter.fetch()

    sony = deals[0]
    assert sony.listed_price == 349.99
    assert sony.sale_price == 174.99
    assert sony.title == "Sony WH-1000XM5 Headphones $174.99 (was $349.99)"


@pytest.mark.asyncio
@respx.mock
async def test_fetch_single_price_sets_both():
    """Entry with one price sets listed and sale to the same value."""
    respx.get(SLICKDEALS_RSS).mock(return_value=httpx.Response(200, text=MOCK_RSS))

    adapter = SlickdealsAdapter()
    deals = await adapter.fetch()

    echo = deals[1]
    assert echo.listed_price == 22.99
    assert echo.sale_price == 22.99


@pytest.mark.asyncio
@respx.mock
async def test_fetch_no_price_uses_sentinel():
    """Entry with no price uses 0.0 sentinel values."""
    respx.get(SLICKDEALS_RSS).mock(return_value=httpx.Response(200, text=MOCK_RSS))

    adapter = SlickdealsAdapter()
    deals = await adapter.fetch()

    no_price = deals[2]
    assert no_price.listed_price == 0.0
    assert no_price.sale_price == 0.0


@pytest.mark.asyncio
@respx.mock
async def test_fetch_raises_on_http_error():
    """Adapter propagates HTTP errors."""
    respx.get(SLICKDEALS_RSS).mock(return_value=httpx.Response(503))

    adapter = SlickdealsAdapter()
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.fetch()
