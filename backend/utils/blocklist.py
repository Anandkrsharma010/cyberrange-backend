"""
backend/utils/blocklist.py

Redis-backed JWT blocklist.

Every revoked token's `jti` is stored in Redis with a TTL equal to the
token's remaining lifetime. Once the token would have expired naturally,
the entry is also gone from Redis — no unbounded growth.

Usage
-----
# Revoke a token (call on logout or account disable)
await revoke_token(jti="uuid...", expires_at=datetime(...))

# Check inside decode_token (called automatically via is_token_revoked)
if await is_token_revoked(jti):
    raise HTTPException(401, "Token has been revoked")

Configuration
-------------
Add to backend/.env:
    REDIS_URL=redis://localhost:6379/0

Add to requirements.txt:
    redis==5.2.1
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis

from backend.config import get_settings

log = logging.getLogger("blocklist")

# ── Module-level singleton ────────────────────────────────────────────────────

_redis: Optional[aioredis.Redis] = None

_BLOCKLIST_PREFIX = "jti:revoked:"


def get_redis() -> aioredis.Redis:
    """
    Returns the process-wide Redis client, creating it on first call.
    Uses connection pooling under the hood (redis-py default).
    """
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _redis


async def close_redis() -> None:
    """
    Close the Redis connection pool. Call from FastAPI lifespan shutdown.
    """
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


# ── Core blocklist operations ─────────────────────────────────────────────────

async def revoke_token(jti: str, expires_at: datetime) -> None:
    """
    Add a token's jti to the blocklist with a TTL equal to its remaining
    lifetime. After natural expiry, the Redis key disappears automatically.

    Args:
        jti:        The JWT ID claim from the token payload.
        expires_at: The `exp` claim as a timezone-aware datetime.
    """
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    ttl_seconds = int((expires_at - now).total_seconds())
    if ttl_seconds <= 0:
        # Token already expired — nothing to blocklist.
        return

    key = f"{_BLOCKLIST_PREFIX}{jti}"
    redis = get_redis()

    try:
        await redis.setex(key, ttl_seconds, "1")
        log.info("Token revoked: jti=%s ttl=%ds", jti, ttl_seconds)
    except Exception as exc:
        # Log and re-raise so the caller can decide whether to hard-fail.
        log.error("Redis blocklist write failed: jti=%s error=%s", jti, exc)
        raise


async def is_token_revoked(jti: str) -> bool:
    """
    Returns True if the jti is on the blocklist (token was revoked).
    Returns False on Redis failure so the system degrades gracefully
    rather than locking everyone out — log the error for alerting.

    In a higher-security context you may want to invert this (fail closed).
    """
    key = f"{_BLOCKLIST_PREFIX}{jti}"
    redis = get_redis()

    try:
        result = await redis.exists(key)
        return bool(result)
    except Exception as exc:
        log.error(
            "Redis blocklist read failed — failing open: jti=%s error=%s",
            jti, exc,
        )
        return False