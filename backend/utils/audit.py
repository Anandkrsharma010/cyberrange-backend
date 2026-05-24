# backend/utils/audit.py
"""
Token audit log writer.

Single coroutine log_token_event() used by all three event sites:
  - 'issued'          → sso_callback in routers/auth.py
  - 'revoked'         → logout in routers/auth.py
  - 'join_key_issued' → join_deployment in routers/labs.py

Errors are logged but never propagated — a failed audit write must
never break authentication or lab access.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("audit")


async def log_token_event(
    pg: AsyncSession,
    *,
    user_id: str,
    jti: str,
    event: str,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Insert one row into token_audit_log.

    The caller is responsible for committing the surrounding transaction —
    we deliberately do not commit here so the audit write and the business
    operation succeed or fail atomically.
    """
    try:
        await pg.execute(
            text("""
                INSERT INTO token_audit_log
                    (user_id, jti, event, ip_address, user_agent)
                VALUES
                    (:user_id, :jti, :event, :ip_address, :user_agent)
            """),
            {
                "user_id":    user_id,
                "jti":        jti,
                "event":      event,
                "ip_address": ip_address,
                "user_agent": user_agent,
            },
        )
        log.debug(
            "Audit event recorded: event=%s user_id=%s jti=%s ip=%s",
            event, user_id, jti, ip_address,
        )
    except Exception as exc:
        log.error(
            "Audit log write failed (non-fatal): event=%s user_id=%s jti=%s error=%s",
            event, user_id, jti, exc,
        )