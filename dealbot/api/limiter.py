from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Keyed by IP for unauthenticated endpoints (auth), by user ID for authenticated ones
limiter = Limiter(key_func=get_remote_address, storage_uri=REDIS_URL)
