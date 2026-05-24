"""Course admin role vs active operator duties."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import ROLE_COURSE_ADMIN, ROLE_PARTICIPANT

log = logging.getLogger("course_admin_role")


async def user_has_course_admin_duties(pg: AsyncSession, user_id: str) -> bool:
    """
    Source of truth for active course-admin operator duties.

    Course-admin operator scope is cohort-based: a user has duties while they still
    own at least one row in `workshop_course_admins`.
    """
    r = await pg.execute(
        text(
            "SELECT 1 FROM workshop_course_admins WHERE user_id = CAST(:u AS uuid) LIMIT 1"
        ),
        {"u": user_id},
    )
    return r.fetchone() is not None


async def maybe_demote_course_admin_to_participant(
    pg: AsyncSession,
    user_id: str,
    *,
    demoted_by: str | None = None,
) -> bool:
    """
    If role is course_admin and there are no remaining cohort operator duties,
    set role to participant. Does not commit.
    Returns True if demotion was applied.
    """
    ur = await pg.execute(
        text("SELECT role FROM users WHERE id = CAST(:id AS uuid)"),
        {"id": user_id},
    )
    row = ur.mappings().first()
    if not row or row["role"] != ROLE_COURSE_ADMIN:
        return False
    if await user_has_course_admin_duties(pg, user_id):
        return False
    await pg.execute(
        text("UPDATE users SET role = :role, updated_at = now() WHERE id = CAST(:id AS uuid)"),
        {"role": ROLE_PARTICIPANT, "id": user_id},
    )
    log.info(
        "User demoted to participant (no remaining course/workshop admin duties): "
        "user_id=%s demoted_by=%s",
        user_id,
        demoted_by,
    )
    return True
