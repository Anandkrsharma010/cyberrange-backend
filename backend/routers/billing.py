"""Billing: Razorpay orders (authenticated) and webhooks (signature only)."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import ROLE_COURSE_ADMIN, ROLE_SYS_ADMIN, get_settings
from backend.dependencies.authz import AnyAuthenticatedUser, SysAdminOnly
from backend.limiter import limiter
from backend.pg import get_pg
from backend.schemas.auth import CurrentUser
from backend.schemas.billing import (
    CreateOrderRequest,
    CreateOrderResponse,
    CreateWorkshopOrderRequest,
    EntitlementRow,
    VerifyCaptureRequest,
    VerifyCaptureResponse,
)
from backend.services.razorpay_billing import (
    create_order_for_content,
    create_order_for_workshop,
    event_id_from_payload,
    process_webhook_payload,
    record_webhook_event,
    verify_capture_and_fulfill,
    verify_webhook_signature,
)
from backend.services.ops_feed import emit_ops_event

log = logging.getLogger("billing")
settings = get_settings()

router = APIRouter(prefix="/billing", tags=["Billing"])
webhook_router = APIRouter(tags=["Webhooks"])


async def _require_workshop_order_actor(
    pg: AsyncSession,
    user: CurrentUser,
    workshop_id: str,
) -> None:
    """Who may create a Razorpay package order for a cohort."""
    if user.role == ROLE_SYS_ADMIN:
        return
    if user.role != ROLE_COURSE_ADMIN:
        raise HTTPException(status_code=403, detail="Access denied")
    r = await pg.execute(
        text(
            """
            SELECT 1 FROM workshop_course_admins
            WHERE workshop_id = CAST(:w AS uuid) AND user_id = CAST(:u AS uuid)
            """
        ),
        {"w": workshop_id, "u": str(user.id)},
    )
    if not r.fetchone():
        raise HTTPException(
            status_code=403,
            detail="Not assigned to this workshop",
        )


@router.post("/orders", response_model=CreateOrderResponse)
@limiter.limit(settings.RATE_LIMIT_BILLING)
async def create_order(
    request: Request,
    response: Response,
    body: CreateOrderRequest,
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Payment gateway is not configured.",
        )
    try:
        out = await create_order_for_content(
            pg,
            settings,
            current_user.id,
            body.content_id,
        )
        await pg.commit()
    except ValueError as e:
        await pg.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        await pg.rollback()
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception:
        await pg.rollback()
        log.exception("create_order failed")
        raise HTTPException(status_code=500, detail="Could not create order") from None

    await emit_ops_event(
        pg,
        event_key=f"billing-order-created:{out['internal_payment_id']}",
        event_type="billing.order_created",
        severity="info",
        title="Checkout order created",
        message="A Razorpay order was created for retail content checkout.",
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="content",
        subject_id=str(body.content_id),
        deep_link=(
            f"/admin/billing/payments?user_id={current_user.id}&status=pending"
        ),
        metadata={
            "internal_payment_id": str(out["internal_payment_id"]),
            "gateway_order_id": out["razorpay_order_id"],
            "content_id": str(body.content_id),
        },
        emitter="billing.router",
    )
    await pg.commit()

    return CreateOrderResponse(
        razorpay_order_id=out["razorpay_order_id"],
        amount_minor=out["amount_minor"],
        currency=out["currency"],
        razorpay_key_id=settings.RAZORPAY_KEY_ID,
        internal_payment_id=out["internal_payment_id"],
    )


@router.post("/workshop-orders", response_model=CreateOrderResponse)
@limiter.limit(settings.RATE_LIMIT_BILLING)
async def create_workshop_order(
    request: Request,
    response: Response,
    body: CreateWorkshopOrderRequest,
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    """
    Corporate cohort checkout: one order for (seat_cap × catalog unit price).
    Webhook fulfillment links payments → workshops; no per-payer lab entitlement.
    """
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Payment gateway is not configured.",
        )
    await _require_workshop_order_actor(pg, current_user, str(body.workshop_id))
    try:
        out = await create_order_for_workshop(
            pg,
            settings,
            current_user.id,
            body.workshop_id,
        )
        await pg.commit()
    except ValueError as e:
        await pg.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        await pg.rollback()
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception:
        await pg.rollback()
        log.exception("create_workshop_order failed")
        raise HTTPException(status_code=500, detail="Could not create order") from None

    await emit_ops_event(
        pg,
        event_key=f"billing-workshop-order-created:{out['internal_payment_id']}",
        event_type="billing.workshop_order_created",
        severity="info",
        title="Cohort package checkout created",
        message="A Razorpay order was created for a workshop (cohort) package.",
        actor_user_id=str(current_user.id),
        actor_email=getattr(current_user, "email", None),
        subject_type="workshop",
        subject_id=str(body.workshop_id),
        workshop_id=str(body.workshop_id),
        deep_link=(
            f"/admin/billing/payments?user_id={current_user.id}&status=pending"
        ),
        metadata={
            "internal_payment_id": str(out["internal_payment_id"]),
            "gateway_order_id": out["razorpay_order_id"],
            "workshop_id": str(body.workshop_id),
        },
        emitter="billing.router",
    )
    await pg.commit()

    return CreateOrderResponse(
        razorpay_order_id=out["razorpay_order_id"],
        amount_minor=out["amount_minor"],
        currency=out["currency"],
        razorpay_key_id=settings.RAZORPAY_KEY_ID,
        internal_payment_id=out["internal_payment_id"],
    )


@router.get("/entitlements", response_model=list[EntitlementRow])
async def list_my_entitlements(
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    result = await pg.execute(
        text("""
            SELECT content_id, status, valid_from, valid_until
            FROM entitlements
            WHERE user_id = :uid AND status = 'active'
            ORDER BY valid_from DESC NULLS LAST
        """),
        {"uid": str(current_user.id)},
    )
    rows = result.fetchall()
    out: list[EntitlementRow] = []
    for r in rows:
        out.append(
            EntitlementRow(
                content_id=r.content_id,
                status=r.status,
                valid_from=r.valid_from.isoformat() if r.valid_from else None,
                valid_until=r.valid_until.isoformat() if r.valid_until else None,
            )
        )
    return out


@router.get("/admin/payments")
async def admin_list_payments(
    status: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    _admin: CurrentUser = Depends(SysAdminOnly),
    pg: AsyncSession = Depends(get_pg),
):
    """
    sys_admin only. Payment-level operational list for admin billing handling.
    """
    normalized_status = status.strip().lower() if status else None
    allowed_status = {"pending", "captured", "paid", "failed", "cancelled", "refunded"}
    if normalized_status and normalized_status not in allowed_status:
        raise HTTPException(status_code=400, detail="Unsupported payment status filter")

    where_clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if normalized_status:
        where_clauses.append("lower(p.status) = :status")
        params["status"] = normalized_status
    if user_id:
        where_clauses.append("p.user_id::text = :user_id")
        params["user_id"] = user_id

    base_sql = """
            SELECT
                p.id::text AS payment_id,
                p.user_id::text AS user_id,
                u.email,
                p.gateway,
                p.gateway_order_id,
                p.gateway_payment_id,
                p.amount,
                p.currency,
                p.status,
                p.created_at,
                pu.content_id::text AS content_id,
                ci.title AS content_title,
                CASE WHEN pu.payment_id IS NULL THEN false ELSE true END AS purchase_exists,
                e.status AS entitlement_status,
                EXISTS (
                    SELECT 1
                    FROM billing_webhook_events bwe
                    WHERE bwe.gateway = p.gateway
                      AND (
                        (bwe.payload #>> '{payload,payment,entity,order_id}') = p.gateway_order_id
                        OR (
                          p.gateway_payment_id IS NOT NULL
                          AND (bwe.payload #>> '{payload,payment,entity,id}') = p.gateway_payment_id
                        )
                      )
                ) AS webhook_seen
            FROM payments p
            JOIN users u ON u.id = p.user_id
            LEFT JOIN purchases pu ON pu.payment_id = p.id
            LEFT JOIN content_items ci ON ci.id = pu.content_id
            LEFT JOIN entitlements e ON e.user_id = pu.user_id AND e.content_id = pu.content_id
            ORDER BY p.created_at DESC
            LIMIT :limit
            """
    if where_clauses:
        where_sql = " WHERE " + " AND ".join(where_clauses)
        sql_text = base_sql.replace("ORDER BY p.created_at DESC", f"{where_sql}\n            ORDER BY p.created_at DESC")
    else:
        sql_text = base_sql

    result = await pg.execute(
        text(sql_text),
        params,
    )
    rows = result.fetchall()

    return {
        "count": len(rows),
        "rows": [
            {
                "payment_id": r.payment_id,
                "user_id": r.user_id,
                "email": r.email,
                "gateway": r.gateway,
                "gateway_order_id": r.gateway_order_id,
                "gateway_payment_id": r.gateway_payment_id,
                "amount": int(r.amount),
                "currency": r.currency,
                "status": r.status,
                "created_at": r.created_at,
                "content_id": r.content_id,
                "content_title": r.content_title,
                "purchase_exists": bool(r.purchase_exists),
                "entitlement_status": r.entitlement_status,
                "webhook_seen": bool(r.webhook_seen),
            }
            for r in rows
        ],
    }


@router.post("/verify-capture", response_model=VerifyCaptureResponse)
@limiter.limit(settings.RATE_LIMIT_BILLING)
async def verify_capture(
    request: Request,
    response: Response,
    body: VerifyCaptureRequest,
    current_user: CurrentUser = Depends(AnyAuthenticatedUser),
    pg: AsyncSession = Depends(get_pg),
):
    """
    After Razorpay checkout returns success, the client calls this so the server
    fetches the payment from Razorpay and updates payments / purchases /
    entitlements — same outcome as a successful webhook.
    """
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Payment gateway is not configured.",
        )
    try:
        outcome = await verify_capture_and_fulfill(
            pg,
            settings,
            current_user.id,
            body.razorpay_payment_id.strip(),
            body.razorpay_order_id.strip(),
        )
        await pg.commit()
        return VerifyCaptureResponse(status=outcome)
    except ValueError as e:
        await pg.rollback()
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        await pg.rollback()
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception:
        await pg.rollback()
        log.exception("verify_capture failed")
        raise HTTPException(status_code=500, detail="Could not verify payment") from None


@webhook_router.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    pg: AsyncSession = Depends(get_pg),
):
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")

    body = await request.body()
    sig = request.headers.get("X-Razorpay-Signature")
    if not verify_webhook_signature(body, sig, settings.RAZORPAY_WEBHOOK_SECRET):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload: dict[str, Any] = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from e

    event_id = event_id_from_payload(payload)

    try:
        first_time = await record_webhook_event(pg, event_id, payload)
        if not first_time:
            await pg.commit()
            return {"status": "ok", "duplicate": True}

        await process_webhook_payload(pg, payload)
        await pg.commit()
    except Exception:
        await pg.rollback()
        log.exception("razorpay_webhook processing failed")
        raise HTTPException(status_code=500, detail="Webhook processing failed") from None

    return {"status": "ok"}
