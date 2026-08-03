"""Shared email notifications (Resend REST via httpx).

Single send path for digest + alert emails. `send_email` returns True on
2xx so callers can record delivery (alert channel bookkeeping) — failures
are logged, never raised.
"""

from __future__ import annotations

import logging
import os

import httpx

from dealbot.db.models import Listing, ListingAlert

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


async def send_email(to: str, subject: str, body: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("send_email: RESEND_API_KEY not set — skipping email to %s", to)
        return False

    from_address = os.environ.get("RESEND_FROM", "Studeal <alerts@studeal.site>")
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                _RESEND_API_URL,
                json={"from": from_address, "to": [to], "subject": subject, "text": body},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            logger.info("send_email: sent to %s (id=%s)", to, resp.json().get("id"))
            return True
        except httpx.HTTPStatusError as exc:
            logger.error(
                "send_email: Resend error %d for %s — %s",
                exc.response.status_code, to, exc.response.text,
            )
        except Exception:
            logger.exception("send_email: failed to send to %s", to)
    return False


def build_alert_email(
    watchlist_name: str,
    alerts: list[tuple[ListingAlert, Listing]],
) -> tuple[str, str]:
    """→ (subject, body) for one hunt's new-match summary email."""
    n = len(alerts)
    subject = f"Studeal: {n} new match{'es' if n != 1 else ''} for {watchlist_name}"
    lines = [f"Your agent found {n} new listing{'s' if n != 1 else ''}:\n"]
    for alert, listing in alerts:
        entry = (
            f"• {listing.title} · ${listing.price:.2f} {listing.currency}"
            f" ({listing.marketplace})"
        )
        # The ranker's one-liner is the most persuasive part of the alert.
        # Absent when ranking degraded to retrieval order — omit the line
        # entirely rather than printing an empty label.
        if alert.reason:
            entry += f"\n  {alert.reason}"
        lines.append(f"{entry}\n  {listing.raw_url}\n")
    lines.append("\nManage your agents at studeal.site")
    return subject, "\n".join(lines)
