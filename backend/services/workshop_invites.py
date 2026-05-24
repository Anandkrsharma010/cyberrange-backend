"""Workshop cohort invites — create, list, resend, revoke, preview, redeem."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from email_validator import EmailNotValidError, validate_email
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.services.email_delivery import send_workshop_invite_email
from backend.services.workshop_invite_tokens import hash_token, new_raw_token
from backend.services.workshop_seat_grants import (
    assert_can_grant_seat,
    compute_entitlement_valid_until,
    grant_workshop_seat as insert_workshop_entitlement,
)

log = logging.getLogger("workshop_invites")


def normalize_invite_email(raw: str) -> str:
    try:
        return validate_email(raw.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError as e:
        raise HTTPException(status_code=400, detail=f"Invalid email: {e}") from e


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if not domain:
        return "***"
    head = local[:2] if len(local) >= 2 else (local[:1] if local else "")
    return f"{head}***@{domain}"


def invite_url_for_token(raw_token: str) -> str:
    base = get_settings().FRONTEND_PUBLIC_URL.rstrip("/")
    return f"{base}/course-invite?token={raw_token}"


async def count_pending_invites(pg: AsyncSession, workshop_id: str) -> int:
    r = await pg.execute(
        text(
            """
            SELECT COUNT(*)::int FROM workshop_invites
            WHERE workshop_id = CAST(:wid AS uuid) AND status = 'pending'
            """
        ),
        {"wid": workshop_id},
    )
    row = r.fetchone()
    return int(row[0]) if row else 0


async def load_workshop_row(pg: AsyncSession, workshop_id: str) -> dict[str, Any] | None:
    wr = await pg.execute(
        text(
            """
            SELECT
                w.id, w.content_id, w.start_at, w.end_at, w.seat_cap, w.used_seats,
                w.payment_status, w.access_policy, w.status, w.title
            FROM workshops w
            WHERE w.id = CAST(:wid AS uuid)
            """
        ),
        {"wid": workshop_id},
    )
    row = wr.mappings().first()
    return dict(row) if row else None


async def assert_room_for_pending_invite(
    pg: AsyncSession, workshop: dict[str, Any], workshop_id: str
) -> None:
    """Pending invites reserve capacity alongside active entitlements."""
    await assert_can_grant_seat(pg, workshop, workshop_id=workshop_id)
    pending = await count_pending_invites(pg, workshop_id)
    used = int(workshop.get("used_seats") or 0)
    cap = int(workshop.get("seat_cap") or 0)
    if used + pending >= cap:
        raise HTTPException(
            status_code=400,
            detail="No seats left for new invites (active seats plus pending invites meet the cap).",
        )


async def create_invite(
    pg: AsyncSession,
    *,
    workshop_id: str,
    email: str,
    invited_by: str,
) -> dict[str, Any]:
    settings = get_settings()
    workshop = await load_workshop_row(pg, workshop_id)
    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    await assert_room_for_pending_invite(pg, workshop, workshop_id)

    raw = new_raw_token()
    th = hash_token(raw)
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=max(1, int(settings.WORKSHOP_INVITE_EXPIRE_DAYS))
    )
    iid = None
    try:
        ins = await pg.execute(
            text(
                """
                INSERT INTO workshop_invites (
                    workshop_id, email, token_hash, status, invited_by, expires_at
                )
                VALUES (
                    CAST(:wid AS uuid), :email, :th, 'pending',
                    CAST(:inv_by AS uuid), :exp
                )
                RETURNING id
                """
            ),
            {
                "wid": workshop_id,
                "email": email,
                "th": th,
                "inv_by": invited_by,
                "exp": expires_at,
            },
        )
        row = ins.fetchone()
        iid = str(row[0]) if row else None
    except IntegrityError as exc:
        await pg.rollback()
        log.info("Duplicate pending invite: workshop=%s email=%s", workshop_id, email)
        raise HTTPException(
            status_code=409,
            detail="A pending invite already exists for this email on this cohort.",
        ) from exc

    if not iid:
        raise HTTPException(status_code=500, detail="Failed to create invite")

    url = invite_url_for_token(raw)
    ok, err = await send_workshop_invite_email(
        to_addr=email,
        workshop_title=str(workshop.get("title") or "Workshop"),
        invite_url=url,
    )
    now = datetime.now(timezone.utc)
    if ok:
        await pg.execute(
            text(
                """
                UPDATE workshop_invites
                SET email_sent_at = :ts, last_email_error = NULL, updated_at = :ts
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"ts": now, "id": iid},
        )
    else:
        await pg.execute(
            text(
                """
                UPDATE workshop_invites
                SET last_email_error = :err, updated_at = :ts
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"err": (err or "send failed")[:2000], "ts": now, "id": iid},
        )

    await pg.commit()
    return {
        "invite_id": iid,
        "email": email,
        "expires_at": expires_at,
        "invite_url": url,
        "email_dispatched": ok,
        "email_error": err,
    }


async def list_invites(pg: AsyncSession, workshop_id: str) -> list[dict[str, Any]]:
    r = await pg.execute(
        text(
            """
            SELECT id, email, status, invited_by, accepted_user_id, accepted_at,
                   expires_at, email_sent_at, last_email_error, created_at, updated_at
            FROM workshop_invites
            WHERE workshop_id = CAST(:wid AS uuid)
            ORDER BY created_at DESC
            LIMIT 200
            """
        ),
        {"wid": workshop_id},
    )
    out = []
    for row in r.mappings().all():
        d = dict(row)
        d["id"] = str(d["id"])
        d["invited_by"] = str(d["invited_by"]) if d.get("invited_by") else None
        d["accepted_user_id"] = (
            str(d["accepted_user_id"]) if d.get("accepted_user_id") else None
        )
        out.append(d)
    return out


async def resend_invite(
    pg: AsyncSession, *, workshop_id: str, invite_id: str
) -> dict[str, Any]:
    workshop = await load_workshop_row(pg, workshop_id)
    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    ir = await pg.execute(
        text(
            """
            SELECT id, email, status, workshop_id
            FROM workshop_invites
            WHERE id = CAST(:iid AS uuid) AND workshop_id = CAST(:wid AS uuid)
            """
        ),
        {"iid": invite_id, "wid": workshop_id},
    )
    inv = ir.mappings().first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    if inv["status"] != "pending":
        raise HTTPException(status_code=400, detail="Only pending invites can be resent")

    raw = new_raw_token()
    th = hash_token(raw)
    settings = get_settings()
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=max(1, int(settings.WORKSHOP_INVITE_EXPIRE_DAYS))
    )
    now = datetime.now(timezone.utc)
    await pg.execute(
        text(
            """
            UPDATE workshop_invites
            SET token_hash = :th, expires_at = :exp, updated_at = :ts
            WHERE id = CAST(:iid AS uuid) AND status = 'pending'
            """
        ),
        {"th": th, "exp": expires_at, "ts": now, "iid": invite_id},
    )

    url = invite_url_for_token(raw)
    ok, err = await send_workshop_invite_email(
        to_addr=str(inv["email"]),
        workshop_title=str(workshop.get("title") or "Workshop"),
        invite_url=url,
    )
    if ok:
        await pg.execute(
            text(
                """
                UPDATE workshop_invites
                SET email_sent_at = :ts, last_email_error = NULL, updated_at = :ts
                WHERE id = CAST(:iid AS uuid)
                """
            ),
            {"ts": now, "iid": invite_id},
        )
    else:
        await pg.execute(
            text(
                """
                UPDATE workshop_invites
                SET last_email_error = :err, updated_at = :ts
                WHERE id = CAST(:iid AS uuid)
                """
            ),
            {"err": (err or "send failed")[:2000], "ts": now, "iid": invite_id},
        )
    await pg.commit()
    return {
        "invite_id": invite_id,
        "invite_url": url,
        "expires_at": expires_at,
        "email_dispatched": ok,
        "email_error": err,
    }


async def revoke_invite(
    pg: AsyncSession, *, workshop_id: str, invite_id: str
) -> None:
    res = await pg.execute(
        text(
            """
            UPDATE workshop_invites
            SET status = 'revoked', updated_at = now()
            WHERE id = CAST(:iid AS uuid)
              AND workshop_id = CAST(:wid AS uuid)
              AND status = 'pending'
            RETURNING id
            """
        ),
        {"iid": invite_id, "wid": workshop_id},
    )
    if not res.fetchone():
        raise HTTPException(status_code=404, detail="Pending invite not found")
    await pg.commit()


async def preview_invite(
    pg: AsyncSession, *, raw_token: str
) -> dict[str, Any]:
    if not raw_token or len(raw_token) < 20:
        return {"valid": False, "reason": "invalid_token"}
    th = hash_token(raw_token)
    r = await pg.execute(
        text(
            """
            SELECT wi.id, wi.email, wi.status, wi.expires_at, w.title AS workshop_title,
                   w.id AS workshop_id
            FROM workshop_invites wi
            JOIN workshops w ON w.id = wi.workshop_id
            WHERE wi.token_hash = :th
            """
        ),
        {"th": th},
    )
    row = r.mappings().first()
    if not row:
        return {"valid": False, "reason": "not_found"}
    now = datetime.now(timezone.utc)
    exp = row["expires_at"]
    if isinstance(exp, datetime) and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp and now > exp:
        return {"valid": False, "reason": "expired"}
    if row["status"] != "pending":
        return {"valid": False, "reason": "already_used_or_revoked"}

    return {
        "valid": True,
        "workshop_id": str(row["workshop_id"]),
        "workshop_title": row.get("workshop_title") or "Workshop",
        "email_mask": mask_email(str(row["email"])),
    }


async def redeem_invite(
    pg: AsyncSession,
    *,
    user_id: str,
    user_email: str,
    raw_token: str,
) -> dict[str, Any]:
    if not raw_token or len(raw_token) < 20:
        raise HTTPException(status_code=400, detail="Invalid invite token")

    th = hash_token(raw_token)
    r = await pg.execute(
        text(
            """
            SELECT
                wi.id AS invite_id, wi.email AS invite_email, wi.workshop_id,
                wi.status AS invite_status, wi.expires_at,
                w.content_id, w.start_at, w.end_at, w.seat_cap, w.used_seats,
                w.payment_status, w.access_policy, w.status AS workshop_status, w.title
            FROM workshop_invites wi
            JOIN workshops w ON w.id = wi.workshop_id
            WHERE wi.token_hash = :th
            """
        ),
        {"th": th},
    )
    row = r.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Invite not found")

    now = datetime.now(timezone.utc)
    exp = row["expires_at"]
    if isinstance(exp, datetime) and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp and now > exp:
        raise HTTPException(status_code=410, detail="This invite has expired")

    if row["invite_status"] != "pending":
        raise HTTPException(status_code=409, detail="This invite is no longer valid")

    invite_email = str(row["invite_email"]).strip().lower()
    if user_email.strip().lower() != invite_email:
        raise HTTPException(
            status_code=403,
            detail="Sign in with the same email address this invitation was sent to.",
        )

    workshop_id = str(row["workshop_id"])
    workshop = {
        "id": row["workshop_id"],
        "content_id": row["content_id"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "seat_cap": row["seat_cap"],
        "used_seats": row["used_seats"],
        "payment_status": row["payment_status"],
        "access_policy": row["access_policy"],
        "status": row["workshop_status"],
    }

    await assert_can_grant_seat(pg, workshop, workshop_id=workshop_id)

    vu = compute_entitlement_valid_until(workshop)
    try:
        upd = await pg.execute(
            text(
                """
                UPDATE workshop_invites
                SET status = 'accepted',
                    accepted_user_id = CAST(:uid AS uuid),
                    accepted_at = now(),
                    updated_at = now()
                WHERE token_hash = :th AND status = 'pending'
                RETURNING id
                """
            ),
            {"uid": user_id, "th": th},
        )
        if not upd.fetchone():
            raise HTTPException(
                status_code=409, detail="This invite was already used or revoked"
            )

        await insert_workshop_entitlement(
            pg,
            workshop_id=workshop_id,
            learner_user_id=user_id,
            content_id=str(row["content_id"]),
            valid_until=vu,
        )
        await pg.commit()
    except HTTPException:
        await pg.rollback()
        raise
    except Exception:
        await pg.rollback()
        raise

    log.info(
        "workshop_invite redeemed invite_id=%s workshop_id=%s user_id=%s",
        row["invite_id"],
        workshop_id,
        user_id,
    )
    return {
        "ok": True,
        "workshop_id": workshop_id,
        "workshop_title": row.get("title"),
        "valid_until": vu.isoformat() if vu else None,
    }
