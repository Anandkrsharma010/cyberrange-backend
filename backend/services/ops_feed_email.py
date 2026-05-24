"""Email / pager fan-out for operations_feed (contract only — Phase 2).

Implement ``OpsFeedEmailAdapter`` in a worker or notification service when you
are ready to send mail. The API process does not call this by default.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


@runtime_checkable
class OpsFeedEmailAdapter(Protocol):
    """
    Adapter boundary: given a persisted feed row, decide whether to notify
    and enqueue outbound email (or another channel).

    Callers should pass the same ``id`` returned by ``GET /admin/ops-feed``.
    """

    async def should_send(
        self,
        *,
        event_type: str,
        severity: str,
        metadata: dict[str, Any],
    ) -> bool:
        """Return True if this event type should generate an outbound message."""
        ...

    async def queue_delivery(self, pg: AsyncSession, *, feed_row_id: str) -> None:
        """Persist a queue row or invoke your mailer — must be idempotent per feed_row_id."""
        ...
