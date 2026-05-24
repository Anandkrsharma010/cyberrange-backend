from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.pg import get_pg
from backend.infrastructure.headscale import mint_preauth_key
from backend.dependencies.auth import get_current_user, CurrentUser
from backend.config import get_settings
from backend.limiter import limiter

router = APIRouter(prefix="/tailnet", tags=["tailnet"])
settings = get_settings()

JOIN_KEY_TTL_MINUTES: int = 15


@router.post("/join-token")
@limiter.limit(settings.RATE_LIMIT_TAILNET)
async def create_device_join_token(
    request: Request,
    response: Response,
    pg: AsyncSession = Depends(get_pg),
    current_user: CurrentUser = Depends(get_current_user),
):
    """
    Mint a short-lived Headscale preauth key for the current user's device.
    Does not accept user_id as input — prevents minting keys for other users.
    """
    user_id = str(current_user.id)

    res = await pg.execute(
        text("SELECT is_active FROM users WHERE id = :uid"),
        {"uid": user_id},
    )
    row = res.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    if row.is_active is False:
        raise HTTPException(status_code=403, detail="User is inactive")

    ent = await pg.execute(
        text("""
            SELECT 1
            FROM entitlements
            WHERE user_id = :uid
              AND status = 'active'
              AND (valid_until IS NULL OR valid_until > now())
            LIMIT 1
        """),
        {"uid": user_id},
    )
    if not ent.fetchone():
        raise HTTPException(status_code=403, detail="No active entitlement")

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=JOIN_KEY_TTL_MINUTES)

    try:
        authkey = await mint_preauth_key(
            pg=pg,
            user_id=user_id,
            key_type="device",
            expires_at=expires_at,
            acl_tags=[],
            reusable=False,
            ephemeral=True,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="VPN join service is currently unavailable. Please try again shortly.",
        ) from exc

    # Use a tailscale-compatible login server origin.
    login_server = settings.resolved_headscale_login_server()

    command = (
        f"sudo tailscale up "
        f"--login-server={login_server} "
        f"--authkey={authkey}"
    )

    return {
        "login_server": login_server,
        "authkey": authkey,
        "expires_at": expires_at.isoformat(),
        "command": command,
        "ttl_minutes": JOIN_KEY_TTL_MINUTES,
    }