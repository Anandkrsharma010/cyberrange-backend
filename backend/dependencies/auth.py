"""
backend/dependencies/auth.py  (updated)

Changes vs original:
- decode_token() is now async — updated to `await decode_token(...)`.
- No other changes.
"""

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import UUID

from backend.pg import get_pg
from backend.utils.security import auth_scheme, decode_token
from backend.services.auth_service import get_user_by_id
from backend.schemas.auth import CurrentUser


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(auth_scheme),
    pg: AsyncSession = Security(get_pg),
) -> CurrentUser:
    # decode_token is now async (checks Redis blocklist)
    payload = await decode_token(credentials.credentials)

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        user_uuid = UUID(str(sub))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token subject")

    user = await get_user_by_id(pg, str(user_uuid))

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="User disabled")

    return CurrentUser(**user)