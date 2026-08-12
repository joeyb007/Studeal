"""Shared email notifications (Resend REST via httpx).

Single send path for digest + alert emails. `send_email` returns True on
2xx so callers can record delivery (alert channel bookkeeping) — failures
are logged, never raised.

Every email ships a styled HTML body plus the plain-text fallback. HTML is
table-based with inline styles (the only layout Gmail/Outlook render
reliably) on a light ground: dark-themed emails get force-inverted
unpredictably by clients, so the brand navy and amber are deepened to read
on paper-white instead.
"""

from __future__ import annotations

import html as _html
import logging
import os

import httpx

from dealbot.db.models import Listing, ListingAlert

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"

# Alert emails cap at this many listing cards; the rest fold into the CTA.
ALERT_EMAIL_MAX_ROWS = 5


def _app_url() -> str:
    return os.environ.get("APP_BASE_URL", "https://studeal.site").rstrip("/")


async def send_email(to: str, subject: str, body: str, html: str | None = None) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("send_email: RESEND_API_KEY not set — skipping email to %s", to)
        return False

    from_address = os.environ.get("RESEND_FROM", "Studeal <alerts@studeal.site>")
    payload: dict = {"from": from_address, "to": [to], "subject": subject, "text": body}
    if html:
        payload["html"] = html
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                _RESEND_API_URL,
                json=payload,
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


# ---------------------------------------------------------------------------
# HTML building blocks (email-safe: tables + inline styles only)
# ---------------------------------------------------------------------------

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_INK = "#1b1a19"
_INK_SOFT = "#6f6e6a"
_INK_FAINT = "#9b9a97"
_LINE = "#e9e6e0"
_NAVY = "#46608f"
_NAVY_SOFT = "#eef1f8"
_AMBER = "#b8860f"
_AMBER_SOFT = "#faf3e3"
_PAPER = "#faf9f7"


def _esc(value: object) -> str:
    return _html.escape(str(value), quote=True)


def email_shell(inner: str, footer: str) -> str:
    """Paper ground, ✦ Studeal brandrow, content, bordered footer."""
    return (
        f'<!doctype html><html><body style="margin:0;padding:0;background:{_PAPER};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{_PAPER};">'
        f'<tr><td align="center" style="padding:28px 16px 0;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;font-family:{_FONT};">'
        f'<tr><td style="padding-bottom:18px;font-size:17px;font-weight:700;color:{_INK};letter-spacing:-0.01em;">'
        f'<span style="color:{_NAVY};">&#10022;</span> Studeal</td></tr>'
        f"{inner}"
        f'<tr><td style="padding:26px 0 32px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid {_LINE};">'
        f'<tr><td style="padding-top:14px;font-size:12px;color:{_INK_FAINT};line-height:1.6;font-family:{_FONT};">{footer}</td></tr>'
        f"</table></td></tr>"
        f"</table></td></tr></table></body></html>"
    )


def _headline(text: str) -> str:
    return (
        f'<tr><td style="font-size:21px;font-weight:650;color:{_INK};'
        f'line-height:1.3;padding:2px 0 4px;">{text}</td></tr>'
    )


def _lede(text: str) -> str:
    return (
        f'<tr><td style="font-size:14.5px;color:{_INK_SOFT};line-height:1.55;'
        f'padding:0 0 18px;">{text}</td></tr>'
    )


def _chip(text: str) -> str:
    return (
        f'<tr><td style="padding:0 0 14px;"><span style="display:inline-block;'
        f"font-size:12px;font-weight:600;color:{_NAVY};background:{_NAVY_SOFT};"
        f'border-radius:20px;padding:4px 12px;">{text}</span></td></tr>'
    )


def _card(inner: str) -> str:
    return (
        f'<tr><td style="padding:0 0 10px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:#ffffff;border:1px solid {_LINE};border-radius:10px;">'
        f'<tr><td style="padding:14px 16px;">{inner}</td></tr>'
        f"</table></td></tr>"
    )


def _cta(label: str, url: str, *, ghost: bool = False) -> str:
    style = (
        f"display:inline-block;font-family:{_FONT};font-size:14px;font-weight:600;"
        f"text-decoration:none;border-radius:8px;padding:11px 22px;"
    )
    style += (
        f"background:transparent;color:{_NAVY};border:1px solid {_NAVY};"
        if ghost else f"background:{_NAVY};color:#ffffff;"
    )
    return f'<a href="{_esc(url)}" style="{style}">{label}</a>'


def _listing_card(
    listing: Listing,
    *,
    reason: str | None = None,
    price_html: str | None = None,
) -> str:
    meta = _esc(listing.marketplace)
    if listing.location:
        meta += f" &middot; {_esc(listing.location)}"
    price = price_html or (
        f'<span style="font-size:16px;font-weight:700;color:{_INK};">'
        f"${listing.price:,.0f}"
        f'<span style="font-size:11.5px;font-weight:600;color:{_INK_FAINT};"> {_esc(listing.currency)}</span></span>'
    )
    inner = (
        f'<a href="{_esc(listing.raw_url)}" style="font-size:14.5px;font-weight:600;'
        f'color:{_INK};text-decoration:none;line-height:1.35;">{_esc(listing.title)}</a>'
        f'<div style="font-size:12.5px;color:{_INK_FAINT};padding:3px 0 6px;font-family:{_FONT};">{meta}</div>'
        f"{price}"
    )
    if reason:
        inner += (
            f'<div style="font-size:13px;color:{_NAVY};line-height:1.45;'
            f"border-left:2px solid {_NAVY};padding-left:10px;margin-top:8px;"
            f'font-family:{_FONT};">{_esc(reason)}</div>'
        )
    return _card(inner)


def unsubscribe_url(user_id: int, email_type: str) -> str:
    """One-click unsubscribe link for this user + email type."""
    from dealbot.api.routes.email_prefs import make_unsubscribe_token

    return f"{_app_url()}/unsubscribe?token={make_unsubscribe_token(user_id, email_type)}"


def _footer(context_line: str, unsub_url: str | None = None) -> str:
    manage = f'<a href="{_app_url()}/settings" style="color:{_INK_SOFT};">Manage emails</a>'
    links = manage
    if unsub_url:
        links += f' &middot; <a href="{_esc(unsub_url)}" style="color:{_INK_SOFT};">Unsubscribe</a>'
    return f"{context_line}<br>{links} &middot; studeal.site"


# ---------------------------------------------------------------------------
# Builders — each returns (subject, text, html); text is the fallback body.
# ---------------------------------------------------------------------------

def build_password_reset_email(reset_url: str) -> tuple[str, str, str]:
    """Transactional reset email — no unsubscribe (account security mail)."""
    subject = "Reset your Studeal password"
    text = "\n".join([
        "Someone asked to reset the password for this Studeal account.",
        "If that was you, use this link within 30 minutes:",
        "",
        reset_url,
        "",
        "If it wasn't you, ignore this email · your password is unchanged.",
    ])
    inner = _headline("Reset your password")
    inner += _lede(
        "Someone asked to reset the password for this account. If that was "
        "you, the link below works for 30 minutes. If not, ignore this "
        "email &middot; your password is unchanged."
    )
    inner += f'<tr><td style="padding:6px 0 0;">{_cta("Choose a new password", reset_url)}</td></tr>'
    html = email_shell(inner, _footer("Sent because a password reset was requested for this address."))
    return subject, text, html


def build_google_account_notice_email() -> tuple[str, str, str]:
    """Reset requested for an account that signs in with Google."""
    subject = "Your Studeal account signs in with Google"
    text = "\n".join([
        "A password reset was requested for this account, but it signs in "
        "with Google · there's no Studeal password to reset.",
        "",
        f"Sign in: {_app_url()}",
    ])
    inner = _headline("This account signs in with Google")
    inner += _lede(
        "A password reset was requested, but this account uses Google "
        "sign-in &middot; there's no Studeal password to reset. Just sign "
        "in with Google as usual."
    )
    inner += f'<tr><td style="padding:6px 0 0;">{_cta("Go to Studeal", _app_url())}</td></tr>'
    html = email_shell(inner, _footer("Sent because a password reset was requested for this address."))
    return subject, text, html


def build_alert_email(
    watchlist_name: str,
    alerts: list[tuple[ListingAlert, Listing]],
    unsub_url: str | None = None,
) -> tuple[str, str, str]:
    """One hunt's new-match summary. Top rows only — a first hunt can surface
    hundreds of matches, and past ALERT_EMAIL_MAX_ROWS they belong on the
    dashboard, not in an email."""
    n = len(alerts)
    shown = alerts[:ALERT_EMAIL_MAX_ROWS]
    more = n - len(shown)
    subject = f"Scout found {n} new match{'es' if n != 1 else ''} for {watchlist_name}"

    # Plain-text fallback.
    lines = [f"Scout pulled {n} new listing{'s' if n != 1 else ''} out of the market:\n"]
    for alert, listing in shown:
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
    if more:
        lines.append(f"…and {more} more on your dashboard.\n")
    lines.append("\nManage your agents at studeal.site")
    text = "\n".join(lines)

    # Styled body.
    inner = _chip(_esc(watchlist_name))
    inner += _headline(f"{n} new find{'s' if n != 1 else ''} worth a look")
    inner += _lede("Scout swept the market and pulled these out for you.")
    for alert, listing in shown:
        inner += _listing_card(listing, reason=alert.reason)
    cta_label = f"See all {n} matches" if more else "Open your agent"
    inner += f'<tr><td style="padding:18px 0 0;">{_cta(cta_label, f"{_app_url()}/dashboard")}</td></tr>'
    html = email_shell(inner, _footer(
        f"You're getting this because your agent <b>{_esc(watchlist_name)}</b> is on the hunt.",
        unsub_url,
    ))
    return subject, text, html


def build_price_drop_email(
    listing: Listing,
    old_price: float,
    *,
    inspected_on: str | None = None,
    unsub_url: str | None = None,
) -> tuple[str, str, str]:
    """The friend-remembered-you-asked email. One listing; the delta is the
    story."""
    subject = f"That {listing.title[:48]} you looked at just dropped to ${listing.price:.0f}"
    pct = round((old_price - listing.price) / old_price * 100) if old_price else 0

    when = f" on {inspected_on}" if inspected_on else ""
    text = "\n".join([
        f"You had Scout look this over{when} at ${old_price:.0f}. "
        f"It just dropped to ${listing.price:.0f} ({listing.marketplace}).",
        "",
        listing.title,
        listing.raw_url,
        "",
        "Worth another look before someone else grabs it.",
        "",
        "Manage your agents at studeal.site",
    ])

    price_html = (
        f'<span style="font-size:14px;color:{_INK_FAINT};text-decoration:line-through;'
        f'margin-right:8px;">${old_price:,.0f}</span>'
        f'<span style="font-size:22px;font-weight:750;color:{_INK};">${listing.price:,.0f}</span>'
        f'<span style="display:inline-block;font-size:12px;font-weight:700;color:{_AMBER};'
        f"background:{_AMBER_SOFT};border-radius:6px;padding:3px 8px;margin-left:10px;"
        f'vertical-align:3px;">&minus;{pct}%</span>'
    )
    inner = _headline("Remember this one? It just got cheaper.")
    inner += _lede(f"You had Scout look it over{_esc(when) if when else ''}. The seller cut the price.")
    inner += _listing_card(
        listing, price_html=price_html,
        reason="Worth another look before someone else grabs it.",
    )
    inner += (
        f'<tr><td style="padding:18px 0 0;">'
        f'{_cta("Open the listing", listing.raw_url)}'
        f'<span style="display:inline-block;width:8px;"></span>'
        f'{_cta("Ask Scout", f"{_app_url()}/scout", ghost=True)}'
        f"</td></tr>"
    )
    html = email_shell(inner, _footer(
        "Price watch set when Scout inspected this listing.", unsub_url,
    ))
    return subject, text, html


def build_digest_email(
    matches: list[tuple[str, Listing]],
    unsub_url: str | None = None,
) -> tuple[str, str, str]:
    """Daily Pro digest: compact rows grouped per agent, one CTA."""
    n = len(matches)
    subject = f"Your agents overnight: {n} new match{'es' if n != 1 else ''}"

    lines = ["Here are your Studeal matches from the last 24 hours:\n"]
    for watchlist_name, listing in matches:
        lines.append(
            f"• [{watchlist_name}] {listing.title}\n"
            f"  ${listing.price:.2f} {listing.currency} · {listing.marketplace}\n"
            f"  {listing.raw_url}\n"
        )
    lines.append("\nManage your agents at studeal.site")
    text = "\n".join(lines)

    # Group in input order (already best-similarity-first) by watchlist.
    grouped: dict[str, list[Listing]] = {}
    for watchlist_name, listing in matches:
        grouped.setdefault(watchlist_name, []).append(listing)

    inner = _headline(f"While you slept &middot; {n} new match{'es' if n != 1 else ''}")
    inner += _lede("Your agents hunted overnight. The standouts:")
    for watchlist_name, listings in grouped.items():
        inner += (
            f'<tr><td style="font-size:12px;font-weight:700;letter-spacing:0.06em;'
            f"text-transform:uppercase;color:{_INK_SOFT};padding:14px 0 6px;"
            f'font-family:{_FONT};">{_esc(watchlist_name)} '
            f'<span style="color:{_NAVY};">&middot; {len(listings)} new</span></td></tr>'
        )
        rows = ""
        for listing in listings:
            rows += (
                f'<tr><td style="padding:7px 0;border-bottom:1px solid {_LINE};'
                f'font-size:13.5px;color:{_INK};font-family:{_FONT};">'
                f'<a href="{_esc(listing.raw_url)}" style="color:{_INK};text-decoration:none;">'
                f"{_esc(listing.title)}</a> "
                f'<span style="color:{_INK_FAINT};font-size:12px;">&middot; {_esc(listing.marketplace)}</span></td>'
                f'<td align="right" style="padding:7px 0;border-bottom:1px solid {_LINE};'
                f'font-size:13.5px;font-weight:650;color:{_INK};font-family:{_FONT};white-space:nowrap;">'
                f"${listing.price:,.0f}</td></tr>"
            )
        inner += (
            f'<tr><td><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{rows}</table></td></tr>'
        )
    inner += f'<tr><td style="padding:18px 0 0;">{_cta("Open your dashboard", f"{_app_url()}/dashboard")}</td></tr>'
    html = email_shell(inner, _footer(
        "Daily digest &middot; sent when your agents found something overnight.",
        unsub_url,
    ))
    return subject, text, html
