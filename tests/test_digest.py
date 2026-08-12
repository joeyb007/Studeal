from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from dealbot.worker.digest import _send_digests


@pytest.mark.asyncio
async def test_digest_skips_free_users():
    """Free users must never receive digest emails."""
    from dealbot.db.models import User

    free_user = User()
    free_user.id = 1
    free_user.email = "free@example.com"
    free_user.is_pro = False

    pro_user = User()
    pro_user.id = 2
    pro_user.email = "pro@example.com"
    pro_user.is_pro = True
    pro_user.email_digest = True    # mock objects skip column defaults

    with (
        patch("dealbot.worker.digest.get_async_session") as mock_session_ctx,
        patch("dealbot.worker.digest._matched_listings_for_user", new_callable=AsyncMock) as mock_match,
        patch("dealbot.worker.digest.send_email", new_callable=AsyncMock) as mock_send,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.return_value = mock_session

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [free_user, pro_user]
        mock_session.execute = AsyncMock(return_value=execute_result)

        mock_listing = MagicMock()
        mock_listing.title = "Test Listing"
        mock_listing.price = 99.99
        mock_listing.currency = "CAD"
        mock_listing.marketplace = "kijiji"
        mock_listing.raw_url = "https://example.com/listing"
        mock_match.return_value = [("My Watchlist", mock_listing)]

        result = await _send_digests()

    assert mock_send.call_count == 1
    assert mock_send.call_args.kwargs["to"] == "pro@example.com"
    assert result["sent"] == 1


# ---------------------------------------------------------------------------
# Pool re-point (Workstream C)
# ---------------------------------------------------------------------------

def test_digest_body_renders_listings():
    from dealbot.db.models import Listing
    from dealbot.notifications.email import build_digest_email

    listing = Listing(
        canonical_url="c1", raw_url="https://kijiji.ca/1",
        marketplace="kijiji", title="Sony WH-1000XM4",
        price=180.0, currency="CAD", condition="used",
    )
    subject, body, html = build_digest_email([("Headphones", listing)])

    assert "1 new match" in subject
    for rendered in (body, html):
        assert "Sony WH-1000XM4" in rendered
        assert "180" in rendered
        assert "https://kijiji.ca/1" in rendered
        assert "kijiji" in rendered
    assert "Headphones" in html, "digest groups rows under the agent name"


def test_digest_no_longer_queries_legacy_deals():
    """The legacy deals table is retiring; the digest must not hold it open."""
    import inspect

    from dealbot.worker import digest

    source = inspect.getsource(digest)
    assert "FROM deals" not in source, "SQL still targets the legacy deals table"
    assert "FROM listings" in source


@pytest.mark.asyncio
async def test_digest_skips_internal_users():
    """The house user is is_pro=True and would otherwise get a daily digest
    bounced off Resend."""
    from dealbot.db.models import User

    house = User()
    house.id = 3
    house.email = "house@studeal.internal"
    house.is_pro = True

    with (
        patch("dealbot.worker.digest.get_async_session") as mock_session_ctx,
        patch("dealbot.worker.digest._matched_listings_for_user", new_callable=AsyncMock) as mock_match,
        patch("dealbot.worker.digest.send_email", new_callable=AsyncMock) as mock_send,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.return_value = mock_session

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [house]
        mock_session.execute = AsyncMock(return_value=execute_result)

        await _send_digests()

    mock_match.assert_not_called()
    mock_send.assert_not_called()
