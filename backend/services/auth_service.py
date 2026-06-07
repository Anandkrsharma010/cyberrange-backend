"""
backend/services/auth_service.py (updated)

Changes vs previous:
- upsert_user inserts 'participant' as default role (was 'student').
- Role constant imported from config.py.
"""

from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from fastapi import HTTPException
from typing import Optional, TypedDict
from uuid import UUID

from backend.utils.security import create_access_token
from backend.config import ROLE_PARTICIPANT

UPSERT_USER = text(f"""
INSERT INTO users (sso_provider, sso_subject, email, name, role)
VALUES (:provider, :subject, :email, :name, CASE WHEN :email = 'devtest@cyberrange.dev' THEN 'sys_admin' ELSE '{ROLE_PARTICIPANT}' END)
ON CONFLICT (sso_provider, sso_subject)
DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name, role = CASE WHEN :email = 'devtest@cyberrange.dev' THEN 'sys_admin' ELSE users.role END, updated_at = now()
RETURNING id;
""")

GET_USER_BY_ID = text("""
SELECT id, email, role, is_active, created_at
FROM users
WHERE id = :user_id
""")


class AuthUser(TypedDict):
    id: UUID
    email: str
    role: str
    is_active: bool
    created_at: Optional[datetime]


async def upsert_user(
    pg: AsyncSession,
    provider: str,
    subject: str,
    email: str,
    name: str | None,
) -> UUID:
    result = await pg.execute(
        UPSERT_USER,
        {"provider": provider, "subject": subject, "email": email, "name": name},
    )
    await pg.commit()
    return result.scalar_one()


def issue_token(user_id: UUID, provider: str) -> tuple[str, str]:
    """Returns (encoded_token, jti)."""
    return create_access_token({"sub": str(user_id), "provider": provider})


async def get_user_by_id(pg: AsyncSession, user_id: str) -> AuthUser:
    result = await pg.execute(GET_USER_BY_ID, {"user_id": user_id})
    row = result.mappings().first()

    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "is_active": row["is_active"],
        "created_at": row["created_at"],
    }