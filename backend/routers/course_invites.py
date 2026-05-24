"""Course admin — workshop cohort email invites (Phase B)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies.authz import CourseAdminOrAbove
from backend.pg import get_pg
from backend.schemas.auth import CurrentUser
from backend.schemas.workshop_invite import WorkshopInviteCreateBody
from backend.routers.workshops import _ensure_workshop_operator, _log_workshop_activity
from backend.services.workshop_invites import (
    create_invite,
    list_invites,
    normalize_invite_email,
    resend_invite,
    revoke_invite,
)
from backend.services.ops_feed import emit_ops_event

log = logging.getLogger("course_invites")

router = APIRouter(prefix="/course", tags=["Course"])


def _missing_workshop_invites_table(exc: ProgrammingError) -> bool:
    msg = str(getattr(exc, "orig", None) or exc)
    return "workshop_invites" in msg


def _migration_required_detail() -> str:
    return (
        "Database is missing workshop_invites (Phase B). From the Backend folder run: "
        "alembic upgrade head — then restart the API."
    )


@router.get("/workshops/{workshop_id}/invites")
async def list_workshop_invites(
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    await _ensure_workshop_operator(pg, current_user, workshop_id)
    try:
        rows = await list_invites(pg, workshop_id)
    except ProgrammingError as exc:
        if _missing_workshop_invites_table(exc):
            log.warning(
                "workshop_invites table missing — run: alembic upgrade head (revision 0013). %s",
                str(exc)[:200],
            )
            return {"count": 0, "invites": []}
        raise
    return {"count": len(rows), "invites": rows}


@router.post("/workshops/{workshop_id}/invites", status_code=201)
async def create_workshop_invite(
    body: WorkshopInviteCreateBody,
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    await _ensure_workshop_operator(pg, current_user, workshop_id)
    email = normalize_invite_email(str(body.email))
    try:
        out = await create_invite(
            pg,
            workshop_id=workshop_id,
            email=email,
            invited_by=str(current_user.id),
        )
    except ProgrammingError as exc:
        if _missing_workshop_invites_table(exc):
            raise HTTPException(status_code=503, detail=_migration_required_detail()) from exc
        raise
    await _log_workshop_activity(
        pg,
        str(current_user.id),
        workshop_id,
        "workshop.invite_created",
        {"email": email, "invite_id": out["invite_id"]},
    )
    await emit_ops_event(
        pg,
        event_key=f"cohort-invite-created:{workshop_id}:{out['invite_id']}",
        event_type="cohort.invite_created",
        severity="warning" if not out.get("email_dispatched") else "info",
        title="Cohort invite created",
        message=(
            f"Invite created for {email}. Email dispatch failed."
            if not out.get("email_dispatched")
            else f"Invite created for {email}."
        ),
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        deep_link=f"/admin/ops/workshop/{workshop_id}?tab=roster",
        metadata={"invite_id": out["invite_id"], "email": email},
        emitter="course_invites.router",
    )
    await pg.commit()
    return out


@router.post("/workshops/{workshop_id}/invites/{invite_id}/resend")
async def resend_workshop_invite(
    workshop_id: str = Path(...),
    invite_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    await _ensure_workshop_operator(pg, current_user, workshop_id)
    try:
        out = await resend_invite(pg, workshop_id=workshop_id, invite_id=invite_id)
    except ProgrammingError as exc:
        if _missing_workshop_invites_table(exc):
            raise HTTPException(status_code=503, detail=_migration_required_detail()) from exc
        raise
    await _log_workshop_activity(
        pg,
        str(current_user.id),
        workshop_id,
        "workshop.invite_resent",
        {"invite_id": invite_id},
    )
    await emit_ops_event(
        pg,
        event_key=f"cohort-invite-resent:{workshop_id}:{invite_id}",
        event_type="cohort.invite_resent",
        severity="warning" if not out.get("email_dispatched") else "info",
        title="Cohort invite resent",
        message=(
            "Invite resent but email dispatch failed."
            if not out.get("email_dispatched")
            else "Cohort invite resent."
        ),
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        deep_link=f"/admin/ops/workshop/{workshop_id}?tab=roster",
        metadata={"invite_id": invite_id},
        emitter="course_invites.router",
    )
    await pg.commit()
    return out


@router.delete("/workshops/{workshop_id}/invites/{invite_id}", status_code=200)
async def revoke_workshop_invite(
    workshop_id: str = Path(...),
    invite_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    await _ensure_workshop_operator(pg, current_user, workshop_id)
    try:
        await revoke_invite(pg, workshop_id=workshop_id, invite_id=invite_id)
    except ProgrammingError as exc:
        if _missing_workshop_invites_table(exc):
            raise HTTPException(status_code=503, detail=_migration_required_detail()) from exc
        raise
    await _log_workshop_activity(
        pg,
        str(current_user.id),
        workshop_id,
        "workshop.invite_revoked",
        {"invite_id": invite_id},
    )
    await emit_ops_event(
        pg,
        event_key=f"cohort-invite-revoked:{workshop_id}:{invite_id}",
        event_type="cohort.invite_revoked",
        severity="warning",
        title="Cohort invite revoked",
        message="Course admin revoked a pending cohort invite.",
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        deep_link=f"/admin/ops/workshop/{workshop_id}?tab=roster",
        metadata={"invite_id": invite_id},
        emitter="course_invites.router",
    )
    await pg.commit()
    return {"ok": True}
