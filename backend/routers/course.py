# backend/routers/course.py
"""
course_admin scoped endpoints.

Data mapping (avoid mixing two “home lists” in the product):
- **Cohorts you operate (canonical operator home):** rows in `workshop_course_admins`
  joined to `workshops`. Exposed as `GET /course/my-operator-cohorts` (and the older
  `GET /course/my-workshops` alias — same query).
- **Per-course managed lab (legacy path):** `course_admin_assignments` + `course_participants`
  + deploy/participant endpoints keyed by `content_id`. Used only by that flow (e.g.
  `/course/{content_id}/deploy`); it is **not** a second cohort and must not be merged
  into the operator cohort list without an explicit product decision + migration.

Endpoints:
  GET    /course/my-courses                        list assigned courses (legacy managed-lab path)
  GET    /course/my-workshops                      operator workshops (alias; same as my-operator-cohorts)
  GET    /course/my-operator-cohorts               operator cohorts (canonical list for course-admin home)
  GET    /course/workshops/{id}                    workshop detail (assigned operators only)
  POST   /course/{content_id}/deploy               deploy with guardrail enforcement
  POST   /course/{content_id}/participants/{uid}   enroll participant
  DELETE /course/{content_id}/participants/{uid}   unenroll participant
  GET    /course/{content_id}/participants         list enrolled participants
  GET    /course/{content_id}/deployments          managed lab deployments (owner + members)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.pg import get_pg
from backend.dependencies.authz import CourseAdminOrAbove
from backend.schemas.auth import CurrentUser
from backend.schemas.labs import LabDeployRequest
from backend.config import (
    get_settings,
    GUARDRAIL_DEFAULT_MAX_CONCURRENT,
    GUARDRAIL_DEFAULT_MAX_DURATION_HOURS,
)
from backend.limiter import limiter
from backend.routers.workshops import _ensure_workshop_operator, _mapping_to_workshop
from backend.services.ops_feed import emit_ops_event

log = logging.getLogger("course")
router = APIRouter(prefix="/course", tags=["Course"])
settings = get_settings()

# Serialize course-admin deploy attempts per (owner, lab) to avoid duplicate rows on double-submit.
_COURSE_DEPLOY_LOCK_NS = 942101


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _verify_course_assignment(
    pg: AsyncSession,
    course_admin_id: str,
    content_id: str,
) -> None:
    """Raises 403 if the course_admin is not assigned to this course."""
    result = await pg.execute(
        text("""
            SELECT 1 FROM course_admin_assignments
            WHERE user_id = :user_id AND content_id = :content_id
        """),
        {"user_id": course_admin_id, "content_id": content_id},
    )
    if not result.fetchone():
        raise HTTPException(
            status_code=403,
            detail="You are not assigned as course_admin for this course.",
        )


async def _get_guardrails(
    pg: AsyncSession,
    course_admin_id: str,
    content_id: str,
) -> tuple[int, int]:
    """
    Returns (max_concurrent_deployments, max_duration_hours).
    Falls back to config defaults if no guardrail row exists.
    """
    result = await pg.execute(
        text("""
            SELECT max_concurrent_deployments, max_duration_hours
            FROM course_guardrails
            WHERE course_admin_id = :course_admin_id
              AND content_id = :content_id
        """),
        {"course_admin_id": course_admin_id, "content_id": content_id},
    )
    row = result.fetchone()
    if row:
        return row.max_concurrent_deployments, row.max_duration_hours
    return GUARDRAIL_DEFAULT_MAX_CONCURRENT, GUARDRAIL_DEFAULT_MAX_DURATION_HOURS


def _runtime_state_from_deployment_status(status: str | None) -> str:
    if not status:
        return "not_requested"
    s = (status or "").lower()
    if s == "queued":
        return "queued"
    if s == "provisioning":
        return "provisioning"
    if s == "running":
        return "ready"
    if s == "failed":
        return "failed"
    if s in {"terminating", "expired", "cleanup_failed"}:
        return "ended"
    return "not_requested"


# ── Endpoints ─────────────────────────────────────────────────────────────────


async def _list_operator_workshops_for_user(
    pg: AsyncSession,
    user_id: str,
) -> list[dict[str, Any]]:
    """
    Workshops where `user_id` appears in `workshop_course_admins`.
    Single source of truth for “cohorts this operator may open” in course-admin UI.
    """
    result = await pg.execute(
        text(
            """
            SELECT
                w.id, w.internal_code, w.title, w.description, w.content_id, c.title AS content_title,
                w.start_at, w.end_at, w.mode, w.seat_cap, w.used_seats,
                w.payment_status, w.payment_id, w.payer_ref, w.access_policy, w.status,
                w.created_by, w.created_at, w.updated_at,
                wca.is_lead AS operator_is_lead,
                (SELECT u.email
                 FROM workshop_course_admins wca2
                 JOIN users u ON u.id = wca2.user_id
                 WHERE wca2.workshop_id = w.id AND wca2.is_lead = true
                 LIMIT 1) AS lead_admin_email
            FROM workshops w
            JOIN content_items c ON c.id = w.content_id
            JOIN workshop_course_admins wca
              ON wca.workshop_id = w.id AND wca.user_id = CAST(:uid AS uuid)
            ORDER BY w.created_at DESC
            """
        ),
        {"uid": user_id},
    )
    rows = result.mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        m = _mapping_to_workshop(dict(r))
        m["lead_admin_email"] = r.get("lead_admin_email")
        m["operator_is_lead"] = bool(r.get("operator_is_lead"))
        out.append(m)
    return out


@router.get("/my-courses")
async def my_courses(
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """List all courses assigned to the current course_admin."""
    result = await pg.execute(
        text("""
            SELECT ci.id, ci.title, ci.description, ci.difficulty,
                   ci.duration_minutes, ci.is_active, caa.assigned_at,
                   g.max_concurrent_deployments, g.max_duration_hours
            FROM course_admin_assignments caa
            JOIN content_items ci ON caa.content_id = ci.id
            LEFT JOIN course_guardrails g
                ON g.course_admin_id = caa.user_id AND g.content_id = caa.content_id
            WHERE caa.user_id = :user_id
            ORDER BY caa.assigned_at DESC
        """),
        {"user_id": str(current_user.id)},
    )
    rows = result.fetchall()
    return {
        "count": len(rows),
        "courses": [
            {
                "content_id":                 r.id,
                "title":                      r.title,
                "description":                r.description,
                "difficulty":                 r.difficulty,
                "duration_minutes":           r.duration_minutes,
                "is_active":                  r.is_active,
                "assigned_at":                r.assigned_at,
                "max_concurrent_deployments": r.max_concurrent_deployments
                                              or GUARDRAIL_DEFAULT_MAX_CONCURRENT,
                "max_duration_hours":         r.max_duration_hours
                                              or GUARDRAIL_DEFAULT_MAX_DURATION_HOURS,
            }
            for r in rows
        ],
    }


@router.get("/my-operator-cohorts")
async def my_operator_cohorts(
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """
    Cohorts (workshops) this user may operate: exactly `workshop_course_admins` ∩ `workshops`.
    Canonical list for the course-admin home — one DB join path, no merge with
    `course_admin_assignments`.
    """
    out = await _list_operator_workshops_for_user(pg, str(current_user.id))
    return {"count": len(out), "cohorts": out}


@router.get("/my-workshops")
async def my_workshops(
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """Backward-compatible alias for :func:`my_operator_cohorts` (same payload key `workshops`)."""
    out = await _list_operator_workshops_for_user(pg, str(current_user.id))
    return {"count": len(out), "workshops": out}


@router.get("/workshops/{workshop_id}")
async def course_get_workshop(
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    await _ensure_workshop_operator(pg, current_user, workshop_id)

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
            WHERE w.id = CAST(:wid AS uuid)
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
            WHERE wca.workshop_id = CAST(:wid AS uuid)
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


@router.get("/workshops/{workshop_id}/roster-runtime")
async def course_workshop_roster_runtime(
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """
    Cohort roster + learner runtime for course-admin detail page.

    This endpoint normalizes invite/seat/runtime sources:
    - invites: workshop_invites
    - active access seats: entitlements(workshop_id, status='active')
    - runtime: latest workshop-scoped lab_deployments row for each learner
    """
    await _ensure_workshop_operator(pg, current_user, workshop_id)

    wr = await pg.execute(
        text(
            """
            SELECT id, content_id, seat_cap, used_seats
            FROM workshops
            WHERE id = CAST(:wid AS uuid)
            """
        ),
        {"wid": workshop_id},
    )
    workshop = wr.mappings().first()
    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")

    invite_result = await pg.execute(
        text(
            """
            SELECT
                wi.id,
                wi.email,
                wi.status,
                wi.accepted_user_id,
                wi.accepted_at,
                wi.expires_at,
                wi.email_sent_at,
                wi.last_email_error,
                wi.created_at,
                wi.updated_at
            FROM workshop_invites wi
            WHERE wi.workshop_id = CAST(:wid AS uuid)
            ORDER BY wi.created_at DESC
            """
        ),
        {"wid": workshop_id},
    )
    invite_rows = invite_result.mappings().all()
    invite_by_user: dict[str, dict[str, Any]] = {}
    invite_by_email: dict[str, dict[str, Any]] = {}
    pending_invites: list[dict[str, Any]] = []
    for inv in invite_rows:
        inv_data = dict(inv)
        accepted_uid = inv_data.get("accepted_user_id")
        if accepted_uid:
            invite_by_user[str(accepted_uid)] = inv_data
        invite_by_email[str(inv_data.get("email", "")).strip().lower()] = inv_data
        if (inv_data.get("status") or "").lower() == "pending":
            pending_invites.append(inv_data)

    seat_result = await pg.execute(
        text(
            """
            SELECT
                e.user_id,
                e.created_at AS entitlement_created_at,
                e.valid_until,
                u.email,
                u.name
            FROM entitlements e
            JOIN users u ON u.id = e.user_id
            WHERE e.workshop_id = CAST(:wid AS uuid)
              AND e.status = 'active'
            ORDER BY e.created_at DESC
            """
        ),
        {"wid": workshop_id},
    )
    seat_rows = seat_result.mappings().all()

    runtime_result = await pg.execute(
        text(
            """
            SELECT DISTINCT ON (dm.user_id)
                dm.user_id,
                ld.id AS deployment_id,
                ld.status AS deployment_status,
                ld.error_message,
                ld.updated_at AS deployment_updated_at,
                ld.expires_at
            FROM deployment_members dm
            JOIN lab_deployments ld ON ld.id = dm.deployment_id
            WHERE ld.workshop_id = CAST(:wid AS uuid)
            ORDER BY dm.user_id, ld.updated_at DESC
            """
        ),
        {"wid": workshop_id},
    )
    runtime_rows = runtime_result.mappings().all()
    runtime_by_user = {str(r["user_id"]): dict(r) for r in runtime_rows}

    roster: list[dict[str, Any]] = []
    for seat in seat_rows:
        uid = str(seat["user_id"])
        email = str(seat["email"])
        invite = invite_by_user.get(uid) or invite_by_email.get(email.lower())
        runtime = runtime_by_user.get(uid)
        runtime_status = _runtime_state_from_deployment_status(
            runtime.get("deployment_status") if runtime else None
        )
        roster.append(
            {
                "learner_key": f"user:{uid}",
                "user_id": uid,
                "email": email,
                "name": seat.get("name"),
                "onboarding_method": "invitation" if invite else "admin_enrollment",
                "access_status": "active",
                "seat_consuming": True,
                "invite": {
                    "id": invite.get("id"),
                    "status": invite.get("status"),
                    "created_at": invite.get("created_at"),
                    "accepted_at": invite.get("accepted_at"),
                    "expires_at": invite.get("expires_at"),
                    "email_sent_at": invite.get("email_sent_at"),
                    "last_email_error": invite.get("last_email_error"),
                }
                if invite
                else None,
                "entitlement": {
                    "created_at": seat.get("entitlement_created_at"),
                    "valid_until": seat.get("valid_until"),
                },
                "runtime": {
                    "state": runtime_status,
                    "last_updated_at": runtime.get("deployment_updated_at") if runtime else None,
                    "failure_reason": runtime.get("error_message") if runtime else None,
                    "deployment_id": runtime.get("deployment_id") if runtime else None,
                    "deployment_status": runtime.get("deployment_status") if runtime else None,
                    "expires_at": runtime.get("expires_at") if runtime else None,
                },
            }
        )

    for inv in pending_invites:
        row = {
            "learner_key": f"invite:{inv['id']}",
            "user_id": None,
            "email": inv.get("email"),
            "name": None,
            "onboarding_method": "invitation",
            "access_status": "not_activated",
            "seat_consuming": True,
            "invite": {
                "id": inv.get("id"),
                "status": inv.get("status"),
                "created_at": inv.get("created_at"),
                "accepted_at": inv.get("accepted_at"),
                "expires_at": inv.get("expires_at"),
                "email_sent_at": inv.get("email_sent_at"),
                "last_email_error": inv.get("last_email_error"),
            },
            "entitlement": None,
            "runtime": {
                "state": "not_requested",
                "last_updated_at": None,
                "failure_reason": None,
                "deployment_id": None,
                "deployment_status": None,
                "expires_at": None,
            },
        }
        roster.append(row)

    runtime_counts = {
        "ready": sum(1 for r in roster if r["runtime"]["state"] == "ready"),
        "in_progress": sum(
            1
            for r in roster
            if r["runtime"]["state"] in {"queued", "provisioning"}
        ),
        "failed": sum(1 for r in roster if r["runtime"]["state"] == "failed"),
    }

    return {
        "count": len(roster),
        "seat_cap": int(workshop.get("seat_cap") or 0),
        "used_seats": int(workshop.get("used_seats") or 0),
        "runtime_counts": runtime_counts,
        "rows": roster,
    }


@router.get("/workshops/{workshop_id}/runs")
async def course_workshop_runs(
    workshop_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """Cohort delivery windows (lab deployments) for this workshop, with member emails."""
    await _ensure_workshop_operator(pg, current_user, workshop_id)

    dep_rows = (
        await pg.execute(
            text(
                """
                SELECT id, status, lab_type, error_message, created_at, updated_at, expires_at
                FROM lab_deployments
                WHERE workshop_id = CAST(:wid AS uuid)
                ORDER BY created_at DESC
                """
            ),
            {"wid": workshop_id},
        )
    ).mappings().all()
    dep_ids = [str(r["id"]) for r in dep_rows]
    members_by_dep: dict[str, list[dict[str, str]]] = {d: [] for d in dep_ids}
    if dep_ids:
        mem_rows = (
            await pg.execute(
                text(
                    """
                    SELECT dm.deployment_id, dm.user_id, u.email
                    FROM deployment_members dm
                    JOIN users u ON u.id = dm.user_id
                    WHERE dm.deployment_id = ANY(CAST(:ids AS uuid[]))
                    ORDER BY dm.added_at ASC
                    """
                ),
                {"ids": dep_ids},
            )
        ).mappings().all()
        for m in mem_rows:
            did = str(m["deployment_id"])
            members_by_dep.setdefault(did, []).append(
                {"user_id": str(m["user_id"]), "email": str(m["email"])}
            )

    out: list[dict[str, Any]] = []
    for r in dep_rows:
        did = str(r["id"])
        out.append(
            {
                "deployment_id": did,
                "status": r["status"],
                "lab_type": r["lab_type"],
                "error_message": r.get("error_message"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
                "expires_at": r.get("expires_at"),
                "members": members_by_dep.get(did, []),
            }
        )
    return {"count": len(out), "runs": out}


@router.post("/workshops/{workshop_id}/request-run", status_code=201)
async def course_workshop_request_run(
    workshop_id: str = Path(...),
    body: dict[str, Any] = Body(default_factory=dict),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """
    Queue one cohort delivery window (lab deployment) for learners with active seats
    in this workshop. This keeps course-admin work in one cohort detail workspace.
    """
    await _ensure_workshop_operator(pg, current_user, workshop_id)

    duration_hours = int(body.get("duration_hours") or 4)
    if duration_hours < 1 or duration_hours > 72:
        raise HTTPException(status_code=400, detail="duration_hours must be between 1 and 72")

    wr = await pg.execute(
        text(
            """
            SELECT w.id, w.content_id, w.status, w.seat_cap, w.used_seats, c.metadata
            FROM workshops w
            JOIN content_items c ON c.id = w.content_id
            WHERE w.id = CAST(:wid AS uuid)
            """
        ),
        {"wid": workshop_id},
    )
    workshop = wr.mappings().first()
    if not workshop:
        raise HTTPException(status_code=404, detail="Workshop not found")
    if workshop["status"] == "archived":
        raise HTTPException(status_code=400, detail="Archived cohorts cannot queue a delivery run.")

    in_flight = (
        await pg.execute(
            text(
                """
                SELECT COUNT(*) FROM lab_deployments
                WHERE workshop_id = CAST(:wid AS uuid)
                  AND status IN ('queued','provisioning')
                """
            ),
            {"wid": workshop_id},
        )
    ).scalar_one()
    if int(in_flight or 0) > 0:
        raise HTTPException(
            status_code=409,
            detail="A delivery run is already queued/provisioning for this cohort.",
        )

    members = (
        await pg.execute(
            text(
                """
                SELECT user_id
                FROM entitlements
                WHERE workshop_id = CAST(:wid AS uuid)
                  AND status = 'active'
                ORDER BY created_at ASC
                """
            ),
            {"wid": workshop_id},
        )
    ).fetchall()
    if not members:
        raise HTTPException(status_code=400, detail="No active learners in this cohort.")

    dep_id = str(uuid4())
    workspace = f"workshop-{workshop_id[:8]}-{dep_id[:8]}".lower()
    lab_type = (workshop.get("metadata") or {}).get("lab_type") or "generic"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=duration_hours)

    await pg.execute(
        text(
            """
            INSERT INTO lab_deployments (
                id, user_id, content_id, workshop_id, lab_type, status,
                terraform_workspace, expires_at
            )
            VALUES (
                :id, :user_id, :content_id, :workshop_id, :lab_type, 'queued',
                :workspace, :expires_at
            )
            """
        ),
        {
            "id": dep_id,
            "user_id": str(current_user.id),
            "content_id": str(workshop["content_id"]),
            "workshop_id": workshop_id,
            "lab_type": lab_type,
            "workspace": workspace,
            "expires_at": expires_at,
        },
    )

    for m in members:
        await pg.execute(
            text(
                """
                INSERT INTO deployment_members (deployment_id, user_id, added_by)
                VALUES (:deployment_id, :user_id, :added_by)
                ON CONFLICT (deployment_id, user_id) DO NOTHING
                """
            ),
            {
                "deployment_id": dep_id,
                "user_id": str(m.user_id),
                "added_by": str(current_user.id),
            },
        )

    await pg.commit()
    await emit_ops_event(
        pg,
        event_key=f"cohort-run-requested:{workshop_id}:{dep_id}",
        event_type="cohort.run_requested",
        severity="info",
        title="Cohort lab session requested",
        message=f"Course admin requested a cohort session and attached {len(members)} learner(s).",
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="workshop",
        subject_id=workshop_id,
        workshop_id=workshop_id,
        deployment_id=dep_id,
        deep_link=f"/admin/ops/workshop/{workshop_id}",
        metadata={"members_attached": len(members), "duration_hours": duration_hours},
        emitter="course.router",
    )
    await pg.commit()
    return {
        "deployment_id": dep_id,
        "status": "queued",
        "expires_at": expires_at,
        "members_attached": len(members),
    }


@router.get("/{content_id}/deployments")
async def list_course_managed_deployments(
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """
    Lab deployments you queued as course admin for this content, with deployment_members
    (enrolled participants attached to each run).
    """
    await _verify_course_assignment(pg, str(current_user.id), content_id)

    dep_result = await pg.execute(
        text(
            """
            SELECT id, status, lab_type, created_at, expires_at, error_message
            FROM lab_deployments
            WHERE user_id = CAST(:uid AS uuid) AND content_id = CAST(:cid AS uuid)
            ORDER BY created_at DESC
            LIMIT 40
            """
        ),
        {"uid": str(current_user.id), "cid": content_id},
    )
    dep_rows = dep_result.mappings().all()
    out: list[dict[str, Any]] = []
    for row in dep_rows:
        did = str(row["id"])
        mem_result = await pg.execute(
            text(
                """
                SELECT dm.user_id, u.email
                FROM deployment_members dm
                JOIN users u ON u.id = dm.user_id
                WHERE dm.deployment_id = CAST(:did AS uuid)
                ORDER BY u.email ASC
                """
            ),
            {"did": did},
        )
        members = [
            {"user_id": str(m["user_id"]), "email": m["email"]}
            for m in mem_result.mappings().all()
        ]
        out.append(
            {
                "deployment_id": did,
                "status": row["status"],
                "lab_type": row["lab_type"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "error_message": row.get("error_message"),
                "members": members,
            }
        )
    return {"count": len(out), "deployments": out}


@router.post("/{content_id}/deploy")
@limiter.limit(settings.RATE_LIMIT_DEPLOY)
async def deploy_course_lab(
    body: LabDeployRequest,
    request: Request,
    response: Response,
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """
    Deploy a lab for a course.
    Enforces guardrails: max concurrent deployments and max duration.
    At most one deployment may be queued or provisioning at a time per course
    admin and lab (avoids accidental double queues); use an advisory lock so
    concurrent requests serialize cleanly.
    Automatically adds all enrolled participants to deployment_members.
    Requires at least one enrolled learner (no empty runs).
    """
    await _verify_course_assignment(pg, str(current_user.id), content_id)

    # Verify course exists and is active
    course = await pg.execute(
        text("""
            SELECT id, metadata FROM content_items
            WHERE id = :id AND type = 'lab' AND is_active = true
        """),
        {"id": content_id},
    )
    course_row = course.fetchone()
    if not course_row:
        raise HTTPException(status_code=404, detail="Course not found")

    lab_type = (course_row.metadata or {}).get("lab_type")
    if not lab_type:
        raise HTTPException(
            status_code=500,
            detail="Course is misconfigured: missing lab_type in metadata.",
        )

    # Normalise expires_at timezone
    if body.expires_at.tzinfo is None:
        expires_at = body.expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = body.expires_at

    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="expires_at must be in the future.")

    # ── Guardrail checks ──────────────────────────────────────────────────────
    max_concurrent, max_duration_hours = await _get_guardrails(
        pg, str(current_user.id), content_id
    )

    # Check duration
    max_expires_at = datetime.now(timezone.utc) + timedelta(hours=max_duration_hours)
    if expires_at > max_expires_at:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Lab duration cannot exceed {max_duration_hours} hours for this course. "
                f"Please set expires_at to at most {max_expires_at.isoformat()}."
            ),
        )

    owner_id = str(current_user.id)
    lock_key = f"{owner_id}:{content_id}"
    await pg.execute(
        text(
            "SELECT pg_advisory_xact_lock(:ns, hashtext(CAST(:lk AS text))::integer)"
        ),
        {"ns": _COURSE_DEPLOY_LOCK_NS, "lk": lock_key},
    )

    pending = await pg.execute(
        text("""
            SELECT 1 FROM lab_deployments
            WHERE user_id = :user_id
              AND content_id = :content_id
              AND status IN ('queued', 'provisioning')
            LIMIT 1
        """),
        {"user_id": owner_id, "content_id": content_id},
    )
    if pending.fetchone():
        raise HTTPException(
            status_code=409,
            detail=(
                "A lab run is already queued or starting for this course. "
                "Wait until its status is running, failed, or cleared before queuing another."
            ),
        )

    # Check concurrent deployments
    active_count_result = await pg.execute(
        text("""
            SELECT COUNT(*) FROM lab_deployments
            WHERE user_id = :user_id
              AND content_id = :content_id
              AND status IN ('queued', 'provisioning', 'running')
        """),
        {"user_id": owner_id, "content_id": content_id},
    )
    active_count = active_count_result.scalar()
    if active_count >= max_concurrent:
        raise HTTPException(
            status_code=400,
            detail=(
                f"You have reached the maximum of {max_concurrent} concurrent "
                f"deployments for this course. Please wait for an existing "
                f"deployment to expire before creating a new one."
            ),
        )

    roster_count_result = await pg.execute(
        text(
            "SELECT COUNT(*) FROM course_participants WHERE content_id = :content_id"
        ),
        {"content_id": content_id},
    )
    roster_count = roster_count_result.scalar() or 0
    if roster_count < 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "Enroll at least one learner on the roster before queuing a lab run. "
                "Each run attaches everyone who is enrolled at queue time."
            ),
        )

    # ── Insert deployment ─────────────────────────────────────────────────────
    deployment_id = uuid4()
    workspace = f"ws-{deployment_id}"

    await pg.execute(
        text("""
            INSERT INTO lab_deployments (
                id, user_id, content_id, lab_type,
                status, terraform_workspace, expires_at
            ) VALUES (
                :id, :user_id, :content_id, :lab_type,
                'queued', :workspace, :expires_at
            )
        """),
        {
            "id":         deployment_id,
            "user_id":    owner_id,
            "content_id": content_id,
            "lab_type":   lab_type,
            "workspace":  workspace,
            "expires_at": expires_at,
        },
    )

    # ── Auto-add enrolled participants to deployment_members ──────────────────
    participants = await pg.execute(
        text("""
            SELECT user_id FROM course_participants
            WHERE content_id = :content_id
        """),
        {"content_id": content_id},
    )
    participant_rows = participants.fetchall()

    for p in participant_rows:
        await pg.execute(
            text("""
                INSERT INTO deployment_members (deployment_id, user_id, added_by)
                VALUES (:deployment_id, :user_id, :added_by)
                ON CONFLICT (deployment_id, user_id) DO NOTHING
            """),
            {
                "deployment_id": str(deployment_id),
                "user_id":       str(p.user_id),
                "added_by":      str(current_user.id),
            },
        )

    await pg.commit()
    await emit_ops_event(
        pg,
        event_key=f"course-deploy-queued:{content_id}:{deployment_id}",
        event_type="course.deploy_queued",
        severity="info",
        title="Course-admin lab deploy queued",
        message=f"Legacy per-course deploy queued with {len(participant_rows)} participant(s).",
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="content",
        subject_id=content_id,
        deployment_id=str(deployment_id),
        deep_link=f"/admin/ops/individual/deployment/{deployment_id}",
        metadata={"participants_added": len(participant_rows)},
        emitter="course.router",
    )
    await pg.commit()

    log.info(
        "Course lab queued: deployment_id=%s course_admin=%s content_id=%s "
        "lab_type=%s expires_at=%s participants_added=%d",
        deployment_id, current_user.id, content_id,
        lab_type, expires_at, len(participant_rows),
    )
    return {
        "deployment_id":      str(deployment_id),
        "status":             "queued",
        "expires_at":         expires_at,
        "participants_added": len(participant_rows),
    }


@router.post("/{content_id}/participants/{user_id}", status_code=201)
async def enroll_participant(
    content_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """Enroll a participant in a course."""
    await _verify_course_assignment(pg, str(current_user.id), content_id)

    # Verify user exists and is active
    user = await pg.execute(
        text("SELECT id, email, is_active, role FROM users WHERE id = :id"),
        {"id": user_id},
    )
    user_row = user.fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    if not user_row.is_active:
        raise HTTPException(status_code=400, detail="User is inactive")

    await pg.execute(
        text("""
            INSERT INTO course_participants (user_id, content_id, enrolled_by)
            VALUES (:user_id, :content_id, :enrolled_by)
            ON CONFLICT (user_id, content_id) DO NOTHING
        """),
        {
            "user_id":     user_id,
            "content_id":  content_id,
            "enrolled_by": str(current_user.id),
        },
    )
    await pg.commit()

    log.info(
        "Participant enrolled: content_id=%s user_id=%s by=%s",
        content_id, user_id, current_user.id,
    )
    return {
        "content_id": content_id,
        "user_id":    user_id,
        "email":      user_row.email,
        "message":    "Participant enrolled successfully",
    }


@router.delete("/{content_id}/participants/{user_id}", status_code=200)
async def unenroll_participant(
    content_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """Unenroll a participant from a course."""
    await _verify_course_assignment(pg, str(current_user.id), content_id)

    result = await pg.execute(
        text("""
            DELETE FROM course_participants
            WHERE user_id = :user_id AND content_id = :content_id
            RETURNING user_id
        """),
        {"user_id": user_id, "content_id": content_id},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Participant not enrolled in this course")

    await pg.commit()
    log.info(
        "Participant unenrolled: content_id=%s user_id=%s by=%s",
        content_id, user_id, current_user.id,
    )
    return {
        "content_id": content_id,
        "user_id":    user_id,
        "message":    "Participant unenrolled",
    }


@router.get("/{content_id}/participants")
async def list_participants(
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(CourseAdminOrAbove),
):
    """List all participants enrolled in a course."""
    await _verify_course_assignment(pg, str(current_user.id), content_id)

    result = await pg.execute(
        text("""
            SELECT cp.user_id, u.email, cp.enrolled_by, cp.enrolled_at
            FROM course_participants cp
            JOIN users u ON cp.user_id = u.id
            WHERE cp.content_id = :content_id
            ORDER BY cp.enrolled_at ASC
        """),
        {"content_id": content_id},
    )
    rows = result.fetchall()
    return {
        "content_id": content_id,
        "count":      len(rows),
        "participants": [
            {
                "user_id":     r.user_id,
                "email":       r.email,
                "enrolled_by": r.enrolled_by,
                "enrolled_at": r.enrolled_at,
            }
            for r in rows
        ],
    }