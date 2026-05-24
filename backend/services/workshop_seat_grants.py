"""Workshop cohort seat grants — single place for policy + cap checks."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("workshops.seat_grants")


def payment_allows_grants(
    payment_status: str,
    access_policy: str,
) -> bool:
    if access_policy == "demo":
        return True
    return payment_status in ("paid", "waived")


def compute_entitlement_valid_until(workshop: Mapping[str, Any]) -> datetime | None:
    end_at = workshop.get("end_at")
    if end_at is None:
        return None
    if isinstance(end_at, datetime):
        return end_at
    return None


async def assert_can_grant_seat(
    pg: AsyncSession,
    workshop: Mapping[str, Any],
    *,
    workshop_id: str,
) -> None:
    """
    Raises HTTPException if this workshop cannot accept another seat grant.
    Caller must load workshop row including: status, seat_cap, used_seats, payment_status,
    access_policy, start_at, end_at.
    """
    if workshop.get("status") == "archived":
        raise HTTPException(status_code=400, detail="Workshop is archived")

    if not payment_allows_grants(
        str(workshop.get("payment_status") or ""),
        str(workshop.get("access_policy") or "requires_payment"),
    ):
        raise HTTPException(
            status_code=400,
            detail="Payment must be paid or waived (or workshop in demo policy) before granting seats",
        )

    now = datetime.now(timezone.utc)
    start_at = workshop.get("start_at")
    if start_at and isinstance(start_at, datetime) and start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=timezone.utc)
    if start_at and now < start_at:
        raise HTTPException(status_code=400, detail="Workshop has not started yet")

    end_at = workshop.get("end_at")
    if end_at and isinstance(end_at, datetime) and end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    if end_at and now > end_at:
        raise HTTPException(status_code=400, detail="Workshop end date has passed")

    used = int(workshop.get("used_seats") or 0)
    cap = int(workshop.get("seat_cap") or 0)
    if cap <= 0:
        raise HTTPException(status_code=400, detail="Seat cap is zero")
    if used >= cap:
        raise HTTPException(status_code=400, detail="Seat cap reached")

    # Double-check count in DB (race with concurrent grants)
    res = await pg.execute(
        text(
            """
            SELECT COUNT(*)::int FROM entitlements
            WHERE workshop_id = CAST(:wid AS uuid) AND status = 'active'
            """
        ),
        {"wid": workshop_id},
    )
    fr = res.fetchone()
    cnt = int(fr[0]) if fr else 0
    if int(cnt) >= cap:
        raise HTTPException(status_code=400, detail="Seat cap reached")


async def grant_workshop_seat(
    pg: AsyncSession,
    *,
    workshop_id: str,
    learner_user_id: str,
    content_id: str,
    valid_until: datetime | None,
) -> dict[str, Any]:
    """
    Insert workshop-scoped entitlement. Caller must have called assert_can_grant_seat first.
    """
    await pg.execute(
        text(
            """
            INSERT INTO entitlements (
                user_id, content_id, workshop_id, status, valid_from, valid_until
            )
            VALUES (
                CAST(:uid AS uuid),
                CAST(:cid AS uuid),
                CAST(:wid AS uuid),
                'active',
                now(),
                :vu
            )
            ON CONFLICT (user_id, content_id, workshop_id)
            WHERE (workshop_id IS NOT NULL)
            DO UPDATE SET
                status = 'active',
                valid_from = EXCLUDED.valid_from,
                valid_until = COALESCE(EXCLUDED.valid_until, entitlements.valid_until)
            """
        ),
        {
            "uid": learner_user_id,
            "cid": content_id,
            "wid": workshop_id,
            "vu": valid_until,
        },
    )
    return {
        "workshop_id": workshop_id,
        "user_id": learner_user_id,
        "content_id": content_id,
        "valid_until": valid_until.isoformat() if valid_until else None,
    }
