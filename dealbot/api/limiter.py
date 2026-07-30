from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Rate-limit counters normally live in Redis so limits hold across API replicas.
# RATELIMIT_STORAGE_URI overrides it — "memory://" gives per-process counters,
# which is what tests use (no broker) and an acceptable degraded mode for a
# single-replica deploy.
STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", REDIS_URL)

# Keyed by IP for unauthenticated endpoints (auth), by user ID for authenticated ones
limiter = Limiter(key_func=get_remote_address, storage_uri=STORAGE_URI)
