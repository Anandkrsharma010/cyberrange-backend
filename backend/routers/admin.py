# backend/routers/admin.py
"""
sys_admin endpoints for course and user management.

Endpoints:
  POST   /admin/courses                            create a course
  GET    /admin/courses                            list all courses
  POST   /admin/courses/{id}/admins/{user_id}      assign course_admin
  DELETE /admin/courses/{id}/admins/{user_id}      remove course_admin
  DELETE /admin/courses/{content_id}               delete course (guarded)
  GET    /admin/courses/{id}/admins                list course_admins for a course
  POST   /admin/courses/{id}/guardrails/{user_id}  set guardrails for a course_admin
  POST   /admin/users/{id}/role                    set any user's role
"""

import logging
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.pg import get_pg
from backend.dependencies.authz import SysAdminOnly
from backend.schemas.auth import CurrentUser
from backend.schemas.admin import (
    CoursePriceUpsertRequest,
    CourseResourceCreateRequest,
    CourseResourcePatchRequest,
    CourseCreateRequest,
    CourseContentPatchRequest,
    CourseVisibilityPatchRequest,
    GuardrailSetRequest,
    RoleSetRequest,
    WebsitePageCreateRequest,
    WebsitePagePatchRequest,
    WebsitePageSectionCreateRequest,
    WebsitePageSectionPatchRequest,
    WebsitePageStatusPatchRequest,
    OpsFeedWorkflowPatchRequest,
)
from backend.config import (
    ROLE_SYS_ADMIN, ROLE_COURSE_ADMIN, ROLE_PARTICIPANT, ALL_ROLES,
    GUARDRAIL_DEFAULT_MAX_CONCURRENT, GUARDRAIL_DEFAULT_MAX_DURATION_HOURS,
)
from backend.services.course_admin_role import maybe_demote_course_admin_to_participant

log = logging.getLogger("admin")
router = APIRouter(prefix="/admin", tags=["Admin"])


async def _log_content_activity(
    pg: AsyncSession,
    actor_user_id: str,
    entity_type: str,
    entity_id: str,
    action: str,
    metadata: dict | None = None,
) -> None:
    await pg.execute(
        text(
            """
            INSERT INTO content_activity_logs (actor_user_id, entity_type, entity_id, action, metadata)
            VALUES (:actor_user_id, :entity_type, :entity_id, :action, CAST(:metadata AS jsonb))
            """
        ),
        {
            "actor_user_id": actor_user_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "action": action,
            "metadata": json.dumps(metadata or {}),
        },
    )


async def _create_page_revision(
    pg: AsyncSession,
    page_id: str,
    created_by: str,
    reason: str,
) -> str | None:
    snapshot_result = await pg.execute(
        text(
            """
            SELECT jsonb_build_object(
                'page', jsonb_build_object(
                    'id', p.id,
                    'slug', p.slug,
                    'title', p.title,
                    'description', p.description,
                    'status', p.status,
                    'seo_title', p.seo_title,
                    'seo_description', p.seo_description,
                    'published_at', p.published_at,
                    'archived_at', p.archived_at
                ),
                'sections', COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'id', s.id,
                            'section_key', s.section_key,
                            'section_type', s.section_type,
                            'position', s.position,
                            'is_visible', s.is_visible,
                            'payload', s.payload
                        )
                        ORDER BY s.position ASC
                    ) FILTER (WHERE s.id IS NOT NULL),
                    '[]'::jsonb
                )
            ) AS snapshot
            FROM website_pages p
            LEFT JOIN website_page_sections s ON s.page_id = p.id
            WHERE p.id = :page_id
            GROUP BY p.id
            """
        ),
        {"page_id": page_id},
    )
    row = snapshot_result.fetchone()
    if not row or not row.snapshot:
        return None
    revision_id = str(uuid4())
    await pg.execute(
        text(
            """
            INSERT INTO content_page_revisions (id, page_id, snapshot, reason, created_by)
            VALUES (:id, :page_id, CAST(:snapshot AS jsonb), :reason, :created_by)
            """
        ),
        {
            "id": revision_id,
            "page_id": page_id,
            "snapshot": json.dumps(row.snapshot),
            "reason": reason,
            "created_by": created_by,
        },
    )
    return revision_id


def _ops_feed_row(m: dict) -> dict:
    return {
        "id": str(m["id"]),
        "event_key": m["event_key"],
        "event_type": m["event_type"],
        "severity": m["severity"],
        "title": m["title"],
        "message": m["message"],
        "actor_user_id": str(m["actor_user_id"]) if m.get("actor_user_id") else None,
        "actor_email": m.get("actor_email"),
        "subject_type": m.get("subject_type"),
        "subject_id": m.get("subject_id"),
        "workshop_id": str(m["workshop_id"]) if m.get("workshop_id") else None,
        "deployment_id": str(m["deployment_id"]) if m.get("deployment_id") else None,
        "target_user_id": str(m["target_user_id"]) if m.get("target_user_id") else None,
        "deep_link": m.get("deep_link"),
        "metadata": m.get("metadata") or {},
        "acknowledged_at": m.get("acknowledged_at"),
        "acknowledged_by": str(m["acknowledged_by"]) if m.get("acknowledged_by") else None,
        "assigned_to_user_id": str(m["assigned_to_user_id"]) if m.get("assigned_to_user_id") else None,
        "escalation": m.get("escalation") or "none",
        "is_read": bool(m.get("is_read")),
        "read_at": m.get("read_at"),
        "read_by": str(m["read_by"]) if m.get("read_by") else None,
        "created_at": m.get("created_at"),
        "updated_at": m.get("updated_at"),
    }


@router.get("/ops-feed")
async def list_operations_feed(
    severity: str | None = Query(default=None, description="info | warning | critical"),
    is_read: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    allowed = {"info", "warning", "critical"}
    if severity and severity not in allowed:
        raise HTTPException(status_code=400, detail="severity must be one of: info, warning, critical")

    where = ["1=1"]
    params: dict[str, object] = {"limit": limit, "offset": offset}
    if severity:
        where.append("severity = :severity")
        params["severity"] = severity
    if is_read is not None:
        where.append("is_read = :is_read")
        params["is_read"] = is_read
    if q and q.strip():
        where.append(
            "(title ILIKE :q OR message ILIKE :q OR COALESCE(actor_email,'') ILIKE :q OR COALESCE(subject_id,'') ILIKE :q)"
        )
        params["q"] = f"%{q.strip()}%"

    where_sql = " AND ".join(where)
    total = (
        await pg.execute(
            text(f"SELECT COUNT(*) FROM operations_feed WHERE {where_sql}"),
            params,
        )
    ).scalar_one()
    rows = (
        await pg.execute(
            text(
                f"""
                SELECT *
                FROM operations_feed
                WHERE {where_sql}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
    ).mappings().all()
    return {"count": int(total or 0), "rows": [_ops_feed_row(dict(r)) for r in rows]}


@router.get("/ops-feed/unread-count")
async def unread_operations_feed_count(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    out = (
        await pg.execute(text("SELECT COUNT(*) FROM operations_feed WHERE is_read = false"))
    ).scalar_one()
    return {"count": int(out or 0)}


@router.post("/ops-feed/{event_id}/read", status_code=200)
async def mark_operations_feed_read(
    event_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    updated = (
        await pg.execute(
            text(
                """
                UPDATE operations_feed
                SET is_read = true, read_at = now(), read_by = CAST(:admin_id AS uuid), updated_at = now()
                WHERE id = CAST(:event_id AS uuid)
                RETURNING id
                """
            ),
            {"event_id": event_id, "admin_id": str(admin.id)},
        )
    ).fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="Feed event not found")
    await pg.commit()
    return {"ok": True, "id": event_id}


@router.post("/ops-feed/read-all", status_code=200)
async def mark_all_operations_feed_read(
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    result = await pg.execute(
        text(
            """
            UPDATE operations_feed
            SET is_read = true, read_at = now(), read_by = CAST(:admin_id AS uuid), updated_at = now()
            WHERE is_read = false
            """
        ),
        {"admin_id": str(admin.id)},
    )
    await pg.commit()
    return {"ok": True, "updated": int(result.rowcount or 0)}


@router.post("/ops-feed/repair-read-state", status_code=200)
async def repair_operations_feed_read_state(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """
    Fix inconsistent read flags (e.g. legacy rows) before or after DB CHECK
    ``operations_feed_read_state_chk``. Safe to run multiple times.
    """
    r1 = await pg.execute(
        text(
            """
            UPDATE operations_feed
            SET is_read = false, read_at = NULL, read_by = NULL
            WHERE is_read = true
              AND (read_at IS NULL OR read_by IS NULL)
            """
        )
    )
    r2 = await pg.execute(
        text(
            """
            UPDATE operations_feed
            SET read_at = NULL, read_by = NULL
            WHERE is_read = false
              AND (read_at IS NOT NULL OR read_by IS NOT NULL)
            """
        )
    )
    await pg.commit()
    return {
        "ok": True,
        "reset_incomplete_read_rows": int(r1.rowcount or 0),
        "cleared_stale_read_metadata_rows": int(r2.rowcount or 0),
    }


@router.post("/ops-feed/{event_id}/acknowledge", status_code=200)
async def acknowledge_operations_feed_item(
    event_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """Mark a feed item as acknowledged by the current sys_admin (Phase 3)."""
    updated = (
        await pg.execute(
            text(
                """
                UPDATE operations_feed
                SET acknowledged_at = now(),
                    acknowledged_by = CAST(:admin_id AS uuid),
                    updated_at = now()
                WHERE id = CAST(:event_id AS uuid)
                RETURNING id
                """
            ),
            {"event_id": event_id, "admin_id": str(admin.id)},
        )
    ).fetchone()
    if not updated:
        raise HTTPException(status_code=404, detail="Feed event not found")
    await pg.commit()
    return {"ok": True, "id": event_id}


@router.patch("/ops-feed/{event_id}/workflow", status_code=200)
async def patch_operations_feed_workflow(
    body: OpsFeedWorkflowPatchRequest,
    event_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """Update assignee and/or escalation without affecting read state (Phase 3)."""
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(
            status_code=400,
            detail="Provide assigned_to_user_id and/or escalation to update.",
        )

    exists = (
        await pg.execute(
            text("SELECT 1 FROM operations_feed WHERE id = CAST(:id AS uuid)"),
            {"id": event_id},
        )
    ).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="Feed event not found")

    if "assigned_to_user_id" in patch:
        raw = patch.get("assigned_to_user_id")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            await pg.execute(
                text(
                    """
                    UPDATE operations_feed
                    SET assigned_to_user_id = NULL, updated_at = now()
                    WHERE id = CAST(:eid AS uuid)
                    """
                ),
                {"eid": event_id},
            )
        else:
            uid = str(raw).strip()
            ur = await pg.execute(
                text(
                    "SELECT 1 FROM users WHERE id = CAST(:id AS uuid) AND is_active = true"
                ),
                {"id": uid},
            )
            if not ur.fetchone():
                raise HTTPException(
                    status_code=400, detail="Assignee user not found or inactive"
                )
            await pg.execute(
                text(
                    """
                    UPDATE operations_feed
                    SET assigned_to_user_id = CAST(:uid AS uuid), updated_at = now()
                    WHERE id = CAST(:eid AS uuid)
                    """
                ),
                {"uid": uid, "eid": event_id},
            )

    if "escalation" in patch and patch["escalation"] is not None:
        await pg.execute(
            text(
                """
                UPDATE operations_feed
                SET escalation = :esc, updated_at = now()
                WHERE id = CAST(:eid AS uuid)
                """
            ),
            {"esc": patch["escalation"], "eid": event_id},
        )

    await pg.commit()
    return {"ok": True, "id": event_id}


# ── Courses ───────────────────────────────────────────────────────────────────

@router.post("/courses", status_code=201)
async def create_course(
    body: CourseCreateRequest,
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """Create a new course (content_items entry of type 'lab')."""
    content_id = uuid4()
    await pg.execute(
        text("""
            INSERT INTO content_items
                (id, type, title, description, difficulty, duration_minutes, metadata, visibility)
            VALUES
                (:id, 'lab', :title, :description, :difficulty, :duration_minutes, :metadata, :visibility)
        """),
        {
            "id":               content_id,
            "title":            body.title,
            "description":      body.description,
            "difficulty":       body.difficulty,
            "duration_minutes": body.duration_minutes,
            "metadata":         body.metadata_json,
            "visibility":       body.visibility,
        },
    )
    await pg.commit()
    log.info("Course created: content_id=%s title=%s by=%s", content_id, body.title, admin.id)
    return {"content_id": str(content_id), "title": body.title}


@router.get("/courses")
async def list_courses(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """List all active courses."""
    result = await pg.execute(
        text("""
            SELECT id, title, description, difficulty, duration_minutes,
                   is_active, visibility, created_at
            FROM content_items
            WHERE type = 'lab'
            ORDER BY created_at DESC
        """)
    )
    rows = result.fetchall()
    return {
        "count": len(rows),
        "courses": [
            {
                "content_id":       r.id,
                "title":            r.title,
                "description":      r.description,
                "difficulty":       r.difficulty,
                "duration_minutes": r.duration_minutes,
                "is_active":        r.is_active,
                "visibility":       r.visibility,
                "created_at":       r.created_at,
            }
            for r in rows
        ],
    }


@router.get("/courses/{content_id}", status_code=200)
async def get_course(
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """Full course row for admin editing (includes metadata)."""
    result = await pg.execute(
        text("""
            SELECT id, title, description, difficulty, duration_minutes,
                   is_active, visibility, metadata, created_at
            FROM content_items
            WHERE id = :id AND type = 'lab'
        """),
        {"id": content_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Course not found")
    meta = row.metadata or {}
    chips = meta.get("feature_chips") if isinstance(meta.get("feature_chips"), list) else []
    return {
        "content_id": str(row.id),
        "title": row.title,
        "description": row.description,
        "difficulty": row.difficulty,
        "duration_minutes": row.duration_minutes,
        "is_active": row.is_active,
        "visibility": row.visibility,
        "lab_type": meta.get("lab_type"),
        "slug": meta.get("slug"),
        "feature_chips": [str(c) for c in chips if str(c).strip()],
        "created_at": row.created_at,
    }


@router.patch("/courses/{content_id}/content", status_code=200)
async def patch_course_content(
    body: CourseContentPatchRequest,
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """Update catalog-facing course fields and metadata."""
    existing = await pg.execute(
        text("SELECT metadata FROM content_items WHERE id = :id AND type = 'lab'"),
        {"id": content_id},
    )
    row = existing.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Course not found")

    meta = dict(row.metadata or {})
    if body.lab_type is not None:
        meta["lab_type"] = body.lab_type.strip()
    if body.slug is not None:
        slug = body.slug.strip().lower()
        if slug:
            meta["slug"] = slug
        elif "slug" in meta:
            del meta["slug"]
    if body.feature_chips is not None:
        meta["feature_chips"] = [c.strip() for c in body.feature_chips if c and c.strip()]

    updates = []
    params: dict = {"id": content_id, "metadata": json.dumps(meta)}
    if body.title is not None:
        updates.append("title = :title")
        params["title"] = body.title
    if body.description is not None:
        updates.append("description = :description")
        params["description"] = body.description
    if body.difficulty is not None:
        updates.append("difficulty = :difficulty")
        params["difficulty"] = body.difficulty or None
    if body.duration_minutes is not None:
        updates.append("duration_minutes = :duration_minutes")
        params["duration_minutes"] = body.duration_minutes
    updates.append("metadata = CAST(:metadata AS jsonb)")

    await pg.execute(
        text(f"""
            UPDATE content_items
            SET {", ".join(updates)}
            WHERE id = :id AND type = 'lab'
        """),
        params,
    )
    await pg.commit()
    log.info("Course content updated: content_id=%s by=%s", content_id, admin.id)
    return {"content_id": content_id, "ok": True}


async def _course_delete_blockers(pg: AsyncSession, content_id: str) -> list[str]:
    """Return human-readable reasons when a lab course cannot be hard-deleted."""
    reasons: list[str] = []

    w = await pg.execute(
        text("SELECT COUNT(*)::int AS n FROM workshops WHERE content_id = :id"),
        {"id": content_id},
    )
    n_workshops = int(w.scalar() or 0)
    if n_workshops:
        reasons.append(f"{n_workshops} workshop(s) linked — delete or reassign workshops first")

    p = await pg.execute(
        text("SELECT COUNT(*)::int AS n FROM purchases WHERE content_id = :id"),
        {"id": content_id},
    )
    n_purchases = int(p.scalar() or 0)
    if n_purchases:
        reasons.append(f"{n_purchases} purchase record(s) — hide the course instead (Unlisted)")

    e = await pg.execute(
        text("SELECT COUNT(*)::int AS n FROM entitlements WHERE content_id = :id"),
        {"id": content_id},
    )
    n_ent = int(e.scalar() or 0)
    if n_ent:
        reasons.append(f"{n_ent} entitlement(s) — revoke access or hide the course instead")

    d = await pg.execute(
        text(
            """
            SELECT COUNT(*)::int AS n
            FROM lab_deployments
            WHERE content_id = :id
              AND status NOT IN ('destroyed', 'failed', 'expired')
            """
        ),
        {"id": content_id},
    )
    n_dep = int(d.scalar() or 0)
    if n_dep:
        reasons.append(f"{n_dep} active lab deployment(s) — wait for teardown or destroy labs first")

    return reasons


@router.delete("/courses/{content_id}", status_code=200)
async def delete_course(
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """Hard-delete a lab course when no workshops, purchases, entitlements, or active deployments block it."""
    exists = await pg.execute(
        text("SELECT id, title FROM content_items WHERE id = :id AND type = 'lab'"),
        {"id": content_id},
    )
    row = exists.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Course not found")

    blockers = await _course_delete_blockers(pg, content_id)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={"message": "Cannot delete this course", "reasons": blockers},
        )

    await pg.execute(
        text("DELETE FROM content_items WHERE id = :id AND type = 'lab'"),
        {"id": content_id},
    )
    await pg.commit()
    log.info("Course deleted: content_id=%s title=%s by=%s", content_id, row.title, admin.id)
    return {"content_id": content_id, "deleted": True, "title": row.title}


@router.patch("/courses/{content_id}", status_code=200)
async def patch_course(
    body: CourseVisibilityPatchRequest,
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. Update lab visibility (public / unlisted / private)."""
    result = await pg.execute(
        text("""
            UPDATE content_items
            SET visibility = :visibility
            WHERE id = :id AND type = 'lab'
            RETURNING id
        """),
        {"id": content_id, "visibility": body.visibility},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Course not found")
    await pg.commit()
    log.info("Course visibility updated: content_id=%s visibility=%s", content_id, body.visibility)
    return {"content_id": content_id, "visibility": body.visibility}


@router.get("/courses/{content_id}/price", status_code=200)
async def get_course_price(
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    course = await pg.execute(
        text(
            """
            SELECT id
            FROM content_items
            WHERE id = :id AND type = 'lab'
            """
        ),
        {"id": content_id},
    )
    if not course.fetchone():
        raise HTTPException(status_code=404, detail="Course not found")

    result = await pg.execute(
        text(
            """
            SELECT content_id, amount_minor, currency, is_active, created_at
            FROM product_prices
            WHERE content_id = :content_id
            LIMIT 1
            """
        ),
        {"content_id": content_id},
    )
    row = result.fetchone()
    if not row:
        return {
            "content_id": content_id,
            "price": None,
        }

    return {
        "content_id": content_id,
        "price": {
            "amount_minor": int(row.amount_minor),
            "currency": row.currency,
            "is_active": bool(row.is_active),
            "created_at": row.created_at,
        },
    }


@router.put("/courses/{content_id}/price", status_code=200)
async def upsert_course_price(
    body: CoursePriceUpsertRequest,
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    course = await pg.execute(
        text(
            """
            SELECT id
            FROM content_items
            WHERE id = :id AND type = 'lab'
            """
        ),
        {"id": content_id},
    )
    if not course.fetchone():
        raise HTTPException(status_code=404, detail="Course not found")

    await pg.execute(
        text(
            """
            INSERT INTO product_prices (content_id, amount_minor, currency, is_active)
            VALUES (:content_id, :amount_minor, :currency, :is_active)
            ON CONFLICT (content_id)
            DO UPDATE SET
                amount_minor = EXCLUDED.amount_minor,
                currency = EXCLUDED.currency,
                is_active = EXCLUDED.is_active
            """
        ),
        {
            "content_id": content_id,
            "amount_minor": body.amount_minor,
            "currency": body.currency,
            "is_active": body.is_active,
        },
    )
    await pg.commit()
    log.info(
        "Course price upserted: content_id=%s amount_minor=%s currency=%s is_active=%s by=%s",
        content_id,
        body.amount_minor,
        body.currency,
        body.is_active,
        admin.id,
    )
    return {
        "content_id": content_id,
        "amount_minor": body.amount_minor,
        "currency": body.currency,
        "is_active": body.is_active,
    }


@router.post("/courses/{content_id}/resources", status_code=201)
async def create_course_resource(
    body: CourseResourceCreateRequest,
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    resource_id = uuid4()
    await pg.execute(
        text(
            """
            INSERT INTO course_resources
                (id, content_id, title, description, resource_type, url, file_key,
                 mime_type, position, is_visible, metadata, created_by, updated_by)
            VALUES
                (:id, :content_id, :title, :description, :resource_type, :url, :file_key,
                 :mime_type, :position, :is_visible, CAST(:metadata AS jsonb), :created_by, :updated_by)
            """
        ),
        {
            "id": str(resource_id),
            "content_id": content_id,
            "title": body.title,
            "description": body.description,
            "resource_type": body.resource_type,
            "url": body.url,
            "file_key": body.file_key,
            "mime_type": body.mime_type,
            "position": body.position,
            "is_visible": body.is_visible,
            "metadata": body.metadata_json,
            "created_by": str(admin.id),
            "updated_by": str(admin.id),
        },
    )
    await _log_content_activity(
        pg,
        str(admin.id),
        "course_resource",
        str(resource_id),
        "created",
        {"content_id": content_id, "resource_type": body.resource_type},
    )
    await pg.commit()
    return {"resource_id": str(resource_id), "content_id": content_id}


@router.get("/courses/{content_id}/resources")
async def list_course_resources(
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    result = await pg.execute(
        text(
            """
            SELECT id, title, description, resource_type, url, file_key, mime_type,
                   position, is_visible, metadata, created_at, updated_at
            FROM course_resources
            WHERE content_id = :content_id
            ORDER BY position ASC, created_at ASC
            """
        ),
        {"content_id": content_id},
    )
    rows = result.fetchall()
    return {
        "content_id": content_id,
        "count": len(rows),
        "resources": [
            {
                "resource_id": r.id,
                "title": r.title,
                "description": r.description,
                "resource_type": r.resource_type,
                "url": r.url,
                "file_key": r.file_key,
                "mime_type": r.mime_type,
                "position": r.position,
                "is_visible": r.is_visible,
                "metadata": r.metadata,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ],
    }


@router.patch("/courses/{content_id}/resources/{resource_id}", status_code=200)
async def patch_course_resource(
    body: CourseResourcePatchRequest,
    content_id: str = Path(...),
    resource_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    updates = []
    params = {
        "resource_id": resource_id,
        "content_id": content_id,
        "updated_by": str(admin.id),
    }
    if body.title is not None:
        updates.append("title = :title")
        params["title"] = body.title
    if body.description is not None:
        updates.append("description = :description")
        params["description"] = body.description
    if body.resource_type is not None:
        updates.append("resource_type = :resource_type")
        params["resource_type"] = body.resource_type
    if body.url is not None:
        updates.append("url = :url")
        params["url"] = body.url
    if body.file_key is not None:
        updates.append("file_key = :file_key")
        params["file_key"] = body.file_key
    if body.mime_type is not None:
        updates.append("mime_type = :mime_type")
        params["mime_type"] = body.mime_type
    if body.position is not None:
        updates.append("position = :position")
        params["position"] = body.position
    if body.is_visible is not None:
        updates.append("is_visible = :is_visible")
        params["is_visible"] = body.is_visible
    if body.metadata_json is not None:
        updates.append("metadata = CAST(:metadata AS jsonb)")
        params["metadata"] = body.metadata_json
    if not updates:
        raise HTTPException(status_code=400, detail="No resource fields provided for update")

    updates.append("updated_by = :updated_by")
    updates.append("updated_at = now()")
    result = await pg.execute(
        text(
            f"""
            UPDATE course_resources
            SET {", ".join(updates)}
            WHERE id = :resource_id AND content_id = :content_id
            RETURNING id
            """
        ),
        params,
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Course resource not found")
    await _log_content_activity(
        pg,
        str(admin.id),
        "course_resource",
        resource_id,
        "updated",
        {"content_id": content_id},
    )
    await pg.commit()
    return {"resource_id": resource_id, "content_id": content_id}


@router.delete("/courses/{content_id}/resources/{resource_id}", status_code=200)
async def delete_course_resource(
    content_id: str = Path(...),
    resource_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    result = await pg.execute(
        text(
            """
            DELETE FROM course_resources
            WHERE id = :resource_id AND content_id = :content_id
            RETURNING id
            """
        ),
        {"resource_id": resource_id, "content_id": content_id},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Course resource not found")
    await _log_content_activity(
        pg,
        str(_admin.id),
        "course_resource",
        resource_id,
        "deleted",
        {"content_id": content_id},
    )
    await pg.commit()
    return {"resource_id": resource_id, "content_id": content_id, "deleted": True}


# ── Content Studio (website pages) ────────────────────────────────────────────

@router.post("/content/pages", status_code=201)
async def create_website_page(
    body: WebsitePageCreateRequest,
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    page_id = uuid4()
    try:
        await pg.execute(
            text(
                """
                INSERT INTO website_pages
                    (id, slug, title, description, seo_title, seo_description, created_by, updated_by)
                VALUES
                    (:id, :slug, :title, :description, :seo_title, :seo_description, :created_by, :updated_by)
                """
            ),
            {
                "id": str(page_id),
                "slug": body.slug.strip().lower(),
                "title": body.title,
                "description": body.description,
                "seo_title": body.seo_title,
                "seo_description": body.seo_description,
                "created_by": str(admin.id),
                "updated_by": str(admin.id),
            },
        )
    except Exception as exc:
        await pg.rollback()
        raise HTTPException(status_code=409, detail="Page slug already exists or payload invalid") from exc
    await _log_content_activity(
        pg,
        str(admin.id),
        "website_page",
        str(page_id),
        "created",
        {"slug": body.slug.strip().lower()},
    )
    await pg.commit()
    return {"page_id": str(page_id), "slug": body.slug.strip().lower()}


@router.get("/content/pages")
async def list_website_pages(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    result = await pg.execute(
        text(
            """
            SELECT id, slug, title, description, status, seo_title, seo_description,
                   published_at, archived_at, created_at, updated_at
            FROM website_pages
            ORDER BY updated_at DESC
            """
        )
    )
    rows = result.fetchall()
    return {
        "count": len(rows),
        "pages": [
            {
                "page_id": r.id,
                "slug": r.slug,
                "title": r.title,
                "description": r.description,
                "status": r.status,
                "seo_title": r.seo_title,
                "seo_description": r.seo_description,
                "published_at": r.published_at,
                "archived_at": r.archived_at,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ],
    }


@router.get("/content/pages/{page_id}")
async def get_website_page(
    page_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    page_result = await pg.execute(
        text(
            """
            SELECT id, slug, title, description, status, seo_title, seo_description,
                   published_at, archived_at, created_at, updated_at
            FROM website_pages
            WHERE id = :page_id
            """
        ),
        {"page_id": page_id},
    )
    page = page_result.fetchone()
    if not page:
        raise HTTPException(status_code=404, detail="Website page not found")

    sections_result = await pg.execute(
        text(
            """
            SELECT id, section_key, section_type, position, is_visible, payload,
                   created_at, updated_at
            FROM website_page_sections
            WHERE page_id = :page_id
            ORDER BY position ASC, created_at ASC
            """
        ),
        {"page_id": page_id},
    )
    sections = sections_result.fetchall()
    return {
        "page": {
            "page_id": page.id,
            "slug": page.slug,
            "title": page.title,
            "description": page.description,
            "status": page.status,
            "seo_title": page.seo_title,
            "seo_description": page.seo_description,
            "published_at": page.published_at,
            "archived_at": page.archived_at,
            "created_at": page.created_at,
            "updated_at": page.updated_at,
        },
        "sections": [
            {
                "section_id": s.id,
                "section_key": s.section_key,
                "section_type": s.section_type,
                "position": s.position,
                "is_visible": s.is_visible,
                "payload": s.payload,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in sections
        ],
    }


@router.patch("/content/pages/{page_id}", status_code=200)
async def patch_website_page(
    body: WebsitePagePatchRequest,
    page_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    updates = []
    params = {"page_id": page_id, "updated_by": str(admin.id)}
    if body.title is not None:
        updates.append("title = :title")
        params["title"] = body.title
    if body.description is not None:
        updates.append("description = :description")
        params["description"] = body.description
    if body.seo_title is not None:
        updates.append("seo_title = :seo_title")
        params["seo_title"] = body.seo_title
    if body.seo_description is not None:
        updates.append("seo_description = :seo_description")
        params["seo_description"] = body.seo_description
    if not updates:
        raise HTTPException(status_code=400, detail="No page fields provided for update")
    updates.append("updated_by = :updated_by")
    updates.append("updated_at = now()")

    result = await pg.execute(
        text(
            f"""
            UPDATE website_pages
            SET {", ".join(updates)}
            WHERE id = :page_id
            RETURNING id
            """
        ),
        params,
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Website page not found")
    await _log_content_activity(pg, str(admin.id), "website_page", page_id, "updated")
    await pg.commit()
    return {"page_id": page_id}


@router.patch("/content/pages/{page_id}/status", status_code=200)
async def patch_website_page_status(
    body: WebsitePageStatusPatchRequest,
    page_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    if body.status == "published":
        await _create_page_revision(pg, page_id, str(admin.id), "publish")
    result = await pg.execute(
        text(
            """
            UPDATE website_pages
            SET status = :status,
                published_at = CASE WHEN :status = 'published' THEN now() ELSE published_at END,
                archived_at = CASE WHEN :status = 'archived' THEN now() ELSE archived_at END,
                updated_by = :updated_by,
                updated_at = now()
            WHERE id = :page_id
            RETURNING id, status
            """
        ),
        {"page_id": page_id, "status": body.status, "updated_by": str(admin.id)},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Website page not found")
    await _log_content_activity(
        pg,
        str(admin.id),
        "website_page",
        str(row.id),
        "status_changed",
        {"status": row.status},
    )
    await pg.commit()
    return {"page_id": row.id, "status": row.status}


@router.post("/content/pages/{page_id}/sections", status_code=201)
async def create_website_page_section(
    body: WebsitePageSectionCreateRequest,
    page_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    section_id = uuid4()
    await pg.execute(
        text(
            """
            INSERT INTO website_page_sections
                (id, page_id, section_key, section_type, position, is_visible, payload, created_by, updated_by)
            VALUES
                (:id, :page_id, :section_key, :section_type, :position, :is_visible,
                 CAST(:payload AS jsonb), :created_by, :updated_by)
            """
        ),
        {
            "id": str(section_id),
            "page_id": page_id,
            "section_key": body.section_key,
            "section_type": body.section_type,
            "position": body.position,
            "is_visible": body.is_visible,
            "payload": body.payload_json,
            "created_by": str(admin.id),
            "updated_by": str(admin.id),
        },
    )
    await _log_content_activity(
        pg,
        str(admin.id),
        "website_page_section",
        str(section_id),
        "created",
        {"page_id": page_id, "section_type": body.section_type},
    )
    await pg.commit()
    return {"section_id": str(section_id), "page_id": page_id}


@router.patch("/content/sections/{section_id}", status_code=200)
async def patch_website_page_section(
    body: WebsitePageSectionPatchRequest,
    section_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    updates = []
    params = {"section_id": section_id, "updated_by": str(admin.id)}
    if body.section_key is not None:
        updates.append("section_key = :section_key")
        params["section_key"] = body.section_key
    if body.section_type is not None:
        updates.append("section_type = :section_type")
        params["section_type"] = body.section_type
    if body.position is not None:
        updates.append("position = :position")
        params["position"] = body.position
    if body.is_visible is not None:
        updates.append("is_visible = :is_visible")
        params["is_visible"] = body.is_visible
    if body.payload_json is not None:
        updates.append("payload = CAST(:payload AS jsonb)")
        params["payload"] = body.payload_json
    if not updates:
        raise HTTPException(status_code=400, detail="No section fields provided for update")
    updates.append("updated_by = :updated_by")
    updates.append("updated_at = now()")
    result = await pg.execute(
        text(
            f"""
            UPDATE website_page_sections
            SET {", ".join(updates)}
            WHERE id = :section_id
            RETURNING id
            """
        ),
        params,
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Website page section not found")
    await _log_content_activity(pg, str(admin.id), "website_page_section", section_id, "updated")
    await pg.commit()
    return {"section_id": section_id}


@router.delete("/content/sections/{section_id}", status_code=200)
async def delete_website_page_section(
    section_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    result = await pg.execute(
        text("DELETE FROM website_page_sections WHERE id = :section_id RETURNING id"),
        {"section_id": section_id},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Website page section not found")
    await _log_content_activity(
        pg, str(_admin.id), "website_page_section", section_id, "deleted"
    )
    await pg.commit()
    return {"section_id": section_id, "deleted": True}


@router.get("/content/pages/{page_id}/revisions")
async def list_website_page_revisions(
    page_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    result = await pg.execute(
        text(
            """
            SELECT id, reason, created_by, created_at
            FROM content_page_revisions
            WHERE page_id = :page_id
            ORDER BY created_at DESC
            LIMIT 50
            """
        ),
        {"page_id": page_id},
    )
    rows = result.fetchall()
    return {
        "page_id": page_id,
        "count": len(rows),
        "revisions": [
            {
                "revision_id": r.id,
                "reason": r.reason,
                "created_by": r.created_by,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.post("/content/pages/{page_id}/rollback/{revision_id}", status_code=200)
async def rollback_website_page_revision(
    page_id: str = Path(...),
    revision_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    rev_result = await pg.execute(
        text(
            """
            SELECT snapshot
            FROM content_page_revisions
            WHERE id = :revision_id AND page_id = :page_id
            LIMIT 1
            """
        ),
        {"revision_id": revision_id, "page_id": page_id},
    )
    rev = rev_result.fetchone()
    if not rev:
        raise HTTPException(status_code=404, detail="Revision not found")

    snapshot = rev.snapshot or {}
    page = snapshot.get("page") or {}
    sections = snapshot.get("sections") or []
    await pg.execute(
        text(
            """
            UPDATE website_pages
            SET title = :title,
                description = :description,
                status = :status,
                seo_title = :seo_title,
                seo_description = :seo_description,
                published_at = :published_at,
                archived_at = :archived_at,
                updated_by = :updated_by,
                updated_at = now()
            WHERE id = :page_id
            """
        ),
        {
            "page_id": page_id,
            "title": page.get("title"),
            "description": page.get("description"),
            "status": page.get("status") or "draft",
            "seo_title": page.get("seo_title"),
            "seo_description": page.get("seo_description"),
            "published_at": page.get("published_at"),
            "archived_at": page.get("archived_at"),
            "updated_by": str(admin.id),
        },
    )
    await pg.execute(
        text("DELETE FROM website_page_sections WHERE page_id = :page_id"),
        {"page_id": page_id},
    )
    for s in sections:
        await pg.execute(
            text(
                """
                INSERT INTO website_page_sections
                    (id, page_id, section_key, section_type, position, is_visible, payload, created_by, updated_by)
                VALUES
                    (:id, :page_id, :section_key, :section_type, :position, :is_visible,
                     CAST(:payload AS jsonb), :created_by, :updated_by)
                """
            ),
            {
                "id": str(s.get("id") or uuid4()),
                "page_id": page_id,
                "section_key": s.get("section_key"),
                "section_type": s.get("section_type"),
                "position": int(s.get("position") or 0),
                "is_visible": bool(s.get("is_visible", True)),
                "payload": json.dumps(s.get("payload") or {}),
                "created_by": str(admin.id),
                "updated_by": str(admin.id),
            },
        )
    await _log_content_activity(
        pg,
        str(admin.id),
        "website_page",
        page_id,
        "rolled_back",
        {"revision_id": revision_id},
    )
    await pg.commit()
    return {"page_id": page_id, "revision_id": revision_id, "rolled_back": True}


@router.get("/content/activity")
async def list_content_activity(
    limit: int = 100,
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    safe_limit = max(1, min(limit, 500))
    result = await pg.execute(
        text(
            """
            SELECT id, actor_user_id, entity_type, entity_id, action, metadata, created_at
            FROM content_activity_logs
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": safe_limit},
    )
    rows = result.fetchall()
    return {
        "count": len(rows),
        "rows": [
            {
                "id": r.id,
                "actor_user_id": r.actor_user_id,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "action": r.action,
                "metadata": r.metadata,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


# ── Course admin assignments ──────────────────────────────────────────────────

@router.post("/courses/{content_id}/admins/{user_id}", status_code=201)
async def assign_course_admin(
    content_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """Assign a course_admin to a course. Promotes user to course_admin if needed."""
    # Verify course exists
    course = await pg.execute(
        text("SELECT id FROM content_items WHERE id = :id AND type = 'lab'"),
        {"id": content_id},
    )
    if not course.fetchone():
        raise HTTPException(status_code=404, detail="Course not found")

    # Verify user exists and is active
    user = await pg.execute(
        text("SELECT id, role, is_active FROM users WHERE id = :id"),
        {"id": user_id},
    )
    user_row = user.fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    if not user_row.is_active:
        raise HTTPException(status_code=400, detail="User is inactive")
    if user_row.role == ROLE_SYS_ADMIN:
        raise HTTPException(
            status_code=400,
            detail="Cannot assign sys_admin as a course_admin.",
        )

    # Promote to course_admin if they are currently a participant
    if user_row.role == ROLE_PARTICIPANT:
        await pg.execute(
            text("UPDATE users SET role = :role, updated_at = now() WHERE id = :id"),
            {"role": ROLE_COURSE_ADMIN, "id": user_id},
        )
        log.info("User promoted to course_admin: user_id=%s by=%s", user_id, admin.id)

    # Insert assignment
    await pg.execute(
        text("""
            INSERT INTO course_admin_assignments (user_id, content_id, assigned_by)
            VALUES (:user_id, :content_id, :assigned_by)
            ON CONFLICT (user_id, content_id) DO NOTHING
        """),
        {"user_id": user_id, "content_id": content_id, "assigned_by": str(admin.id)},
    )
    await pg.commit()

    log.info(
        "Course admin assigned: content_id=%s user_id=%s by=%s",
        content_id, user_id, admin.id,
    )
    return {
        "content_id": content_id,
        "user_id":    user_id,
        "message":    "course_admin assigned successfully",
    }


@router.delete("/courses/{content_id}/admins/{user_id}", status_code=200)
async def remove_course_admin(
    content_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """
    Remove a course_admin assignment.
    If the user has no remaining assignments, demotes them back to participant.
    """
    result = await pg.execute(
        text("""
            DELETE FROM course_admin_assignments
            WHERE user_id = :user_id AND content_id = :content_id
            RETURNING user_id
        """),
        {"user_id": user_id, "content_id": content_id},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Assignment not found")

    await maybe_demote_course_admin_to_participant(
        pg, user_id, demoted_by=str(admin.id)
    )

    await pg.commit()
    log.info(
        "Course admin removed: content_id=%s user_id=%s by=%s",
        content_id, user_id, admin.id,
    )
    return {
        "content_id": content_id,
        "user_id":    user_id,
        "message":    "course_admin removed",
    }


@router.get("/courses/{content_id}/admins")
async def list_course_admins(
    content_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """List all course_admins assigned to a course."""
    result = await pg.execute(
        text("""
            SELECT
                caa.user_id,
                u.email,
                caa.assigned_by,
                caa.assigned_at,
                g.max_concurrent_deployments,
                g.max_duration_hours,
                g.updated_at AS guardrail_updated_at,
                g.set_by AS guardrail_set_by,
                su.email AS guardrail_set_by_email,
                CASE WHEN g.course_admin_id IS NULL THEN 'default' ELSE 'custom' END AS guardrail_source,
                COALESCE((
                    SELECT COUNT(*)::int
                    FROM lab_deployments ld
                    WHERE ld.user_id = caa.user_id
                      AND ld.content_id = caa.content_id
                      AND ld.status IN ('queued', 'provisioning', 'running')
                ), 0) AS active_deployments_count
            FROM course_admin_assignments caa
            JOIN users u ON caa.user_id = u.id
            LEFT JOIN course_guardrails g
                ON g.course_admin_id = caa.user_id AND g.content_id = caa.content_id
            LEFT JOIN users su ON su.id = g.set_by
            WHERE caa.content_id = :content_id
            ORDER BY caa.assigned_at ASC
        """),
        {"content_id": content_id},
    )
    rows = result.fetchall()
    return {
        "content_id": content_id,
        "count":      len(rows),
        "admins": [
            {
                "user_id":                    r.user_id,
                "email":                      r.email,
                "assigned_by":                r.assigned_by,
                "assigned_at":                r.assigned_at,
                "max_concurrent_deployments": r.max_concurrent_deployments
                                              or GUARDRAIL_DEFAULT_MAX_CONCURRENT,
                "max_duration_hours":         r.max_duration_hours
                                              or GUARDRAIL_DEFAULT_MAX_DURATION_HOURS,
                "guardrail_source":           r.guardrail_source,
                "guardrail_updated_at":       r.guardrail_updated_at or r.assigned_at,
                "guardrail_set_by":           r.guardrail_set_by or r.assigned_by,
                "guardrail_set_by_email":     r.guardrail_set_by_email,
                "active_deployments_count":   int(r.active_deployments_count or 0),
            }
            for r in rows
        ],
    }


# ── Guardrails ────────────────────────────────────────────────────────────────

@router.post("/courses/{content_id}/guardrails/{user_id}", status_code=200)
async def set_guardrails(
    body: GuardrailSetRequest,
    content_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """Set or update guardrails for a course_admin on a specific course."""
    # Verify assignment exists
    assignment = await pg.execute(
        text("""
            SELECT 1 FROM course_admin_assignments
            WHERE user_id = :user_id AND content_id = :content_id
        """),
        {"user_id": user_id, "content_id": content_id},
    )
    if not assignment.fetchone():
        raise HTTPException(
            status_code=404,
            detail="This user is not assigned as course_admin for this course.",
        )

    await pg.execute(
        text("""
            INSERT INTO course_guardrails
                (course_admin_id, content_id, max_concurrent_deployments,
                 max_duration_hours, set_by)
            VALUES
                (:course_admin_id, :content_id, :max_concurrent, :max_duration, :set_by)
            ON CONFLICT (course_admin_id, content_id) DO UPDATE SET
                max_concurrent_deployments = EXCLUDED.max_concurrent_deployments,
                max_duration_hours         = EXCLUDED.max_duration_hours,
                set_by                     = EXCLUDED.set_by,
                updated_at                 = now()
        """),
        {
            "course_admin_id": user_id,
            "content_id":      content_id,
            "max_concurrent":  body.max_concurrent_deployments,
            "max_duration":    body.max_duration_hours,
            "set_by":          str(admin.id),
        },
    )
    await pg.commit()

    log.info(
        "Guardrails set: content_id=%s course_admin=%s max_concurrent=%s max_duration=%s by=%s",
        content_id, user_id, body.max_concurrent_deployments,
        body.max_duration_hours, admin.id,
    )
    return {
        "content_id":                content_id,
        "course_admin_id":           user_id,
        "max_concurrent_deployments": body.max_concurrent_deployments,
        "max_duration_hours":        body.max_duration_hours,
    }


# ── User role management ──────────────────────────────────────────────────────

@router.post("/users/{user_id}/role", status_code=200)
async def set_user_role(
    body: RoleSetRequest,
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """
    Set any user's role directly.
    Useful for testing and for manual role corrections.
    sys_admin cannot demote themselves.
    """
    if str(admin.id) == user_id and body.role != ROLE_SYS_ADMIN:
        raise HTTPException(
            status_code=400,
            detail="sys_admin cannot demote themselves.",
        )

    result = await pg.execute(
        text("""
            UPDATE users SET role = :role, updated_at = now()
            WHERE id = :id
            RETURNING id, email, role
        """),
        {"role": body.role, "id": user_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    await pg.commit()
    log.info(
        "Role updated: user_id=%s new_role=%s by=%s",
        user_id, body.role, admin.id,
    )
    return {"user_id": row.id, "email": row.email, "role": row.role}