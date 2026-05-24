"""
backend/dependencies/authz.py (updated)

Changes vs previous:
- AdminOnly → SysAdminOnly
- InstructorOrAbove → CourseAdminOrAbove
- Role strings → constants from config.py
- AdminOnly kept as alias for backwards compatibility during rollout.
"""

from __future__ import annotations
from fastapi import Depends, HTTPException
from backend.dependencies.auth import get_current_user
from backend.schemas.auth import CurrentUser
from backend.config import ROLE_SYS_ADMIN, ROLE_COURSE_ADMIN, ROLE_PARTICIPANT


def require_role(*allowed_roles: str):
    """
    Returns a FastAPI dependency that enforces role-based access.
    sys_admin always passes regardless of allowed_roles.
    """
    role_set = set(allowed_roles)

    async def _check(
        current_user: CurrentUser = Depends(get_current_user),
    ) -> CurrentUser:
        if current_user.role == ROLE_SYS_ADMIN:
            return current_user
        if current_user.role not in role_set:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required role: {', '.join(sorted(role_set))}.",
            )
        return current_user

    return _check


def SysAdminOnly(
    current_user: CurrentUser = Depends(require_role(ROLE_SYS_ADMIN)),
) -> CurrentUser:
    """Dependency: sys_admin only."""
    return current_user


def CourseAdminOrAbove(
    current_user: CurrentUser = Depends(
        require_role(ROLE_SYS_ADMIN, ROLE_COURSE_ADMIN)
    ),
) -> CurrentUser:
    """Dependency: course_admin or sys_admin."""
    return current_user


def AnyAuthenticatedUser(
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    """Dependency: any authenticated user."""
    return current_user


# Backwards-compatibility alias — remove once all call sites confirmed updated
AdminOnly = SysAdminOnly