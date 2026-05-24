# sys_admin — workshop (cohort) header and course-admin assignment.
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import ROLE_COURSE_ADMIN, ROLE_SYS_ADMIN
from backend.dependencies.authz import CourseAdminOrAbove, SysAdminOnly
from backend.pg import get_pg
from backend.schemas.auth import CurrentUser
from backend.schemas.workshop import (
    WorkshopAssignAdminRequest,
    WorkshopCreateRequest,
    WorkshopGrantSeatRequest,
    WorkshopPatchRequest,
)
from backend.services.course_admin_role import maybe_demote_course_admin_to_participant
from backend.services.ops_feed import emit_ops_event
from backend.services.workshop_seat_grants import (
    assert_can_grant_seat,
    compute_entitlement_valid_until,
    grant_workshop_seat as insert_workshop_entitlement,
)

log = logging.getLogger("workshops")
router = APIRouter(prefix="/admin/workshops", tags=["Workshop"])


def _patch_fields_digest(patch: dict[str, Any]) -> str:
    canonical = json.dumps(patch, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


async def _ensure_workshop_operator(
    pg: AsyncSession,
    actor: CurrentUser,
    workshop_id: str,
) -> None:
    """sys_admin may operate any workshop; course_admin only assigned cohorts."""
    if actor.role == ROLE_SYS_ADMIN:
        return
    if actor.role != ROLE_COURSE_ADMIN:
        raise HTTPException(status_code=403, detail="Access denied")
    chk = await pg.execute(
        text(
            """
            SELECT 1 FROM workshop_course_admins
            WHERE workshop_id = CAST(:w AS uuid) AND user_id = CAST(:u AS uuid)
            """
        ),
        {"w": workshop_id, "u": str(actor.id)},
    )
    if not chk.fetchone():
        raise HTTPException(status_code=403, detail="Not assigned to this workshop")


def _norm_code(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    s = v.strip()
    return s if s else None


async def _log_workshop_activity(
    pg: AsyncSession,
    actor_user_id: str,
    workshop_id: str,
    action: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    await pg.execute(
        text(
            """
            INSERT INTO content_activity_logs (actor_user_id, entity_type, entity_id, action, metadata)
            VALUES (:actor_user_id, 'workshop', :entity_id, :action, CAST(:metadata AS jsonb))
            """
        ),
        {
            "actor_user_id": actor_user_id,
            "entity_id": workshop_id,
            "action": action,
            "metadata": json.dumps(metadata or {}),
        },
    )


def _mapping_to_workshop(m: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": m["id"],
        "internal_code": m.get("internal_code"),
        "title": m["title"],
        "description": m.get("description"),
        "content_id": m["content_id"],
        "content_title": m.get("content_title"),
        "start_at": m.get("start_at"),
        "end_at": m.get("end_at"),
        "mode": m["mode"],
        "seat_cap": m["seat_cap"],
        "used_seats": m["used_seats"],
        "payment_status": m["payment_status"],
        "payment_id": m.get("payment_id"),
        "payer_ref": m.get("payer_ref"),
        "access_policy": m.get("access_policy") or "requires_payment",
        "status": m["status"],
        "created_by": m.get("created_by"),
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
    }


@router.get("")
async def list_workshops(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
    q: str | None = Query(default=None, max_length=200),
    segment: str = Query(
        default="all",
        description="all | draft | live | closed (archived manifests) | payment_pending (payment_status=pending)",
    ),
):
    allowed = {"all", "draft", "live", "closed", "payment_pending"}
    if segment not in allowed:
        raise HTTPException(
            status_code=400,
            detail="segment must be one of: all, draft, live, closed, payment_pending",
        )

    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    if q and q.strip():
        where_clauses.append(
            "(w.title ILIKE :like OR w.internal_code ILIKE :like OR c.title ILIKE :like)"
        )
        params["like"] = f"%{q.strip()}%"
    if segment == "draft":
        where_clauses.append("w.status = 'draft'")
    elif segment == "live":
        where_clauses.append("w.status = 'active'")
    elif segment == "closed":
        where_clauses.append("w.status = 'archived'")
    elif segment == "payment_pending":
        where_clauses.append("w.payment_status = 'pending'")

    where_sql = ""
    if where_clauses:
        where_sql = " AND " + " AND ".join(where_clauses)

    result = await pg.execute(
        text(
            f"""
            SELECT
                w.id, w.internal_code, w.title, w.description, w.content_id, c.title AS content_title,
                w.start_at, w.end_at, w.mode, w.seat_cap, w.used_seats,
                w.payment_status, w.payment_id, w.payer_ref, w.access_policy, w.status,
                w.created_by, w.created_at, w.updated_at,
                (SELECT u.email
                 FROM workshop_course_admins wca
                 JOIN users u ON u.id = wca.user_id
                 WHERE wca.workshop_id = w.id AND wca.is_lead = true
                 LIMIT 1) AS lead_admin_email
            FROM workshops w
            JOIN content_items c ON c.id = w.content_id
            WHERE 1=1
            {where_sql}
            ORDER BY w.created_at DESC
            """
        ),
        params,
    )
    rows = result.mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        m = _mapping_to_workshop(dict(r))
        m["lead_admin_email"] = r.get("lead_admin_email")
        out.append(m)
    return {"workshops": out}


@router.get("/{workshop_id}/activity")
async def workshop_activity(
    workshop_id: str = Path(...),
    limit: int = Query(default=200, ge=1, le=500),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    ex = await pg.execute(
        text("SELECT id FROM workshops WHERE id = :wid"),
        {"wid": workshop_id},
    )
    if not ex.fetchone():
        raise HTTPException(status_code=404, detail="Workshop not found")

    result = await pg.execute(
        text(
            """
            SELECT
                l.id,
                l.actor_user_id,
                u.email AS actor_email,
                l.action,
                l.metadata,
                l.created_at
            FROM content_activity_logs l
            LEFT JOIN users u ON u.id = l.actor_user_id
            WHERE l.entity_type = 'workshop' AND l.entity_id = :wid
            ORDER BY l.created_at DESC
            LIMIT :lim
            """
        ),
        {"wid": workshop_id, "lim": limit},
    )
    rows = result.mappings().all()
    return {
        "rows": [
            {
                "id": r["id"],
                "actor_user_id": str(r["actor_user_id"])
                if r.get("actor_user_id")
                else None,
                "actor_email": r.get("actor_email"),
                "action": r["action"],
                "metadata": r.get("metadata") or {},
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


@router.get("/{workshop_id}")
async def get_workshop(
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    wr = await pg.execute(
        text(
            """
            SELECT
                w.id, w.internal_code, w.title, w.description, w.content_id, c.title AS content_title,
                c.metadata->>'lab_type' AS content_lab_type,
                w.start_at, w.end_at, w.mode, w.seat_cap, w.used_seats,
                w.payment_status, w.payment_id, w.payer_ref, w.access_policy, w.status,
                w.created_by, w.created_at, w.updated_at,
                p.id AS pay_id, p.amount AS pay_amount, p.currency AS pay_currency,
                p.status AS pay_status, p.gateway_order_id AS pay_gateway_order_id,
                p.gateway_payment_id AS pay_gateway_payment_id
            FROM workshops w
            JOIN content_items c ON c.id = w.content_id
            LEFT JOIN payments p ON p.id = w.payment_id
            WHERE w.id = :wid
            """
        ),
        {"wid": workshop_id},
    )
    row = wr.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Workshop not found")

    ar = await pg.execute(
        text(
            """
            SELECT wca.user_id, wca.is_lead, u.email, u.name
            FROM workshop_course_admins wca
            JOIN users u ON u.id = wca.user_id
            WHERE wca.workshop_id = :wid
            ORDER BY wca.is_lead DESC, u.email ASC
            """
        ),
        {"wid": workshop_id},
    )
    admins = [
        {
            "user_id": a.user_id,
            "email": a.email,
            "name": a.name,
            "is_lead": a.is_lead,
        }
        for a in ar.mappings().all()
    ]
    rd = dict(row)
    pay = None
    if rd.get("pay_id") is not None:
        pay = {
            "payment_id": str(rd["pay_id"]),
            "amount": rd["pay_amount"],
            "currency": rd["pay_currency"],
            "gateway_status": rd["pay_status"],
            "gateway_order_id": rd["pay_gateway_order_id"],
            "gateway_payment_id": rd.get("pay_gateway_payment_id"),
        }
    for k in (
        "pay_id",
        "pay_amount",
        "pay_currency",
        "pay_status",
        "pay_gateway_order_id",
        "pay_gateway_payment_id",
        "content_lab_type",
    ):
        rd.pop(k, None)
    base = _mapping_to_workshop(rd)
    lab_type = row.get("content_lab_type")
    return {**base, "content_lab_type": lab_type, "payment": pay, "admins": admins}


@router.post("", status_code=201)
async def create_workshop(
    body: WorkshopCreateRequest,
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    cr = await pg.execute(
        text("SELECT id FROM content_items WHERE id = :id AND type = 'lab'"),
        {"id": str(body.content_id)},
    )
    if not cr.fetchone():
        raise HTTPException(status_code=404, detail="Lab content not found")

    if body.payment_id is not None:
        pr = await pg.execute(
            text("SELECT id FROM payments WHERE id = :id"),
            {"id": str(body.payment_id)},
        )
        if not pr.fetchone():
            raise HTTPException(status_code=404, detail="Payment not found")

    code = _norm_code(body.internal_code)
    if code is not None:
        dup = await pg.execute(
            text("SELECT 1 FROM workshops WHERE internal_code = :c"),
            {"c": code},
        )
        if dup.fetchone():
            raise HTTPException(
                status_code=409, detail="internal_code already in use"
            )

    wid = str(uuid4())
    await pg.execute(
        text(
            """
            INSERT INTO workshops (
                id, internal_code, title, description, content_id, start_at, end_at, mode,
                seat_cap, used_seats, payment_status, payment_id, payer_ref,
                access_policy, status, created_by
            )
            VALUES (
                CAST(:id AS uuid), :internal_code, :title, :description, CAST(:content_id AS uuid),
                :start_at, :end_at, :mode, :seat_cap, 0,
                :payment_status, CAST(:payment_id AS uuid), :payer_ref,
                :access_policy, :status, CAST(:created_by AS uuid)
            )
            """
        ),
        {
            "id": wid,
            "internal_code": code,
            "title": body.title,
            "description": body.description,
            "content_id": str(body.content_id),
            "start_at": body.start_at,
            "end_at": body.end_at,
            "mode": body.mode,
            "seat_cap": body.seat_cap,
            "payment_status": body.payment_status,
            "payment_id": str(body.payment_id) if body.payment_id else None,
            "payer_ref": body.payer_ref,
            "access_policy": body.access_policy,
            "status": body.status,
            "created_by": str(admin.id),
        },
    )

    await _log_workshop_activity(
        pg, str(admin.id), wid, "workshop.created", {"title": body.title}
    )
    await emit_ops_event(
        pg,
        event_key=f"workshop-created:{wid}",
        event_type="workshop.created",
        severity="info",
        title="Workshop (cohort) created",
        message=f"Sys admin created cohort “{body.title}”.",
        actor_user_id=str(admin.id),
        actor_email=getattr(admin, "email", None),
        subject_type="workshop",
        subject_id=wid,
        workshop_id=wid,
        deep_link=f"/admin/ops/workshop/{wid}?tab=overview",
        metadata={
            "title": body.title,
            "content_id": str(body.content_id),
            "seat_cap": body.seat_cap,
            "payment_status": body.payment_status,
            "internal_code": code,
        },
        emitter="workshops.router",
    )
    await pg.commit()

    log.info("Workshop created: id=%s by=%s", wid, admin.id)

    wr = await pg.execute(
        text(
            """
            SELECT
                w.id, w.internal_code, w.title, w.description, w.content_id, c.title AS content_title,
                w.start_at, w.end_at, w.mode, w.seat_cap, w.used_seats,
                w.payment_status, w.payment_id, w.payer_ref, w.access_policy, w.status,
                w.created_by, w.created_at, w.updated_at
            FROM workshops w
            JOIN content_items c ON c.id = w.content_id
            WHERE w.id = :wid
            """
        ),
        {"wid": wid},
    )
    row = wr.mappings().first()
    return {"workshop": _mapping_to_workshop(dict(row))}


@router.patch("/{workshop_id}")
async def patch_workshop(
    body: WorkshopPatchRequest,
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    wr = await pg.execute(
        text("SELECT id FROM workshops WHERE id = :wid"),
        {"wid": workshop_id},
    )
    if not wr.fetchone():
        raise HTTPException(status_code=404, detail="Workshop not found")

    patch = body.model_dump(exclude_unset=True)
    updates: list[str] = []
    params: dict[str, Any] = {"wid": workshop_id}

    if "title" in patch:
        updates.append("title = :title")
        params["title"] = patch["title"]
    if "description" in patch:
        updates.append("description = :description")
        params["description"] = patch["description"]
    if "internal_code" in patch:
        code = _norm_code(patch["internal_code"])
        params["internal_code"] = code
        updates.append("internal_code = :internal_code")
        if code is not None:
            dup = await pg.execute(
                text(
                    "SELECT 1 FROM workshops WHERE internal_code = :c AND id <> CAST(:wid AS uuid)"
                ),
                {"c": code, "wid": workshop_id},
            )
            if dup.fetchone():
                raise HTTPException(
                    status_code=409, detail="internal_code already in use"
                )
    if "start_at" in patch:
        updates.append("start_at = :start_at")
        params["start_at"] = patch["start_at"]
    if "end_at" in patch:
        updates.append("end_at = :end_at")
        params["end_at"] = patch["end_at"]
    if "mode" in patch:
        updates.append("mode = :mode")
        params["mode"] = patch["mode"]
    if "seat_cap" in patch:
        updates.append("seat_cap = :seat_cap")
        params["seat_cap"] = patch["seat_cap"]
    if "payment_status" in patch:
        updates.append("payment_status = :payment_status")
        params["payment_status"] = patch["payment_status"]
    if "payment_id" in patch:
        pid = patch["payment_id"]
        if pid is None:
            updates.append("payment_id = NULL")
        else:
            pr = await pg.execute(
                text("SELECT id FROM payments WHERE id = :id"),
                {"id": str(pid)},
            )
            if not pr.fetchone():
                raise HTTPException(status_code=404, detail="Payment not found")
            updates.append("payment_id = CAST(:payment_id AS uuid)")
            params["payment_id"] = str(pid)
    if "payer_ref" in patch:
        updates.append("payer_ref = :payer_ref")
        params["payer_ref"] = patch["payer_ref"]
    if "status" in patch:
        updates.append("status = :status")
        params["status"] = patch["status"]
    if "access_policy" in patch:
        updates.append("access_policy = :access_policy")
        params["access_policy"] = patch["access_policy"]

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    updates.append("updated_at = now()")
    sql = f"UPDATE workshops SET {', '.join(updates)} WHERE id = CAST(:wid AS uuid)"
    await pg.execute(text(sql), params)
    await pg.commit()

    await _log_workshop_activity(
        pg, str(admin.id), workshop_id, "workshop.updated", {}
    )
    digest = _patch_fields_digest(patch)
    keys = ", ".join(sorted(patch.keys()))
    sev: str = "info"
    if patch.get("status") == "archived":
        sev = "warning"
    ps = patch.get("payment_status")
    if isinstance(ps, str) and ps.lower() in {"failed", "refunded", "chargeback"}:
        sev = "warning"
    tab = "billing" if (
        {"payment_status", "payment_id", "payer_ref", "seat_cap"} & set(patch.keys())
    ) else "overview"
    await emit_ops_event(
        pg,
        event_key=f"workshop-patch:{workshop_id}:{digest}",
        event_type="workshop.updated",
        severity=sev,
        title="Workshop configuration updated",
        message=f"Fields changed: {keys}." if keys else "Workshop record updated.",
        actor_user_id=str(admin.id),
        actor_email=getattr(admin, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        deep_link=f"/admin/ops/workshop/{workshop_id}?tab={tab}",
        metadata=patch,
        emitter="workshops.router",
    )
    await pg.commit()

    wr2 = await pg.execute(
        text(
            """
            SELECT
                w.id, w.internal_code, w.title, w.description, w.content_id, c.title AS content_title,
                w.start_at, w.end_at, w.mode, w.seat_cap, w.used_seats,
                w.payment_status, w.payment_id, w.payer_ref, w.access_policy, w.status,
                w.created_by, w.created_at, w.updated_at
            FROM workshops w
            JOIN content_items c ON c.id = w.content_id
            WHERE w.id = :wid
            """
        ),
        {"wid": workshop_id},
    )
    row = wr2.mappings().first()
    return {"workshop": _mapping_to_workshop(dict(row))}


@router.post("/{workshop_id}/grant-seat")
async def grant_workshop_seat_endpoint(
    body: WorkshopGrantSeatRequest,
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    actor: CurrentUser = Depends(CourseAdminOrAbove),
):
    """
    Grant one learner a cohort seat (workshop-scoped entitlement).
    course_admin: must be assigned on this workshop; sys_admin: unrestricted.
    """
    await _ensure_workshop_operator(pg, actor, workshop_id)

    wr = await pg.execute(
        text(
            """
            SELECT
                w.id, w.content_id, w.start_at, w.end_at, w.seat_cap, w.used_seats,
                w.payment_status, w.access_policy, w.status
            FROM workshops w
            WHERE w.id = CAST(:wid AS uuid)
            """
        ),
        {"wid": workshop_id},
    )
    workshop = wr.mappings().first()
    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    ur = await pg.execute(
        text("SELECT id FROM users WHERE id = CAST(:id AS uuid)"),
        {"id": str(body.user_id)},
    )
    if not ur.fetchone():
        raise HTTPException(status_code=404, detail="User not found")

    await assert_can_grant_seat(pg, workshop, workshop_id=workshop_id)
    vu = compute_entitlement_valid_until(workshop)

    await insert_workshop_entitlement(
        pg,
        workshop_id=workshop_id,
        learner_user_id=str(body.user_id),
        content_id=str(workshop["content_id"]),
        valid_until=vu,
    )
    await pg.commit()

    await _log_workshop_activity(
        pg,
        str(actor.id),
        workshop_id,
        "workshop.seat_granted",
        {"learner_user_id": str(body.user_id)},
    )
    grant_token = str(uuid4())
    await emit_ops_event(
        pg,
        event_key=f"cohort-seat-granted:{workshop_id}:{body.user_id}:{grant_token}",
        event_type="cohort.seat_granted",
        severity="info",
        title="Cohort seat granted",
        message="A learner seat was granted by course administration.",
        actor_user_id=str(actor.id),
        actor_email=getattr(actor, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        target_user_id=str(body.user_id),
        deep_link=f"/admin/ops/workshop/{workshop_id}?tab=roster",
        metadata={"learner_user_id": str(body.user_id), "grant_token": grant_token},
        emitter="workshops.router",
    )
    await pg.commit()

    return {"ok": True, "user_id": str(body.user_id), "valid_until": vu.isoformat() if vu else None}


@router.post("/{workshop_id}/admins/{user_id}", status_code=201)
async def assign_workshop_admin(
    body: WorkshopAssignAdminRequest,
    workshop_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    wr = await pg.execute(
        text("SELECT id FROM workshops WHERE id = :wid"),
        {"wid": workshop_id},
    )
    if not wr.fetchone():
        raise HTTPException(status_code=404, detail="Workshop not found")

    ur = await pg.execute(
        text("SELECT id, role, is_active FROM users WHERE id = :id"),
        {"id": user_id},
    )
    urow = ur.mappings().first()
    if not urow:
        raise HTTPException(status_code=404, detail="User not found")
    if not urow["is_active"]:
        raise HTTPException(status_code=400, detail="User is inactive")
    if urow["role"] == ROLE_SYS_ADMIN:
        raise HTTPException(
            status_code=400,
            detail="Cannot assign sys_admin as a workshop operator.",
        )
    if urow["role"] != ROLE_COURSE_ADMIN:
        raise HTTPException(
            status_code=400,
            detail="User must have role course_admin to be assigned to a workshop.",
        )

    await pg.execute(
        text(
            """
            INSERT INTO workshop_course_admins (id, workshop_id, user_id, is_lead)
            VALUES (gen_random_uuid(), CAST(:wid AS uuid), CAST(:uid AS uuid), :is_lead)
            ON CONFLICT (workshop_id, user_id) DO UPDATE SET is_lead = EXCLUDED.is_lead
            """
        ),
        {"wid": workshop_id, "uid": user_id, "is_lead": body.is_lead},
    )
    await pg.commit()

    await _log_workshop_activity(
        pg,
        str(admin.id),
        workshop_id,
        "workshop.admin_assigned",
        {"user_id": user_id, "is_lead": body.is_lead},
    )
    await emit_ops_event(
        pg,
        event_key=f"workshop-operator-assigned:{workshop_id}:{user_id}",
        event_type="workshop.operator_assigned",
        severity="info",
        title="Workshop operator assigned",
        message="Sys admin assigned a course admin to this cohort.",
        actor_user_id=str(admin.id),
        actor_email=getattr(admin, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        target_user_id=user_id,
        deep_link=f"/admin/ops/workshop/{workshop_id}?tab=assignments",
        metadata={"is_lead": body.is_lead},
        emitter="workshops.router",
    )
    await pg.commit()

    log.info(
        "Workshop admin assigned: workshop_id=%s user_id=%s by=%s",
        workshop_id, user_id, admin.id,
    )
    return {"workshop_id": workshop_id, "user_id": user_id, "is_lead": body.is_lead}


@router.delete("/{workshop_id}/admins/{user_id}", status_code=200)
async def remove_workshop_admin(
    workshop_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    result = await pg.execute(
        text(
            """
            DELETE FROM workshop_course_admins
            WHERE workshop_id = CAST(:wid AS uuid) AND user_id = CAST(:uid AS uuid)
            RETURNING user_id
            """
        ),
        {"wid": workshop_id, "uid": user_id},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Assignment not found")

    await maybe_demote_course_admin_to_participant(
        pg, user_id, demoted_by=str(admin.id)
    )

    await pg.commit()

    await _log_workshop_activity(
        pg, str(admin.id), workshop_id, "workshop.admin_removed", {"user_id": user_id}
    )
    await emit_ops_event(
        pg,
        event_key=f"workshop-operator-removed:{workshop_id}:{user_id}",
        event_type="workshop.operator_removed",
        severity="info",
        title="Workshop operator removed",
        message="Sys admin removed a course admin from this cohort.",
        actor_user_id=str(admin.id),
        actor_email=getattr(admin, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        target_user_id=user_id,
        deep_link=f"/admin/ops/workshop/{workshop_id}?tab=assignments",
        metadata={},
        emitter="workshops.router",
    )
    await pg.commit()

    log.info(
        "Workshop admin removed: workshop_id=%s user_id=%s by=%s",
        workshop_id, user_id, admin.id,
    )
    return {"workshop_id": workshop_id, "user_id": user_id}
