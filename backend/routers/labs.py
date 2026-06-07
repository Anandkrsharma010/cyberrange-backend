"""
backend/routers/labs.py (updated)

Changes vs previous:
- AdminOnly → SysAdminOnly throughout.
- Log messages updated: 'admin' → 'sys_admin', 'Member' → 'Participant'.
- All logic unchanged — course_admin scoping comes in Phase 2.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.dependencies.auth import get_current_user
from backend.dependencies.authz import SysAdminOnly, AnyAuthenticatedUser
from backend.schemas.auth import CurrentUser
from backend.schemas.labs import LabDeployRequest, AdminDeployForUserRequest
from backend.pg import get_pg
from backend.config import get_settings
from backend.limiter import limiter
from backend.infrastructure.headscale import mint_preauth_key
from backend.services.ops_feed import emit_ops_event
from backend.utils.audit import log_token_event

log = logging.getLogger("labs")
router = APIRouter(prefix="/labs", tags=["Labs"])
settings = get_settings()

_ACTIVE_STATUSES = {"running"}
_ERROR_MESSAGE = (
    "Deployment failed. Contact support with your deployment ID for details."
)
JOIN_KEY_TTL_MINUTES = 15


def _normalize_expires_at(raw_expires_at: datetime) -> datetime:
    if raw_expires_at.tzinfo is None:
        return raw_expires_at.replace(tzinfo=timezone.utc)
    return raw_expires_at


async def _resolve_lab_type(pg: AsyncSession, content_id: str):
    result = await pg.execute(
        text(
            """
            SELECT id, metadata
            FROM content_items
            WHERE id = :content_id AND type = 'lab' AND is_active = true
            """
        ),
        {"content_id": content_id},
    )
    lab = result.fetchone()
    if not lab:
        raise HTTPException(status_code=404, detail="Lab not found")

    lab_type = (lab.metadata or {}).get("lab_type")
    if not lab_type:
        raise HTTPException(
            status_code=500,
            detail="Lab is misconfigured: missing lab_type in metadata.",
        )
    return lab_type


def _extract_instances(terraform_outputs: Any) -> dict[str, Any]:
    if not isinstance(terraform_outputs, dict):
        return {}
    summary = terraform_outputs.get("lab_summary") or {}
    value = summary.get("value") if isinstance(summary, dict) else {}
    nested_instances = value.get("instances") if isinstance(value, dict) else {}
    if isinstance(nested_instances, dict) and nested_instances:
        return nested_instances
    direct_instances = terraform_outputs.get("instances")
    if isinstance(direct_instances, dict):
        return direct_instances
    return {}


def _build_access_details_payload(
    *,
    deployment_id: str,
    lab_type: str | None,
    status: str,
    is_owner: bool,
    terraform_outputs: Any,
    expires_at: datetime | None,
) -> dict[str, Any]:
    normalized_lab_type = (lab_type or "").lower()
    instances = _extract_instances(terraform_outputs)
    machines: list[dict[str, Any]] = []
    instructions: list[str] = []
    access_model = "restricted"

    if normalized_lab_type == "windows":
        role_specs = [
            ("domain_controller", "Domain Controller", "RDP", 3389),
            ("domain_client", "Domain Client", "RDP", 3389),
            ("wazuh_manager", "Wazuh Manager", "HTTPS", 443),
            ("kali_machine", "Kali Machine", "SSH", 22),
            ("subnet_router", "Subnet Router", "Internal Router", None),
        ]
        for role_key, label, protocol, port in role_specs:
            inst = instances.get(role_key) if isinstance(instances, dict) else None
            if not isinstance(inst, dict):
                continue
            private_ip = inst.get("private_ip")
            public_ip = inst.get("public_ip")
            host = private_ip or public_ip
            machines.append(
                {
                    "role": role_key,
                    "label": label,
                    "protocol": protocol,
                    "port": port,
                    "host": host,
                    "private_ip": private_ip,
                    "public_ip": public_ip if is_owner else None,
                    "credential_label": "Use the credentials provided in your lab resources.",
                }
            )
        access_model = "tailscale"
        instructions = [
            "Open VPN Join section and run your Tailscale join command first.",
            "After VPN is connected, use machine private IPs shown below.",
            "Use credentials from your lab resources or instructor-provided details.",
        ]
    elif normalized_lab_type == "aws":
        role_specs = [
            ("attacker", "Kali Attacker Machine", "SSH", 22),
            ("target", "Linux Target Instance", "SSH", 22),
        ]
        for role_key, label, protocol, port in role_specs:
            inst = instances.get(role_key) if isinstance(instances, dict) else None
            if not isinstance(inst, dict):
                continue
            private_ip = inst.get("private_ip")
            public_ip = inst.get("public_ip")
            host = private_ip or public_ip
            machines.append(
                {
                    "role": role_key,
                    "label": label,
                    "protocol": protocol,
                    "port": port,
                    "host": host,
                    "private_ip": private_ip,
                    "public_ip": public_ip if is_owner else None,
                    "credential_label": "Use the credentials provided in your lab resources.",
                }
            )
        access_model = "tailscale"
        instructions = [
            "Open VPN Join section and run your Tailscale join command first.",
            "After VPN is connected, use machine private IPs shown below.",
            "Use credentials from your lab resources.",
        ]
    else:
        instructions = [
            "Access details are available once the deployment is running.",
        ]

    return {
        "deployment_id": deployment_id,
        "lab_type": lab_type,
        "status": status,
        "is_owner": is_owner,
        "access_model": access_model,
        "expires_at": expires_at,
        "instructions": instructions,
        "machines": machines,
    }


async def _ensure_no_active_deployment(pg: AsyncSession, user_id: str, content_id: str):
    existing = await pg.execute(
        text(
            """
            SELECT 1 FROM lab_deployments
            WHERE user_id = :user_id AND content_id = :content_id
              AND status IN ('queued', 'provisioning', 'running')
            LIMIT 1
            """
        ),
        {"user_id": user_id, "content_id": content_id},
    )
    if existing.fetchone():
        raise HTTPException(
            status_code=400,
            detail="An active deployment already exists for this lab.",
        )


async def _queue_deployment(
    pg: AsyncSession,
    *,
    owner_user_id: str,
    content_id: str,
    expires_at: datetime,
):
    deployment_id = uuid4()
    workspace = f"ws-{deployment_id}"

    await pg.execute(
        text(
            """
            INSERT INTO lab_deployments (
                id, user_id, content_id, lab_type,
                status, terraform_workspace, expires_at
            ) VALUES (
                :id, :user_id, :content_id, :lab_type,
                'queued', :workspace, :expires_at
            )
            """
        ),
        {
            "id": deployment_id,
            "user_id": owner_user_id,
            "content_id": content_id,
            "lab_type": await _resolve_lab_type(pg, content_id),
            "workspace": workspace,
            "expires_at": expires_at,
        },
    )
    return deployment_id


@router.post("/deploy")
@limiter.limit(settings.RATE_LIMIT_DEPLOY)
async def deploy_lab(
    request: Request,
    response: Response,
    body: LabDeployRequest,
    current_user: CurrentUser = Depends(SysAdminOnly),
    pg: AsyncSession = Depends(get_pg),
):
    """sys_admin only. Queue a lab deployment with explicit expires_at."""
    expires_at = _normalize_expires_at(body.expires_at)

    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="expires_at must be in the future.")

    await _ensure_no_active_deployment(
        pg,
        user_id=str(current_user.id),
        content_id=str(body.content_id),
    )
    deployment_id = await _queue_deployment(
        pg,
        owner_user_id=str(current_user.id),
        content_id=str(body.content_id),
        expires_at=expires_at,
    )
    did = str(deployment_id)
    await emit_ops_event(
        pg,
        event_key=f"sysadmin-lab-queued-self:{did}",
        event_type="lab.sysadmin_deploy_self",
        severity="info",
        title="Sys-admin lab deploy queued",
        message="A lab deployment was queued from the sys-admin labs API (self).",
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="content",
        subject_id=str(body.content_id),
        deployment_id=did,
        target_user_id=str(current_user.id),
        deep_link=f"/admin/ops/individual/deployment/{did}",
        emitter="labs.router",
        metadata={"content_id": str(body.content_id)},
    )
    await pg.commit()

    log.info(
        "Lab queued: deployment_id=%s sys_admin_id=%s owner_user_id=%s expires_at=%s",
        deployment_id,
        current_user.id,
        current_user.id,
        expires_at,
    )
    return {"deployment_id": did, "status": "queued", "expires_at": expires_at}


@router.post("/admin/deploy-for-user")
@limiter.limit(settings.RATE_LIMIT_DEPLOY)
async def deploy_lab_for_user(
    request: Request,
    response: Response,
    body: AdminDeployForUserRequest,
    current_user: CurrentUser = Depends(SysAdminOnly),
    pg: AsyncSession = Depends(get_pg),
):
    """
    sys_admin only. Queue a deployment for a specific user (beneficiary owner).
    Requires an active entitlement for the selected content_id.
    """
    expires_at = _normalize_expires_at(body.expires_at)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="expires_at must be in the future.")

    target_user = await pg.execute(
        text(
            """
            SELECT id, email, is_active
            FROM users
            WHERE id = :user_id
            """
        ),
        {"user_id": str(body.target_user_id)},
    )
    target_user_row = target_user.fetchone()
    if not target_user_row:
        raise HTTPException(status_code=404, detail="Target user not found")
    if not target_user_row.is_active:
        raise HTTPException(status_code=400, detail="Target user is inactive")

    entitlement = await pg.execute(
        text(
            """
            SELECT 1
            FROM entitlements
            WHERE user_id = :user_id
              AND content_id = :content_id
              AND status = 'active'
            LIMIT 1
            """
        ),
        {
            "user_id": str(body.target_user_id),
            "content_id": str(body.content_id),
        },
    )
    if not entitlement.fetchone():
        raise HTTPException(
            status_code=400,
            detail="Target user has no active entitlement for this lab.",
        )

    await _ensure_no_active_deployment(
        pg,
        user_id=str(body.target_user_id),
        content_id=str(body.content_id),
    )

    deployment_id = await _queue_deployment(
        pg,
        owner_user_id=str(body.target_user_id),
        content_id=str(body.content_id),
        expires_at=expires_at,
    )
    did = str(deployment_id)
    await emit_ops_event(
        pg,
        event_key=f"sysadmin-lab-queued-for-user:{did}",
        event_type="lab.sysadmin_deploy_for_user",
        severity="info",
        title="Sys-admin lab deploy queued for user",
        message=f"Sys admin queued a lab for user {target_user_row.email}.",
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="content",
        subject_id=str(body.content_id),
        deployment_id=did,
        target_user_id=str(body.target_user_id),
        deep_link=f"/admin/ops/individual/deployment/{did}",
        emitter="labs.router",
        metadata={
            "content_id": str(body.content_id),
            "target_user_email": target_user_row.email,
        },
    )
    await pg.commit()

    log.info(
        "Lab queued for target user: deployment_id=%s sys_admin_id=%s owner_user_id=%s owner_email=%s expires_at=%s",
        deployment_id,
        current_user.id,
        body.target_user_id,
        target_user_row.email,
        expires_at,
    )
    return {
        "deployment_id": did,
        "status": "queued",
        "expires_at": expires_at,
        "target_user_id": str(body.target_user_id),
    }


@router.get("/status")
async def list_labs(
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    """
    Returns deployments visible to the current user.
    Owners see full details including IPs.
    Participants see status and join availability only.
    """
    own_result = await pg.execute(
        text("""
            SELECT ld.id, ld.status, ld.instance_public_ip, ld.instance_private_ip,
                   ld.error_message, ld.created_at, ld.expires_at, ci.title,
                   true AS is_owner
            FROM lab_deployments ld
            JOIN content_items ci ON ld.content_id = ci.id
            WHERE ld.user_id = :uid
            ORDER BY ld.created_at DESC
        """),
        {"uid": current_user.id},
    )
    own_rows = own_result.fetchall()

    member_result = await pg.execute(
        text("""
            SELECT ld.id, ld.status, ld.error_message, ld.created_at,
                   ld.expires_at, ci.title, false AS is_owner
            FROM deployment_members dm
            JOIN lab_deployments ld ON dm.deployment_id = ld.id
            JOIN content_items ci ON ld.content_id = ci.id
            WHERE dm.user_id = :uid
            ORDER BY ld.created_at DESC
        """),
        {"uid": current_user.id},
    )
    member_rows = member_result.fetchall()

    deployments = []

    for r in own_rows:
        deployments.append({
            "deployment_id": r.id,
            "status": r.status,
            "is_owner": True,
            "public_ip":  r.instance_public_ip  if r.status in _ACTIVE_STATUSES else None,
            "private_ip": r.instance_private_ip if r.status in _ACTIVE_STATUSES else None,
            "error": _ERROR_MESSAGE if r.error_message else None,
            "lab_title": r.title,
            "created_at": r.created_at,
            "expires_at": r.expires_at,
            "can_join": False,
        })

    for r in member_rows:
        if any(d["deployment_id"] == r.id for d in deployments):
            continue
        deployments.append({
            "deployment_id": r.id,
            "status": r.status,
            "is_owner": False,
            "public_ip": None,
            "private_ip": None,
            "error": _ERROR_MESSAGE if r.error_message else None,
            "lab_title": r.title,
            "created_at": r.created_at,
            "expires_at": r.expires_at,
            "can_join": r.status in _ACTIVE_STATUSES,
        })

    return {"count": len(deployments), "deployments": deployments}


@router.get("/resources/{content_id}")
async def list_visible_course_resources_for_user(
    content_id: str = Path(...),
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    """
    Returns visible course resources for a content item only when the current user
    has an active entitlement to that content. This is the participant-safe read path
    for dashboard/course resource rendering.
    """
    entitlement = await pg.execute(
        text(
            """
            SELECT 1
            FROM entitlements
            WHERE user_id = :user_id
              AND content_id = :content_id
              AND status = 'active'
            LIMIT 1
            """
        ),
        {"user_id": str(current_user.id), "content_id": content_id},
    )
    if not entitlement.fetchone():
        raise HTTPException(status_code=403, detail="No active entitlement for this content")

    rows_result = await pg.execute(
        text(
            """
            SELECT id, title, description, resource_type, url, file_key, mime_type,
                   position, metadata, created_at, updated_at
            FROM course_resources
            WHERE content_id = :content_id
              AND is_visible = true
            ORDER BY position ASC, created_at ASC
            """
        ),
        {"content_id": content_id},
    )
    rows = rows_result.fetchall()
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
                "metadata": r.metadata,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ],
    }


@router.post("/join/{deployment_id}")
@limiter.limit(settings.RATE_LIMIT_TAILNET)
async def join_deployment(
    request: Request,
    response: Response,
    deployment_id: str = Path(...),
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    """Mint a short-lived Headscale device key for a deployment the user is a member of."""
    result = await pg.execute(
        text("SELECT id, user_id, status, expires_at FROM lab_deployments WHERE id = :id"),
        {"id": deployment_id},
    )
    deployment = result.fetchone()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if deployment.status != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Lab is not ready yet (status: {deployment.status}). Try again shortly.",
        )

    is_owner = str(deployment.user_id) == str(current_user.id)

    if not is_owner:
        member_check = await pg.execute(
            text("""
                SELECT 1 FROM deployment_members
                WHERE deployment_id = :deployment_id AND user_id = :user_id
            """),
            {"deployment_id": deployment_id, "user_id": current_user.id},
        )
        if not member_check.fetchone():
            raise HTTPException(
                status_code=403,
                detail="You are not a participant of this deployment.",
            )

    if not current_user.is_active:
        raise HTTPException(status_code=403, detail="User is inactive")

    dep_expires = deployment.expires_at
    if dep_expires.tzinfo is None:
        dep_expires = dep_expires.replace(tzinfo=timezone.utc)

    expires_at = min(
        datetime.now(timezone.utc) + timedelta(minutes=JOIN_KEY_TTL_MINUTES),
        dep_expires,
    )

    try:
        authkey = await mint_preauth_key(
            pg=pg,
            user_id=str(current_user.id),
            key_type="device",
            expires_at=expires_at,
            acl_tags=[],
            reusable=False,
            ephemeral=True,
        )
    except Exception as exc:
        log.exception(
            "Failed to mint Headscale join key: deployment_id=%s user_id=%s",
            deployment_id,
            current_user.id,
        )
        raise HTTPException(
            status_code=503,
            detail="VPN join service is currently unavailable. Please try again shortly.",
        ) from exc

    await log_token_event(
        pg,
        user_id=str(current_user.id),
        jti=authkey,
        event="join_key_issued",
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await pg.commit()

    login_server = settings.resolved_headscale_login_server()
    command = (
        f"sudo tailscale up "
        f"--login-server={login_server} "
        f"--authkey={authkey} "
        f"--accept-routes=true"
    )

    log.info(
        "Join token minted: deployment_id=%s user_id=%s is_owner=%s",
        deployment_id, current_user.id, is_owner,
    )

    return {
        "deployment_id": deployment_id,
        "login_server": login_server,
        "authkey": authkey,
        "expires_at": expires_at.isoformat(),
        "command": command,
        "ttl_minutes": JOIN_KEY_TTL_MINUTES,
    }


@router.get("/access-details/{deployment_id}")
async def get_access_details(
    deployment_id: str = Path(...),
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    """
    Returns participant-safe access details for a deployment.
    Uses one stable endpoint for all lab types and formats payload by lab_type.
    """
    result = await pg.execute(
        text(
            """
            SELECT id, user_id, lab_type, status, terraform_outputs, expires_at
            FROM lab_deployments
            WHERE id = :id
            LIMIT 1
            """
        ),
        {"id": deployment_id},
    )
    deployment = result.fetchone()
    if not deployment:
        raise HTTPException(status_code=404, detail="Deployment not found")

    is_owner = str(deployment.user_id) == str(current_user.id)
    if not is_owner:
        member_check = await pg.execute(
            text(
                """
                SELECT 1
                FROM deployment_members
                WHERE deployment_id = :deployment_id AND user_id = :user_id
                LIMIT 1
                """
            ),
            {"deployment_id": deployment_id, "user_id": current_user.id},
        )
        if not member_check.fetchone():
            raise HTTPException(status_code=403, detail="You are not a participant of this deployment.")

    return _build_access_details_payload(
        deployment_id=str(deployment.id),
        lab_type=deployment.lab_type,
        status=deployment.status,
        is_owner=is_owner,
        terraform_outputs=deployment.terraform_outputs,
        expires_at=deployment.expires_at,
    )


@router.post("/admin/deployments/{deployment_id}/members/{user_id}", status_code=201)
async def add_participant(
    deployment_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. Add a participant to a deployment."""
    dep = await pg.execute(
        text("SELECT id FROM lab_deployments WHERE id = :id"),
        {"id": deployment_id},
    )
    if not dep.fetchone():
        raise HTTPException(status_code=404, detail="Deployment not found")

    usr = await pg.execute(
        text("SELECT id, email, is_active FROM users WHERE id = :id"),
        {"id": user_id},
    )
    user_row = usr.fetchone()
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
    if not user_row.is_active:
        raise HTTPException(status_code=400, detail="User is inactive")

    await pg.execute(
        text("""
            INSERT INTO deployment_members (deployment_id, user_id, added_by)
            VALUES (:deployment_id, :user_id, :added_by)
            ON CONFLICT (deployment_id, user_id) DO NOTHING
        """),
        {"deployment_id": deployment_id, "user_id": user_id, "added_by": str(admin.id)},
    )
    await pg.commit()

    log.info(
        "Participant added: deployment_id=%s user_id=%s by sys_admin=%s",
        deployment_id, user_id, admin.id,
    )
    return {
        "deployment_id": deployment_id,
        "user_id": user_id,
        "email": user_row.email,
        "message": "Participant added to deployment successfully",
    }


@router.delete("/admin/deployments/{deployment_id}/members/{user_id}", status_code=200)
async def remove_participant(
    deployment_id: str = Path(...),
    user_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. Remove a participant from a deployment."""
    result = await pg.execute(
        text("""
            DELETE FROM deployment_members
            WHERE deployment_id = :deployment_id AND user_id = :user_id
            RETURNING deployment_id
        """),
        {"deployment_id": deployment_id, "user_id": user_id},
    )
    if not result.fetchone():
        raise HTTPException(
            status_code=404,
            detail="Participant not found on this deployment",
        )

    await pg.commit()
    log.info(
        "Participant removed: deployment_id=%s user_id=%s",
        deployment_id, user_id,
    )
    return {"deployment_id": deployment_id, "user_id": user_id, "message": "Participant removed"}


@router.get("/admin/deployments/{deployment_id}/members")
async def list_participants(
    deployment_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. List all participants in a deployment."""
    result = await pg.execute(
        text("""
            SELECT dm.user_id, u.email, dm.added_by, dm.added_at
            FROM deployment_members dm
            JOIN users u ON dm.user_id = u.id
            WHERE dm.deployment_id = :deployment_id
            ORDER BY dm.added_at ASC
        """),
        {"deployment_id": deployment_id},
    )
    rows = result.fetchall()

    return {
        "deployment_id": deployment_id,
        "count": len(rows),
        "participants": [
            {
                "user_id": r.user_id,
                "email": r.email,
                "added_by": r.added_by,
                "added_at": r.added_at,
            }
            for r in rows
        ],
    }


@router.get("/admin/memberships/by-user")
async def list_participant_memberships_by_user(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """
    sys_admin only. Returns participant memberships aggregated by user_id.

    Response rows map each user to labs where they were added as participant.
    """
    result = await pg.execute(
        text(
            """
            SELECT
                dm.user_id,
                ld.id AS deployment_id,
                COALESCE(ci.title, ld.lab_type) AS lab_title,
                ld.status
            FROM deployment_members dm
            JOIN lab_deployments ld ON dm.deployment_id = ld.id
            LEFT JOIN content_items ci ON ld.content_id = ci.id
            ORDER BY dm.user_id, ld.created_at DESC
            LIMIT 5000
            """
        )
    )
    rows = result.fetchall()

    return {
        "count": len(rows),
        "rows": [
            {
                "user_id": r.user_id,
                "deployment_id": r.deployment_id,
                "lab_title": r.lab_title,
                "status": r.status,
            }
            for r in rows
        ],
    }


@router.get("/admin/coverage")
async def list_deployment_coverage(
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """
    sys_admin only. Course deployment user coverage aggregate.

    Scope is limited to course-admin-managed deployments (joined through
    course_admin_assignments) so personal deployments do not appear in this monitor.
    """
    result = await pg.execute(
        text(
            """
            SELECT
                ld.id AS deployment_id,
                ld.content_id,
                ld.status,
                ld.created_at,
                COALESCE(ci.title, ld.lab_type) AS lab_title,
                u.email AS owner_email,
                COUNT(DISTINCT dm.user_id) AS attached_count,
                COUNT(DISTINCT cp.user_id) AS enrolled_count
            FROM lab_deployments ld
            JOIN course_admin_assignments caa
                ON caa.user_id = ld.user_id
               AND caa.content_id = ld.content_id
            JOIN users u ON ld.user_id = u.id
            LEFT JOIN content_items ci ON ld.content_id = ci.id
            LEFT JOIN deployment_members dm ON ld.id = dm.deployment_id
            LEFT JOIN course_participants cp ON ld.content_id = cp.content_id
            GROUP BY ld.id, ld.content_id, ld.status, ld.created_at, ci.title, u.email, ld.lab_type
            ORDER BY ld.created_at DESC
            LIMIT 500
            """
        )
    )
    rows = result.fetchall()

    deployments = []
    for r in rows:
        attached_count = int(r.attached_count or 0)
        enrolled_count = int(r.enrolled_count or 0)
        gap_count = max(enrolled_count - attached_count, 0)
        status = (r.status or "").lower()

        if status != "running":
            coverage_state = "not_running"
        elif attached_count == 0:
            coverage_state = "no_users_added"
        elif attached_count < enrolled_count:
            coverage_state = "users_missing"
        else:
            coverage_state = "all_users_added"

        deployments.append(
            {
                "deployment_id": str(r.deployment_id),
                "content_id": str(r.content_id),
                "lab_title": r.lab_title,
                "owner_email": r.owner_email,
                "status": r.status,
                "created_at": r.created_at,
                "attached_count": attached_count,
                "enrolled_count": enrolled_count,
                "gap_count": gap_count,
                "coverage_state": coverage_state,
            }
        )

    return {"count": len(deployments), "rows": deployments}


@router.get("/admin/all")
async def list_all_labs(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. Returns paginated deployments across all users."""
    total_result = await pg.execute(text("SELECT COUNT(*) AS total FROM lab_deployments"))
    total = int(total_result.scalar() or 0)

    result = await pg.execute(
        text("""
            SELECT ld.id, ld.content_id, ld.status, ld.lab_type, ld.user_id, u.email,
                   ld.instance_public_ip, ld.instance_private_ip,
                   ld.error_message, ld.terraform_outputs, ld.created_at, ld.updated_at, ld.expires_at,
                   ci.title, COUNT(dm.user_id) AS participant_count
            FROM lab_deployments ld
            JOIN content_items ci ON ld.content_id = ci.id
            JOIN users u ON ld.user_id = u.id
            LEFT JOIN deployment_members dm ON ld.id = dm.deployment_id
            GROUP BY ld.id, ci.title, u.email
            ORDER BY ld.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        {"limit": limit, "offset": offset},
    )
    rows = result.fetchall()

    return {
        "count": len(rows),
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(rows)) < total,
        "deployments": [
            {
                "deployment_id": r.id,
                "content_id": r.content_id,
                "status": r.status,
                "lab_type": r.lab_type,
                "user_id": r.user_id,
                "user_email": r.email,
                "public_ip": r.instance_public_ip,
                "private_ip": r.instance_private_ip,
                "error": r.error_message,
                "terraform_outputs": r.terraform_outputs,
                "lab_title": r.title,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "expires_at": r.expires_at,
                "participant_count": r.participant_count,
            }
            for r in rows
        ],
    }


@router.get("/admin/deployments/{deployment_id}")
async def get_admin_deployment_by_id(
    deployment_id: str = Path(...),
    pg: AsyncSession = Depends(get_pg),
    _admin: CurrentUser = Depends(SysAdminOnly),
):
    """sys_admin only. Returns one deployment by id."""
    result = await pg.execute(
        text(
            """
            SELECT ld.id, ld.content_id, ld.status, ld.lab_type, ld.user_id, u.email,
                   ld.instance_public_ip, ld.instance_private_ip,
                   ld.error_message, ld.terraform_outputs, ld.created_at, ld.updated_at, ld.expires_at,
                   ci.title, COUNT(dm.user_id) AS participant_count
            FROM lab_deployments ld
            JOIN content_items ci ON ld.content_id = ci.id
            JOIN users u ON ld.user_id = u.id
            LEFT JOIN deployment_members dm ON ld.id = dm.deployment_id
            WHERE ld.id = :deployment_id
            GROUP BY ld.id, ci.title, u.email
            LIMIT 1
            """
        ),
        {"deployment_id": deployment_id},
    )
    r = result.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Deployment not found")

    return {
        "deployment": {
            "deployment_id": r.id,
            "content_id": r.content_id,
            "status": r.status,
            "lab_type": r.lab_type,
            "user_id": r.user_id,
            "user_email": r.email,
            "public_ip": r.instance_public_ip,
            "private_ip": r.instance_private_ip,
            "error": r.error_message,
            "terraform_outputs": r.terraform_outputs,
            "lab_title": r.title,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "expires_at": r.expires_at,
            "participant_count": r.participant_count,
        }
    }