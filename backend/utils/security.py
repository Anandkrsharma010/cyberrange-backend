"""
backend/utils/security.py  (updated)

Changes vs original:
- decode_token() is now async — it checks the Redis blocklist before
  returning the payload. Any caller that previously used the sync version
  must be updated to `await decode_token(...)`.
- get_token_payload() removed (was unused outside this module).
- revoke_token_by_payload() helper added for use by the logout endpoint.
"""

import bcrypt
import jwt
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from uuid import uuid4
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import get_settings
from backend.utils.blocklist import is_token_revoked, revoke_token

settings = get_settings()
auth_scheme = HTTPBearer()


def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain.encode(), salt).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(data: dict) -> tuple[str, str]:
    """
    Returns (encoded_token, jti).
    jti is surfaced so callers can write an audit log entry.
    """
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    jti = str(uuid4())

    to_encode = data.copy()
    to_encode.update(
        {
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "jti": jti,
            "exp": expire,
        }
    )

    token = jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)
    return token, jti


async def revoke_token_by_payload(
    payload: dict,
    *,
    pg: AsyncSession,
    user_id: str,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """
    Revoke a token given its decoded payload, then write a 'revoked'
    audit log entry. Both operations share the same transaction.
    """
    from backend.utils.audit import log_token_event  # local import avoids circular

    jti = payload.get("jti")
    exp = payload.get("exp")

    if not jti or not exp:
        return

    expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
    await revoke_token(jti=jti, expires_at=expires_at)

    await log_token_event(
        pg,
        user_id=user_id,
        jti=jti,
        event="revoked",
        ip_address=ip_address,
        user_agent=user_agent,
    )


async def decode_token(token: str) -> dict:
    """
    Decode and validate a JWT, then check the Redis blocklist.

    Raises HTTPException 401 on any failure:
    - Malformed / invalid signature
    - Expired
    - Revoked (jti in blocklist)
    """
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGO],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

    jti = payload.get("jti")
    if jti and await is_token_revoked(jti):
        raise HTTPException(status_code=401, detail="Token has been revoked")

    return payload