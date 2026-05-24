from __future__ import annotations

import hashlib
import logging
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from backend.utils.headscale_client import HeadscaleClient
from backend.config import get_settings

log = logging.getLogger("headscale")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Key hashing (T-08) ────────────────────────────────────────────────────────

def _hash_key(raw_key: str) -> str:
    """SHA-256 hex digest of a preauth key, prefixed for clear identification."""
    return "sha256:" + hashlib.sha256(raw_key.encode()).hexdigest()


# ── SSM helpers (T-09) ────────────────────────────────────────────────────────

def _ssm_param_name(deployment_id: str) -> str:
    """
    Canonical SSM parameter path for a deployment's Headscale auth key.
    Namespaced under /cyberrange/ so IAM policies can be scoped with a
    single path prefix condition.
    """
    return f"/cyberrange/headscale-key/{deployment_id}"


def _ssm_client():
    settings = get_settings()
    return boto3.client("ssm", region_name=settings.AWS_REGION)


def _put_ssm_key(deployment_id: str, raw_key: str, expires_at: datetime) -> str:
    name = _ssm_param_name(deployment_id)
    _ssm_client().put_parameter(
        Name=name,
        Value=raw_key,
        Type="SecureString",
        Overwrite=True,
        Description=f"Headscale auth key for deployment {deployment_id} — deleted by cleanup worker after lab expiry",
    )
    log.info("[%s] Headscale key stored in SSM: %s (expires: %s)", deployment_id, name, expires_at)
    return name


def _delete_ssm_key(deployment_id: str) -> None:
    """
    Delete the SSM parameter after terraform apply has consumed it.
    Failure is logged but not fatal — the Headscale key expires naturally.
    """
    name = _ssm_param_name(deployment_id)
    try:
        _ssm_client().delete_parameter(Name=name)
        log.info("[%s] SSM parameter deleted: %s", deployment_id, name)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ParameterNotFound":
            log.info("[%s] SSM parameter already gone: %s", deployment_id, name)
        else:
            log.warning("[%s] Failed to delete SSM parameter %s: %s", deployment_id, name, e)


# ── Identity helpers ──────────────────────────────────────────────────────────

async def _get_identity(pg: AsyncSession, user_id: str) -> Optional[Dict[str, Any]]:
    res = await pg.execute(
        text("""
            SELECT user_id, headscale_username, headscale_user_id
            FROM headscale_identities
            WHERE user_id = :uid
        """),
        {"uid": user_id},
    )
    row = res.fetchone()
    if not row:
        return None

    hs_user_id = row.headscale_user_id
    if not isinstance(hs_user_id, int):
        log.error(
            "headscale_identities row for user %s has non-integer headscale_user_id: %r (%s). "
            "Row will be re-provisioned.",
            user_id, hs_user_id, type(hs_user_id).__name__,
        )
        return None

    return {
        "user_id": str(row.user_id),
        "headscale_username": row.headscale_username,
        "headscale_user_id": hs_user_id,
    }


async def _create_identity(
    pg: AsyncSession, hs: HeadscaleClient, user_id: str
) -> Dict[str, Any]:
    hs_username = f"user-{user_id}"
    log.info("Creating Headscale identity for user_id=%s (hs_username=%s)", user_id, hs_username)

    created = await hs.ensure_user(hs_username)
    hs_user_id: int = created["id"]
    hs_username = created["name"]

    if not isinstance(hs_user_id, int):
        raise RuntimeError(
            f"ensure_user returned non-integer id for '{hs_username}': "
            f"{hs_user_id!r} ({type(hs_user_id).__name__})"
        )

    log.info(
        "Upserting headscale_identities: user_id=%s hs_user_id=%s hs_username=%s",
        user_id, hs_user_id, hs_username,
    )

    await pg.execute(
        text("""
            INSERT INTO headscale_identities
                (user_id, headscale_username, headscale_user_id, created_at, updated_at)
            VALUES (:uid, :hs_user, :hs_uid, now(), now())
            ON CONFLICT (user_id)
            DO UPDATE SET headscale_username = EXCLUDED.headscale_username,
                          headscale_user_id  = EXCLUDED.headscale_user_id,
                          updated_at = now()
        """),
        {"uid": user_id, "hs_user": hs_username, "hs_uid": hs_user_id},
    )
    await pg.commit()

    return {
        "user_id": user_id,
        "headscale_username": hs_username,
        "headscale_user_id": hs_user_id,
    }


async def _insert_key(
    pg: AsyncSession,
    user_id: str,
    key_type: str,
    key_hash: str,          # T-08: hash only, never the raw key
    hs_user: str,
    hs_id: Optional[str],
    reusable: bool,
    ephemeral: bool,
    expiration: datetime,
    acl_tags: Optional[List[str]],
    hs_created_at: Optional[datetime] = None,
) -> None:
    await pg.execute(
        text("""
            INSERT INTO headscale_keys (
                user_id, type, key_hash, hs_id, hs_user,
                reusable, ephemeral, used, expiration,
                hs_created_at, acl_tags, created_at
            )
            VALUES (
                :user_id, :type, :key_hash, :hs_id, :hs_user,
                :reusable, :ephemeral, false, :expiration,
                :hs_created_at, :acl_tags, now()
            )
        """),
        {
            "user_id":     user_id,
            "type":        key_type,
            "key_hash":    key_hash,
            "hs_id":       hs_id,
            "hs_user":     hs_user,
            "reusable":    reusable,
            "ephemeral":   ephemeral,
            "expiration":  expiration,
            "hs_created_at": hs_created_at,
            "acl_tags":    acl_tags or [],
        },
    )
    await pg.commit()


# ── Public API ────────────────────────────────────────────────────────────────

async def mint_preauth_key(
    pg: AsyncSession,
    user_id: str,
    key_type: str,
    expires_at: datetime,
    deployment_id: Optional[str] = None,
    acl_tags: Optional[List[str]] = None,
    reusable: bool = False,
    ephemeral: bool = False,
) -> str:
    """
    Mint a Headscale preauth key and persist an audit record.

    Returns:
      - key_type == "router": SSM parameter name (e.g. /cyberrange/headscale-key/<id>).
        Terraform passes this name to the subnet router; the EC2 instance fetches
        the raw value from SSM at boot. Call delete_router_ssm_key() after apply.
      - key_type == "device": raw preauth key, consumed directly by `tailscale up`.

    T-08: Only key_hash (SHA-256) is written to the DB — never the raw key.
    T-09: Router keys are stored in SSM; Terraform never sees the raw value.
    """
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= _utcnow():
        raise ValueError(f"Refusing to mint key with past expiration: {expires_at}")
    if key_type == "router" and not deployment_id:
        raise ValueError("deployment_id is required when key_type='router'")

    hs = HeadscaleClient.from_settings()

    identity = await _get_identity(pg, user_id)
    if not identity:
        log.info("No Headscale identity for user_id=%s — provisioning...", user_id)
        identity = await _create_identity(pg, hs, user_id)

    hs_user    = identity["headscale_username"]
    hs_user_id = identity["headscale_user_id"]

    log.info(
        "Minting preauth key: user_id=%s hs_user=%s key_type=%s expires_at=%s",
        user_id, hs_user, key_type, expires_at,
    )

    created = await hs.create_preauth_key(
        hs_user_id=hs_user_id,
        expiration=expires_at,
        reusable=reusable,
        ephemeral=ephemeral,
        acl_tags=acl_tags or [],
    )
    raw_key = created["key"]

    if key_type == "router":
        ssm_name = _put_ssm_key(deployment_id, raw_key, expires_at)  # ← add expires_at
        return ssm_name

    # T-08: hash before writing to DB
    await _insert_key(
        pg=pg,
        user_id=user_id,
        key_type=key_type,
        key_hash=_hash_key(raw_key),
        hs_user=hs_user,
        hs_id=str(created["id"]) if created.get("id") is not None else None,
        reusable=reusable,
        ephemeral=ephemeral,
        expiration=expires_at,
        acl_tags=acl_tags or [],
        hs_created_at=created.get("created_at"),
    )
    log.info("Preauth key minted and stored (hashed) for user_id=%s", user_id)

    return raw_key


def delete_router_ssm_key(deployment_id: str) -> None:
    """
    Remove the SSM parameter for a router key once terraform apply has finished.
    The EC2 instance fetched the key at boot; keeping it around serves no purpose.
    Call this from the worker immediately after run_terraform() returns.
    """
    _delete_ssm_key(deployment_id)


async def delete_nodes_for_deployment(deployment_id: str) -> None:
    """Delete all Headscale nodes belonging to this deployment."""
    log.info("[%s] Listing Headscale nodes...", deployment_id)
    hs = HeadscaleClient.from_settings()
    nodes = await hs.list_nodes()

    matched = [n for n in nodes if deployment_id in (n.get("name") or "")]
    if not matched:
        log.info("[%s] No Headscale nodes found.", deployment_id)
        return

    log.info("[%s] Found %d node(s) to delete.", deployment_id, len(matched))
    for node in matched:
        node_id   = str(node.get("id") or node.get("ID"))
        node_name = node.get("name") or node_id
        log.info("[%s] Deleting node: %s (id=%s)", deployment_id, node_name, node_id)
        await hs.delete_node(node_id)
        log.info("[%s] Deleted node: %s", deployment_id, node_name)