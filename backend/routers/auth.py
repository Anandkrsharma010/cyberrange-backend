"""
backend/routers/auth.py (updated)

Changes vs previous:
- sso_callback: issue_token now returns (token, jti); audit 'issued' event written.
- logout: revoke_token_by_payload now takes pg + user context for audit trail.
"""

import logging

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, Path
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import Security
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from backend.pg import get_pg
from backend.services.auth_service import upsert_user, issue_token
from backend.dependencies.auth import get_current_user
from backend.dependencies.authz import SysAdminOnly
from backend.schemas.auth import SSOCallbackRequest, TokenResponse, CurrentUser
from backend.schemas.workshop_invite import WorkshopInviteRedeemBody
from backend.services.workshop_invites import redeem_invite
from backend.routers.workshops import _log_workshop_activity
from backend.config import get_settings, ROLE_SYS_ADMIN
from backend.limiter import limiter
from backend.utils.security import decode_token, revoke_token_by_payload
from backend.utils.audit import log_token_event

log = logging.getLogger("auth")
router = APIRouter(prefix="/auth", tags=["Authentication"])
settings = get_settings()

_google_request = google_requests.Request()
_bearer = HTTPBearer()


def _verify_google_token(raw_token: str) -> dict:
    try:
        payload = google_id_token.verify_oauth2_token(
            raw_token,
            _google_request,
            settings.GOOGLE_CLIENT_ID,
        )
    except ValueError as e:
        log.warning("Google token verification failed: %s", e)
        raise HTTPException(status_code=401, detail="Invalid or expired SSO token")

    if payload.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        log.warning("Google token has unexpected issuer: %s", payload.get("iss"))
        raise HTTPException(status_code=401, detail="Invalid SSO token issuer")

    return payload


@router.post("/sso/callback", response_model=TokenResponse)
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def sso_callback(
    request: Request,
    response: Response,
    payload: SSOCallbackRequest,
    pg: AsyncSession = Depends(get_pg),
):
    provider = payload.provider.strip().lower()
    if provider not in [p.lower() for p in settings.ALLOWED_SSO_PROVIDERS]:
        raise HTTPException(status_code=400, detail="Unsupported SSO provider")

    if provider == "google":
        verified = _verify_google_token(payload.id_token)
        subject = verified["sub"]
        email   = verified["email"].strip().lower()
        name    = verified.get("name") or verified.get("given_name")

        if not verified.get("email_verified", False):
            raise HTTPException(
                status_code=403,
                detail="Google account email is not verified",
            )
    else:
        raise HTTPException(status_code=400, detail="Unsupported SSO provider")

    user_id = await upsert_user(pg, provider, subject, email, name)
    # upsert_user commits internally — open a fresh implicit transaction
    # for the audit write below.
    token, jti = issue_token(user_id, provider)

    await log_token_event(
        pg,
        user_id=str(user_id),
        jti=jti,
        event="issued",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await pg.commit()

    log.info("SSO login: provider=%s email=%s user_id=%s", provider, email, user_id)
    return TokenResponse(access_token=token)

# ── DEV-ONLY login endpoints (ENABLE_DOCS gate) ─────────────────────
# TODO(production): Remove this entire block before production deploy.
# These endpoints exist solely for local testing with two test accounts:
#   - /auth/dev-login           → sys_admin  (devtest@cyberrange.dev)
#   - /auth/dev-login-participant → participant (participant@cyberrange.dev)

if settings.ENABLE_DOCS:
    @router.post("/dev-login", response_model=TokenResponse)
    async def dev_login(
        request: Request,
        pg: AsyncSession = Depends(get_pg),
    ):
        """DEV ONLY — upsert the admin test user and return a JWT."""
        email = "devtest@cyberrange.dev"
        user_id = await upsert_user(pg, "dev", "dev-local-user", email, "Dev Tester")
        token, jti = issue_token(user_id, "dev")
        await log_token_event(
            pg, user_id=str(user_id), jti=jti, event="issued",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await pg.commit()
        log.info("Dev login: email=%s user_id=%s", email, user_id)
        return TokenResponse(access_token=token)

    @router.post("/dev-login-participant", response_model=TokenResponse)
    async def dev_login_participant(
        request: Request,
        email: str = Body("participant@cyberrange.dev", embed=True),
        pg: AsyncSession = Depends(get_pg),
    ):
        """DEV ONLY — upsert a participant test user with any email."""
        sso_subject = f"dev-{email}"
        name = email.split("@")[0].replace(".", " ").title()
        user_id = await upsert_user(pg, "dev", sso_subject, email, name)
        token, jti = issue_token(user_id, "dev")
        await log_token_event(
            pg, user_id=str(user_id), jti=jti, event="issued",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await pg.commit()
        log.info("Dev login (participant): email=%s user_id=%s", email, user_id)
        return TokenResponse(access_token=token)


@router.get("/me", response_model=CurrentUser)
async def me(user: CurrentUser = Depends(get_current_user)):
    """Return the currently authenticated user including their role."""
    return user


@router.post("/workshop-invite/redeem")
@limiter.limit(settings.RATE_LIMIT_AUTH)
async def redeem_workshop_invite(
    request: Request,
    body: WorkshopInviteRedeemBody,
    pg: AsyncSession = Depends(get_pg),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Bind a pending workshop invite to the current user (email must match invite).
    Grants workshop-scoped entitlement and marks the invite accepted (single use).
    """
    out = await redeem_invite(
        pg,
        user_id=str(user.id),
        user_email=str(user.email),
        raw_token=body.token.strip(),
    )
    await _log_workshop_activity(
        pg,
        str(user.id),
        out["workshop_id"],
        "workshop.invite_redeemed",
        {},
    )
    await pg.commit()
    log.info(
        "workshop invite redeemed: user_id=%s workshop_id=%s",
        user.id,
        out.get("workshop_id"),
    )
    return out


@router.post("/admin/users/{user_id}/disable", status_code=200)
async def disable_user(
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. Marks a user account as inactive."""
    if str(admin.id) == user_id:
        raise HTTPException(
            status_code=400,
            detail="sys_admin cannot disable their own account.",
        )

    result = await pg.execute(
        text("""
            UPDATE users SET is_active = false, updated_at = now()
            WHERE id = :uid RETURNING id
        """),
        {"uid": user_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    await pg.commit()
    log.info("User disabled: user_id=%s by sys_admin=%s", user_id, admin.id)
    return {"user_id": user_id, "is_active": False}


@router.post("/admin/users/{user_id}/enable", status_code=200)
async def enable_user(
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. Marks a user account as active."""
    result = await pg.execute(
        text("""
            UPDATE users SET is_active = true, updated_at = now()
            WHERE id = :uid RETURNING id
        """),
        {"uid": user_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    await pg.commit()
    log.info("User enabled: user_id=%s by sys_admin=%s", user_id, admin.id)
    return {"user_id": user_id, "is_active": True}


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
    pg: AsyncSession = Depends(get_pg),
):
    """Revoke the current JWT (Redis blocklist) and write a revoked audit entry."""
    payload = await decode_token(credentials.credentials)

    await revoke_token_by_payload(
        payload,
        pg=pg,
        user_id=payload.get("sub", "unknown"),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await pg.commit()

    log.info(
        "User logged out: sub=%s jti=%s",
        payload.get("sub"), payload.get("jti"),
    )

@router.get("/admin/users")
async def list_users(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. List all user accounts."""
    result = await pg.execute(
        text("""
            SELECT id, email, role, is_active, created_at
            FROM users
            ORDER BY created_at ASC
        """)
    )
    rows = result.fetchall()
    return {
        "count": len(rows),
        "users": [
            {
                "user_id":    r.id,
                "email":      r.email,
                "role":       r.role,
                "is_active":  r.is_active,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.get("/admin/users/overview")
async def admin_users_overview(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """
    sys_admin only. Per-user billing counters (purchases, payment pending, entitlements).
    Used by the admin dashboard together with GET /auth/admin/users and GET /labs/admin/all.
    """
    result = await pg.execute(
        text("""
            SELECT
                u.id::text AS user_id,
                COALESCE(pu.n, 0) AS purchase_count,
                COALESCE(pp.n, 0) AS pending_payment_count,
                COALESCE(ea.n, 0) AS entitlement_active,
                COALESCE(ex.n, 0) AS entitlement_expired,
                COALESCE(er.n, 0) AS entitlement_revoked
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n FROM purchases GROUP BY user_id
            ) pu ON pu.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM payments WHERE status = 'pending' GROUP BY user_id
            ) pp ON pp.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM entitlements WHERE status = 'active' GROUP BY user_id
            ) ea ON ea.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM entitlements WHERE status = 'expired' GROUP BY user_id
            ) ex ON ex.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM entitlements WHERE status = 'revoked' GROUP BY user_id
            ) er ON er.user_id = u.id
            ORDER BY u.created_at ASC
        """)
    )
    rows = result.fetchall()
    return {
        "count": len(rows),
        "rows": [
            {
                "user_id":               r.user_id,
                "purchase_count":        int(r.purchase_count),
                "pending_payment_count": int(r.pending_payment_count),
                "entitlement_active":    int(r.entitlement_active),
                "entitlement_expired":   int(r.entitlement_expired),
                "entitlement_revoked":   int(r.entitlement_revoked),
            }
            for r in rows
        ],
    }


@router.get("/admin/users/ops-summary")
async def admin_users_ops_summary(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """
    sys_admin only. Single aggregated source for User Detail screen.
    Combines identity, billing counters, entitlement counters, and deployment
    health metrics to avoid multiple client round trips and heavy pagination.
    """
    result = await pg.execute(
        text(
            """
            SELECT
                u.id::text AS user_id,
                u.email,
                u.role,
                u.is_active,
                u.created_at,
                COALESCE(pu.n, 0) AS purchase_count,
                COALESCE(pp.n, 0) AS pending_payment_count,
                COALESCE(ea.n, 0) AS entitlement_active,
                COALESCE(ex.n, 0) AS entitlement_expired,
                COALESCE(er.n, 0) AS entitlement_revoked,
                COALESCE(ld.attempts_30d, 0) AS attempts_30d,
                COALESCE(ld.failed_30d, 0) AS failed_30d,
                COALESCE(ld.live_now, 0) AS live_now,
                COALESCE(ld.has_failed_any, false) AS has_failed_any
            FROM users u
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM purchases
                GROUP BY user_id
            ) pu ON pu.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM payments
                WHERE status = 'pending'
                GROUP BY user_id
            ) pp ON pp.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM entitlements
                WHERE status = 'active'
                GROUP BY user_id
            ) ea ON ea.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM entitlements
                WHERE status = 'expired'
                GROUP BY user_id
            ) ex ON ex.user_id = u.id
            LEFT JOIN (
                SELECT user_id, COUNT(*)::int AS n
                FROM entitlements
                WHERE status = 'revoked'
                GROUP BY user_id
            ) er ON er.user_id = u.id
            LEFT JOIN (
                SELECT
                    user_id,
                    COUNT(*) FILTER (
                        WHERE created_at >= now() - interval '30 day'
                    )::int AS attempts_30d,
                    COUNT(*) FILTER (
                        WHERE created_at >= now() - interval '30 day'
                          AND status IN ('failed', 'cleanup_failed')
                    )::int AS failed_30d,
                    COUNT(*) FILTER (
                        WHERE status = 'running'
                    )::int AS live_now,
                    BOOL_OR(status IN ('failed', 'cleanup_failed')) AS has_failed_any
                FROM lab_deployments
                GROUP BY user_id
            ) ld ON ld.user_id = u.id
            ORDER BY u.created_at ASC
            """
        )
    )
    rows = result.fetchall()
    return {
        "count": len(rows),
        "rows": [
            {
                "user_id": str(r.user_id),
                "email": r.email,
                "role": r.role,
                "is_active": bool(r.is_active),
                "created_at": r.created_at,
                "purchase_count": int(r.purchase_count),
                "pending_payment_count": int(r.pending_payment_count),
                "entitlement_active": int(r.entitlement_active),
                "entitlement_expired": int(r.entitlement_expired),
                "entitlement_revoked": int(r.entitlement_revoked),
                "attempts30d": int(r.attempts_30d),
                "failed30d": int(r.failed_30d),
                "live_now": int(r.live_now),
                "has_failed_any": bool(r.has_failed_any),
            }
            for r in rows
        ],
    }