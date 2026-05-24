from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

OPS_METADATA_SCHEMA_VERSION = 1
_OPS_SEVERITIES = frozenset({"info", "warning", "critical"})


def _normalize_ops_metadata(
    metadata: dict[str, Any] | None, *, emitter: str | None
) -> dict[str, Any]:
    out: dict[str, Any] = dict(metadata or {})
    if emitter:
        out["emitter"] = emitter
    out["schema_version"] = OPS_METADATA_SCHEMA_VERSION
    return out


def _normalize_event_key(event_key: str) -> str:
    k = (event_key or "").strip()
    if not k:
        raise ValueError("operations_feed event_key must be a non-empty string")
    return k


def _coerce_severity(severity: str) -> str:
    s = (severity or "info").strip().lower()
    if s not in _OPS_SEVERITIES:
        log.warning("operations_feed: invalid severity %r — using info", severity)
        return "info"
    return s


async def emit_ops_event(
    pg: AsyncSession,
    *,
    event_key: str,
    event_type: str,
    severity: str,
    title: str,
    message: str,
    actor_user_id: str | None = None,
    actor_email: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    workshop_id: str | None = None,
    deployment_id: str | None = None,
    target_user_id: str | None = None,
    deep_link: str | None = None,
    metadata: dict[str, Any] | None = None,
    emitter: str | None = None,
) -> None:
    """Insert or update an operations-feed event using event_key for idempotency."""
    ek = _normalize_event_key(event_key)
    sev = _coerce_severity(severity)
    md = _normalize_ops_metadata(metadata, emitter=emitter)
    await pg.execute(
        text(
            """
            INSERT INTO operations_feed (
                id, event_key, event_type, severity, title, message,
                actor_user_id, actor_email, subject_type, subject_id,
                workshop_id, deployment_id, target_user_id, deep_link, metadata,
                is_read, read_at, read_by, created_at, updated_at
            )
            VALUES (
                gen_random_uuid(), :event_key, :event_type, :severity, :title, :message,
                CAST(:actor_user_id AS uuid), :actor_email, :subject_type, :subject_id,
                CAST(:workshop_id AS uuid), CAST(:deployment_id AS uuid), CAST(:target_user_id AS uuid),
                :deep_link, CAST(:metadata AS jsonb), false, NULL, NULL, now(), now()
            )
            ON CONFLICT (event_key) DO UPDATE SET
                severity = EXCLUDED.severity,
                title = EXCLUDED.title,
                message = EXCLUDED.message,
                actor_user_id = EXCLUDED.actor_user_id,
                actor_email = EXCLUDED.actor_email,
                subject_type = EXCLUDED.subject_type,
                subject_id = EXCLUDED.subject_id,
                workshop_id = EXCLUDED.workshop_id,
                deployment_id = EXCLUDED.deployment_id,
                target_user_id = EXCLUDED.target_user_id,
                deep_link = EXCLUDED.deep_link,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """
        ),
        {
            "event_key": ek,
            "event_type": event_type,
            "severity": sev,
            "title": title,
            "message": message,
            "actor_user_id": actor_user_id,
            "actor_email": actor_email,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "workshop_id": workshop_id,
            "deployment_id": deployment_id,
            "target_user_id": target_user_id,
            "deep_link": deep_link,
            "metadata": json.dumps(md),
        },
    )


_LAB_DEPLOY_FEED_TRANSITIONS = frozenset(
    {
        "provisioning",
        "running",
        "failed",
        "terminating",
        "expired",
        "cleanup_failed",
    }
)


async def emit_lab_deployment_feed_for_transition(
    pg: AsyncSession,
    *,
    deployment_id: str,
    transition: str,
    emitter: str,
) -> None:
    """
    Record a lab deployment lifecycle step for the sys_admin operations feed.
    Call in the same DB transaction as the status UPDATE, before commit.
    """
    if transition not in _LAB_DEPLOY_FEED_TRANSITIONS:
        return

    row = (
        await pg.execute(
            text(
                """
                SELECT
                    user_id::text AS user_id,
                    content_id::text AS content_id,
                    workshop_id::text AS workshop_id,
                    lab_type,
                    status,
                    error_message
                FROM lab_deployments
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": deployment_id},
        )
    ).mappings().first()
    if not row:
        return

    uid = row.get("user_id")
    cid = row.get("content_id")
    wid = row.get("workshop_id")
    lab_type = row.get("lab_type") or ""
    err = (row.get("error_message") or "").strip()
    err_short = err[:800] if err else ""

    if transition == "provisioning":
        title, sev = "Lab provisioning started", "info"
        msg = (
            f"Provisioning worker claimed deployment {deployment_id} "
            f"(lab_type={lab_type})."
        )
    elif transition == "running":
        title, sev = "Lab deployment running", "info"
        msg = f"Lab {deployment_id} finished provisioning and is running (lab_type={lab_type})."
    elif transition == "failed":
        title, sev = "Lab provisioning failed", "warning"
        msg = (
            f"Lab {deployment_id} failed during provisioning (lab_type={lab_type})."
            + (f" Detail: {err_short}" if err_short else "")
        )
    elif transition == "terminating":
        title, sev = "Lab teardown started", "info"
        msg = (
            f"Cleanup worker claimed deployment {deployment_id} for teardown "
            f"(lab_type={lab_type})."
        )
    elif transition == "expired":
        title, sev = "Lab deployment expired", "info"
        msg = f"Teardown completed for deployment {deployment_id}; status is expired."
    else:  # cleanup_failed
        title, sev = "Lab teardown failed", "warning"
        msg = (
            f"Cleanup failed for deployment {deployment_id} (lab_type={lab_type})."
            + (f" Detail: {err_short}" if err_short else "")
        )

    deep = f"/admin/ops/individual/deployment/{deployment_id}"
    await emit_ops_event(
        pg,
        event_key=f"lab-deploy:{deployment_id}:{transition}",
        event_type=f"lab.deployment_{transition}",
        severity=sev,
        title=title,
        message=msg,
        subject_type="deployment",
        subject_id=deployment_id,
        workshop_id=wid,
        deployment_id=deployment_id,
        target_user_id=uid,
        deep_link=deep,
        emitter=emitter,
        metadata={
            "lab_type": lab_type,
            "status": row.get("status"),
            "content_id": cid,
            "workshop_id": wid,
            "transition": transition,
        },
    )

