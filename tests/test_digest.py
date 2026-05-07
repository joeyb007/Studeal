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

    with (
        patch("dealbot.worker.digest.get_async_session") as mock_session_ctx,
        patch("dealbot.worker.digest._matched_deals_for_user", new_callable=AsyncMock) as mock_match,
        patch("dealbot.worker.digest._send_email", new_callable=AsyncMock) as mock_send,
    ):
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_ctx.return_value = mock_session

        execute_result = MagicMock()
        execute_result.scalars.return_value.all.return_value = [free_user, pro_user]
        mock_session.execute = AsyncMock(return_value=execute_result)

        mock_deal = MagicMock()
        mock_deal.real_discount_pct = 20.0
        mock_deal.score = 75
        mock_deal.sale_price = 99.99
        mock_deal.url = "https://example.com/deal"
        mock_deal.title = "Test Deal"
        mock_match.return_value = [("My Watchlist", mock_deal)]

        result = await _send_digests()

    assert mock_send.call_count == 1
    assert mock_send.call_args.kwargs["to"] == "pro@example.com"
    assert result["sent"] == 1
