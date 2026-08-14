"""AgentCore browser helpers + shared session semaphore.

Amazon Bedrock AgentCore Browser is the AWS-native managed browser: starting
a session returns a CDP WebSocket endpoint that Playwright connects to like
any remote Chromium (SigV4-signed headers). Compute and bandwidth land on
the AWS bill, so sessions burn credits instead of third-party metered GB —
the whole reason this backend exists (2026-08-13: Browserbase residential
proxies ran ~$12/GB, ~100 MB per browser-hour with images loading).

What this backend does NOT provide: residential IP reputation (sessions exit
from AWS datacenter IPs) and Browserbase's stealth fingerprint. Sites that
punish either stay on the browserbase backend (MarketplaceConfig.backend).

Deliberately SDK-free: the official `bedrock-agentcore` package requires
botocore>=1.43 while every aioboto3 release pins aiobotocore's much older
botocore ceiling — co-installation is unresolvable (verified 2026-08-14).
The three calls we need (start/stop session, SigV4 ws headers) run fine on
the pinned botocore (browser ops verified present in 1.40.61), so we make
them directly. Mirrors browserbase_session.py's split: this module owns the
HTTP calls + concurrency cap; the lifecycle class lives in
browser_session.py as AgentCoreBrowserSession.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

AGENTCORE_MAX_SESSIONS = int(os.environ.get("AGENTCORE_MAX_SESSIONS", "24"))

# The account-default managed browser sandbox.
_BROWSER_IDENTIFIER = os.environ.get("AGENTCORE_BROWSER_ID", "aws.browser.v1")

_session_sem: asyncio.Semaphore | None = None


def agentcore_region() -> str:
    return os.environ.get("AGENTCORE_REGION") or os.environ.get("AWS_REGION", "us-east-1")


def get_session_sem() -> asyncio.Semaphore:
    """Process-wide semaphore capping concurrent AgentCore sessions.

    Lazy because asyncio.Semaphore needs a running event loop.
    """
    global _session_sem
    if _session_sem is None:
        _session_sem = asyncio.Semaphore(AGENTCORE_MAX_SESSIONS)
    return _session_sem


@dataclass
class AgentCoreSession:
    """Handle for one remote browser session (needed to stop it later)."""

    region: str
    identifier: str
    session_id: str
    client: Any  # boto3 "bedrock-agentcore" data-plane client


def _sign_ws_headers(region: str, host: str, path: str, session_id: str) -> dict[str, str]:
    """SigV4-signed WebSocket upgrade headers for the automation stream.

    Same construction as the official SDK's generate_ws_headers: sign a GET
    for the https flavor of the stream URL, then carry the signature into
    the websocket upgrade request.
    """
    import boto3
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    credentials = boto3.Session().get_credentials()
    if credentials is None:
        raise RuntimeError("No AWS credentials found for AgentCore browser")
    frozen = credentials.get_frozen_credentials()

    request = AWSRequest(
        method="GET",
        url=f"https://{host}{path}",
        headers={
            "host": host,
            "x-amz-date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        },
    )
    SigV4Auth(frozen, "bedrock-agentcore", region).add_auth(request)

    headers = {
        "Host": host,
        "X-Amz-Date": request.headers["x-amz-date"],
        "Authorization": request.headers["Authorization"],
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": base64.b64encode(secrets.token_bytes(16)).decode(),
        "User-Agent": f"BrowserSandbox-Client/1.0 (Session: {session_id})",
    }
    if frozen.token:
        headers["X-Amz-Security-Token"] = frozen.token
    return headers


async def open_browser() -> tuple[AgentCoreSession, str, dict[str, str]]:
    """Start a session; returns (handle, cdp_ws_url, signed headers).

    Counts against the shared daily session cap — credits are still spend,
    and the cap keeps a runaway fleet visible either way. boto3 is sync, so
    calls run in a thread.
    """
    from dealbot.costs import build_meter

    meter = build_meter()
    if not await meter.session_cap_ok():
        raise RuntimeError("daily browser-session cap reached")
    await meter.record_session()

    region = agentcore_region()
    timeout_s = int(os.environ.get("AGENTCORE_SESSION_TIMEOUT_S", "900"))

    def _start() -> tuple[AgentCoreSession, str, dict[str, str]]:
        import boto3

        client = boto3.client("bedrock-agentcore", region_name=region)
        resp = client.start_browser_session(
            browserIdentifier=_BROWSER_IDENTIFIER,
            sessionTimeoutSeconds=timeout_s,
        )
        handle = AgentCoreSession(
            region=region,
            identifier=resp["browserIdentifier"],
            session_id=resp["sessionId"],
            client=client,
        )
        host = client.meta.endpoint_url.replace("https://", "")
        path = f"/browser-streams/{handle.identifier}/sessions/{handle.session_id}/automation"
        headers = _sign_ws_headers(region, host, path, handle.session_id)
        return handle, f"wss://{host}{path}", headers

    return await asyncio.to_thread(_start)


async def close_browser(handle: AgentCoreSession) -> None:
    """Best-effort release; the session timeout backstops a failed stop."""
    try:
        await asyncio.to_thread(
            handle.client.stop_browser_session,
            browserIdentifier=handle.identifier,
            sessionId=handle.session_id,
        )
    except Exception as exc:
        logger.debug("agentcore: failed to stop session %s: %s", handle.session_id, exc)
