"""Razorpay order creation and webhook fulfillment."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import razorpay
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import Settings, get_settings
from backend.services.ops_feed import emit_ops_event

log = logging.getLogger("billing.razorpay")


async def merge_payment_notes_from_order(pay_ent: dict[str, Any]) -> dict[str, Any]:
    """Fill missing notes from Razorpay order (payment payload sometimes omits notes)."""
    order_id = pay_ent.get("order_id")
    notes = pay_ent.get("notes") or {}
    if not isinstance(notes, dict):
        notes = {}
    if not order_id:
        return pay_ent
    # Individual retail: order has no workshop_id; do not fetch when content_id is present.
    # Workshop package: order notes use kind=workshop; fetch if workshop_id was dropped on payment.
    need_fetch = not notes.get("content_id") or (
        notes.get("kind") == "workshop" and not notes.get("workshop_id")
    )
    if not need_fetch:
        return pay_ent
    s = get_settings()
    if not s.RAZORPAY_KEY_ID or not s.RAZORPAY_KEY_SECRET:
        return pay_ent
    client = razorpay.Client(auth=(s.RAZORPAY_KEY_ID, s.RAZORPAY_KEY_SECRET))

    def _fo() -> dict[str, Any]:
        return client.order.fetch(order_id)

    order = await asyncio.to_thread(_fo)
    if isinstance(order, dict):
        onotes = order.get("notes") or {}
        if isinstance(onotes, dict):
            merged = {**onotes, **notes}
            return {**pay_ent, "notes": merged}
    return pay_ent


def verify_webhook_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def event_id_from_payload(payload: dict[str, Any]) -> str:
    eid = payload.get("id")
    if isinstance(eid, str) and eid.strip():
        return eid.strip()
    pay = _extract_payment_entity(payload)
    pid = (pay or {}).get("id")
    ev = payload.get("event") or "unknown"
    created = payload.get("created_at")
    fallback = f"{ev}:{created}:{pid}"
    return hashlib.sha256(fallback.encode()).hexdigest()


def _extract_payment_entity(payload: dict[str, Any]) -> dict[str, Any] | None:
    pl = payload.get("payload") or {}
    if not isinstance(pl, dict):
        return None
    pay_wrap = pl.get("payment")
    if isinstance(pay_wrap, dict):
        ent = pay_wrap.get("entity")
        if isinstance(ent, dict):
            return ent
    return None


async def create_order_for_content(
    pg: AsyncSession,
    settings: Settings,
    user_id: UUID,
    content_id: UUID,
) -> dict[str, Any]:
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay keys are not configured")

    row = (
        await pg.execute(
            text("""
                SELECT pp.amount_minor, pp.currency, ci.id
                FROM product_prices pp
                JOIN content_items ci ON ci.id = pp.content_id
                WHERE pp.content_id = :content_id
                  AND pp.is_active = true
                  AND ci.is_active = true
            """),
            {"content_id": str(content_id)},
        )
    ).fetchone()

    if not row:
        raise ValueError("No active price for this content")

    amount_minor = int(row.amount_minor)
    currency = row.currency or "INR"

    receipt = uuid4().hex[:40]

    if settings.RAZORPAY_KEY_SECRET == "dummy_secret":
        order = {
            "id": f"order_mock_{uuid4().hex[:14]}",
            "amount": amount_minor,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
            "notes": {
                "user_id": str(user_id),
                "content_id": str(content_id),
            }
        }
    else:
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        def _create() -> dict[str, Any]:
            return client.order.create(
                {
                    "amount": amount_minor,
                    "currency": currency,
                    "receipt": receipt,
                    "notes": {
                        "user_id": str(user_id),
                        "content_id": str(content_id),
                    },
                }
            )

        order = await asyncio.to_thread(_create)
    razorpay_order_id = order["id"]

    payment_row = (
        await pg.execute(
            text("""
                INSERT INTO payments (
                    user_id, gateway, gateway_order_id, amount, currency, status,
                    kind, raw_response
                )
                VALUES (
                    :user_id, 'razorpay', :gateway_order_id, :amount, :currency, 'pending',
                    'one_time', cast(:raw as jsonb)
                )
                RETURNING id
            """),
            {
                "user_id": str(user_id),
                "gateway_order_id": razorpay_order_id,
                "amount": amount_minor,
                "currency": currency,
                "raw": json.dumps(order),
            },
        )
    ).fetchone()

    if not payment_row:
        raise RuntimeError("Failed to persist payment row")

    return {
        "razorpay_order_id": razorpay_order_id,
        "amount_minor": amount_minor,
        "currency": currency,
        "internal_payment_id": payment_row.id,
    }


async def create_order_for_workshop(
    pg: AsyncSession,
    settings: Settings,
    user_id: UUID,
    workshop_id: UUID,
) -> dict[str, Any]:
    """
    Corporate / cohort package: one Razorpay order for seat_cap × catalog unit price.
    Notes carry workshop_id so webhook fulfillment links payments → workshops (no per-seat capture).
    """
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay keys are not configured")

    wr = (
        await pg.execute(
            text(
                """
                SELECT
                    w.id, w.content_id, w.seat_cap, w.status
                FROM workshops w
                WHERE w.id = CAST(:wid AS uuid)
                """
            ),
            {"wid": str(workshop_id)},
        )
    ).mappings().first()
    if not wr:
        raise ValueError("Workshop not found")
    if wr["status"] == "archived":
        raise ValueError("Workshop is archived")
    seat_cap = int(wr["seat_cap"] or 0)
    if seat_cap < 1:
        raise ValueError("Workshop seat_cap must be at least 1 for a package order")

    row = (
        await pg.execute(
            text("""
                SELECT pp.amount_minor, pp.currency
                FROM product_prices pp
                JOIN content_items ci ON ci.id = pp.content_id
                WHERE pp.content_id = :content_id
                  AND pp.is_active = true
                  AND ci.is_active = true
            """),
            {"content_id": str(wr["content_id"])},
        )
    ).fetchone()
    if not row:
        raise ValueError("No active price for this workshop lab")

    unit_minor = int(row.amount_minor)
    currency = row.currency or "INR"
    amount_minor = unit_minor * seat_cap

    receipt = uuid4().hex[:40]
    cid = str(wr["content_id"])
    wid = str(workshop_id)

    if settings.RAZORPAY_KEY_SECRET == "dummy_secret":
        order = {
            "id": f"order_mock_{uuid4().hex[:14]}",
            "amount": amount_minor,
            "currency": currency,
            "receipt": receipt,
            "status": "created",
            "notes": {
                "user_id": str(user_id),
                "content_id": cid,
                "workshop_id": wid,
                "kind": "workshop",
            }
        }
    else:
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        def _create() -> dict[str, Any]:
            return client.order.create(
                {
                    "amount": amount_minor,
                    "currency": currency,
                    "receipt": receipt,
                    "notes": {
                        "user_id": str(user_id),
                        "content_id": cid,
                        "workshop_id": wid,
                        "kind": "workshop",
                    },
                }
            )

        order = await asyncio.to_thread(_create)
    razorpay_order_id = order["id"]

    payment_row = (
        await pg.execute(
            text("""
                INSERT INTO payments (
                    user_id, gateway, gateway_order_id, amount, currency, status,
                    kind, raw_response
                )
                VALUES (
                    :user_id, 'razorpay', :gateway_order_id, :amount, :currency, 'pending',
                    'one_time', cast(:raw as jsonb)
                )
                RETURNING id
            """),
            {
                "user_id": str(user_id),
                "gateway_order_id": razorpay_order_id,
                "amount": amount_minor,
                "currency": currency,
                "raw": json.dumps(order),
            },
        )
    ).fetchone()

    if not payment_row:
        raise RuntimeError("Failed to persist payment row")

    return {
        "razorpay_order_id": razorpay_order_id,
        "amount_minor": amount_minor,
        "currency": currency,
        "internal_payment_id": payment_row.id,
    }


async def process_webhook_payload(pg: AsyncSession, payload: dict[str, Any]) -> None:
    event = payload.get("event")
    if event == "payment.captured":
        pay_ent = _extract_payment_entity(payload)
        if not pay_ent:
            log.warning("payment.captured missing payment entity")
            return
        await _fulfill_payment_captured(pg, pay_ent)
        return
    if event == "payment.failed":
        pay_ent = _extract_payment_entity(payload)
        if pay_ent:
            await _mark_payment_failed(pg, pay_ent)
        return
    # Other events: acknowledged only (dedupe row already recorded)
    log.debug("Ignoring Razorpay event: %s", event)


async def _mark_payment_failed(pg: AsyncSession, pay_ent: dict[str, Any]) -> None:
    order_id = pay_ent.get("order_id")
    if not order_id:
        return
    up = await pg.execute(
        text("""
            UPDATE payments
            SET status = 'failed',
                gateway_payment_id = COALESCE(:pid, gateway_payment_id),
                raw_response = cast(:raw as jsonb)
            WHERE gateway = 'razorpay'
              AND gateway_order_id = :oid
              AND status = 'pending'
            RETURNING id, user_id
        """),
        {
            "oid": order_id,
            "pid": pay_ent.get("id"),
            "raw": json.dumps(pay_ent),
        },
    )
    row = up.fetchone()
    if not row:
        return
    pay_db_id, user_id = row[0], row[1]
    await emit_ops_event(
        pg,
        event_key=f"billing-payment-failed:{pay_db_id}",
        event_type="billing.payment_failed",
        severity="warning",
        title="Payment failed",
        message="Razorpay reported payment.failed for a pending checkout.",
        subject_type="payment",
        subject_id=str(pay_db_id),
        target_user_id=str(user_id),
        deep_link=f"/admin/billing/payments?user_id={user_id}&status=failed",
        metadata={
            "gateway_order_id": order_id,
            "gateway_payment_id": pay_ent.get("id"),
        },
        emitter="razorpay_billing",
    )


async def _workshop_package_capture_allowed(
    pg: AsyncSession,
    *,
    workshop_id: UUID,
    content_uuid: UUID,
    payment_amount_minor: int,
    payment_db_id: Any,
) -> tuple[bool, str]:
    wr = (
        await pg.execute(
            text(
                """
                SELECT id, content_id, seat_cap, payment_id, status
                FROM workshops
                WHERE id = CAST(:wid AS uuid)
                """
            ),
            {"wid": str(workshop_id)},
        )
    ).mappings().first()
    if not wr or wr["status"] == "archived":
        log.warning("workshop capture rejected: not found or archived")
        return False, "Workshop not found or is archived."
    if str(wr["content_id"]) != str(content_uuid):
        log.warning("workshop capture rejected: content_id mismatch")
        return (
            False,
            "Workshop lab content does not match payment metadata (content_id mismatch).",
        )
    seat_cap = int(wr["seat_cap"] or 0)
    if seat_cap < 1:
        log.warning("workshop capture rejected: seat_cap < 1")
        return False, "Workshop seat_cap is invalid for a package capture."
    pr = (
        await pg.execute(
            text("""
                SELECT amount_minor
                FROM product_prices
                WHERE content_id = CAST(:cid AS uuid) AND is_active = true
                LIMIT 1
            """),
            {"cid": str(content_uuid)},
        )
    ).fetchone()
    if not pr:
        log.warning("workshop capture rejected: no product_prices")
        return False, "No active product price for this lab content."
    unit_minor = int(pr[0])
    expected = unit_minor * seat_cap
    if int(payment_amount_minor) != int(expected):
        log.warning(
            "workshop capture rejected: amount expected=%s got=%s",
            expected,
            payment_amount_minor,
        )
        return (
            False,
            "Payment amount does not match seat_cap × catalog unit price "
            f"(expected {expected} minor units, got {payment_amount_minor}).",
        )
    existing_pid = wr.get("payment_id")
    if existing_pid is not None and str(existing_pid) != str(payment_db_id):
        log.warning("workshop capture rejected: workshop already linked to another payment")
        return (
            False,
            "This workshop is already linked to a different payment record.",
        )
    return True, ""


async def _link_workshop_payment_captured(
    pg: AsyncSession,
    *,
    workshop_id: UUID,
    payment_db_id: Any,
    payer_user_id: str,
) -> None:
    await pg.execute(
        text("""
            UPDATE workshops
            SET payment_id = CAST(:pid AS uuid),
                payment_status = 'paid',
                updated_at = now()
            WHERE id = CAST(:wid AS uuid)
        """),
        {"pid": str(payment_db_id), "wid": str(workshop_id)},
    )
    await pg.execute(
        text("""
            INSERT INTO content_activity_logs (actor_user_id, entity_type, entity_id, action, metadata)
            VALUES (
                CAST(:actor AS uuid),
                'workshop',
                :entity_id,
                'workshop.payment_captured',
                cast(:metadata as jsonb)
            )
        """),
        {
            "actor": payer_user_id,
            "entity_id": str(workshop_id),
            "metadata": json.dumps(
                {"payment_id": str(payment_db_id), "source": "razorpay"}
            ),
        },
    )


async def _fulfill_payment_captured(pg: AsyncSession, pay_ent: dict[str, Any]) -> None:
    pay_ent = await merge_payment_notes_from_order(pay_ent)
    order_id = pay_ent.get("order_id")
    pay_id = pay_ent.get("id")
    amount = pay_ent.get("amount")
    if not order_id:
        log.warning("payment.captured missing order_id: %s", pay_id)
        return

    notes = pay_ent.get("notes") or {}
    if not isinstance(notes, dict):
        notes = {}
    cid_raw = notes.get("content_id")
    uid_raw = notes.get("user_id")
    wid_raw = notes.get("workshop_id")

    result = await pg.execute(
        text("""
            SELECT id, user_id, amount, status
            FROM payments
            WHERE gateway = 'razorpay' AND gateway_order_id = :oid
        """),
        {"oid": order_id},
    )
    row = result.fetchone()
    if not row:
        log.warning("No local payment for Razorpay order_id=%s", order_id)
        return

    if uid_raw and str(row.user_id) != str(uid_raw):
        log.error("user_id mismatch on payment %s", row.id)
        return

    if not cid_raw:
        log.error("Razorpay payment missing notes.content_id for order %s", order_id)
        return

    try:
        content_uuid = UUID(str(cid_raw))
    except ValueError:
        log.error("Invalid content_id in notes for order %s", order_id)
        return

    workshop_uuid: UUID | None = None
    if wid_raw:
        try:
            workshop_uuid = UUID(str(wid_raw))
        except ValueError:
            log.error("Invalid workshop_id in notes for order %s", order_id)
            return

    if row.status == "captured":
        return

    if amount is not None and int(amount) != int(row.amount):
        log.error(
            "Amount mismatch for payment %s: razorpay=%s db=%s",
            row.id,
            amount,
            row.amount,
        )
        return

    if workshop_uuid is not None:
        ok, reject_reason = await _workshop_package_capture_allowed(
            pg,
            workshop_id=workshop_uuid,
            content_uuid=content_uuid,
            payment_amount_minor=int(row.amount),
            payment_db_id=row.id,
        )
        if not ok:
            await emit_ops_event(
                pg,
                event_key=f"billing-workshop-capture-rejected:{row.id}",
                event_type="billing.workshop_capture_rejected",
                severity="warning",
                title="Cohort package capture blocked",
                message=(
                    "Razorpay reported a captured payment, but it was not linked to the "
                    f"workshop: {reject_reason}"
                ),
                actor_user_id=str(row.user_id),
                subject_type="workshop",
                subject_id=str(workshop_uuid),
                workshop_id=str(workshop_uuid),
                target_user_id=str(row.user_id),
                deep_link=f"/admin/ops/workshop/{workshop_uuid}?tab=billing",
                metadata={
                    "payment_id": str(row.id),
                    "gateway_order_id": order_id,
                    "gateway_payment_id": pay_id,
                    "reject_reason": reject_reason,
                    "content_id": str(content_uuid),
                },
                emitter="razorpay_billing",
            )
            return

    up = await pg.execute(
        text("""
            UPDATE payments
            SET status = 'captured',
                gateway_payment_id = :pid,
                raw_response = cast(:raw as jsonb)
            WHERE id = :id AND status = 'pending'
            RETURNING id
        """),
        {
            "id": row.id,
            "pid": pay_id,
            "raw": json.dumps(pay_ent),
        },
    )
    if not up.fetchone():
        return

    if workshop_uuid is not None:
        await _link_workshop_payment_captured(
            pg,
            workshop_id=workshop_uuid,
            payment_db_id=row.id,
            payer_user_id=str(row.user_id),
        )
        await emit_ops_event(
            pg,
            event_key=f"billing-workshop-payment-captured:{row.id}",
            event_type="billing.workshop_payment_captured",
            severity="info",
            title="Cohort package payment captured",
            message="Razorpay capture linked this payment to the workshop record.",
            actor_user_id=str(row.user_id),
            subject_type="workshop",
            subject_id=str(workshop_uuid),
            workshop_id=str(workshop_uuid),
            deep_link=f"/admin/ops/workshop/{workshop_uuid}?tab=billing",
            metadata={
                "payment_id": str(row.id),
                "gateway_order_id": order_id,
                "gateway_payment_id": pay_id,
                "content_id": str(content_uuid),
            },
            emitter="razorpay_billing",
        )
        return

    await pg.execute(
        text("""
            INSERT INTO purchases (user_id, content_id, payment_id)
            VALUES (:user_id, :content_id, :payment_id)
            ON CONFLICT (user_id, content_id) DO NOTHING
        """),
        {
            "user_id": row.user_id,
            "content_id": str(content_uuid),
            "payment_id": row.id,
        },
    )

    await pg.execute(
        text("""
            INSERT INTO entitlements (user_id, content_id, status, valid_from, valid_until)
            VALUES (:user_id, :content_id, 'active', now(), NULL)
            ON CONFLICT (user_id, content_id) WHERE (workshop_id IS NULL)
            DO UPDATE SET
                status = 'active',
                valid_from = EXCLUDED.valid_from,
                valid_until = EXCLUDED.valid_until
        """),
        {"user_id": row.user_id, "content_id": str(content_uuid)},
    )

    await emit_ops_event(
        pg,
        event_key=f"billing-payment-captured:{row.id}",
        event_type="billing.payment_captured",
        severity="info",
        title="Retail payment captured",
        message="Payment captured; purchase and entitlement were applied.",
        actor_user_id=str(row.user_id),
        subject_type="content",
        subject_id=str(content_uuid),
        deep_link=f"/admin/billing/payments?user_id={row.user_id}&status=pending",
        metadata={
            "payment_id": str(row.id),
            "gateway_order_id": order_id,
            "gateway_payment_id": pay_id,
            "content_id": str(content_uuid),
        },
        emitter="razorpay_billing",
    )


async def verify_capture_and_fulfill(
    pg: AsyncSession,
    settings: Settings,
    user_id: UUID,
    razorpay_payment_id: str,
    razorpay_order_id: str,
) -> str:
    """
    Fetch payment from Razorpay API (server-side) and run the same fulfillment as
    the payment.captured webhook. Fixes stuck `pending` rows when webhooks fail.

    Returns: 'fulfilled' | 'already_fulfilled' | 'not_captured'
    """
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay keys are not configured")

    result = await pg.execute(
        text("""
            SELECT id, user_id, amount, status, raw_response
            FROM payments
            WHERE gateway = 'razorpay'
              AND gateway_order_id = :oid
              AND user_id = :uid
        """),
        {"oid": razorpay_order_id, "uid": str(user_id)},
    )
    row = result.fetchone()
    if not row:
        raise ValueError("No pending payment found for this order and account")

    if row.status == "captured":
        return "already_fulfilled"

    if settings.RAZORPAY_KEY_SECRET == "dummy_secret" or razorpay_order_id.startswith("order_mock_"):
        raw_resp = {}
        try:
            if row.raw_response and isinstance(row.raw_response, dict):
                raw_resp = row.raw_response
            elif isinstance(row.raw_response, str):
                raw_resp = json.loads(row.raw_response)
        except Exception:
            pass
        notes = raw_resp.get("notes") or {
            "user_id": str(user_id),
        }
        if not notes.get("content_id"):
            c_res = await pg.execute(
                text("""
                    SELECT content_id FROM product_prices WHERE amount_minor = :amount AND is_active = true LIMIT 1
                """),
                {"amount": row.amount}
            )
            c_row = c_res.fetchone()
            if c_row:
                notes["content_id"] = str(c_row.content_id)
        pay_ent = {
            "id": razorpay_payment_id,
            "order_id": razorpay_order_id,
            "amount": row.amount,
            "status": "captured",
            "notes": notes
        }
    else:
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        def _fetch_payment() -> dict[str, Any]:
            return client.payment.fetch(razorpay_payment_id)

        pay_ent = await asyncio.to_thread(_fetch_payment)
        if not isinstance(pay_ent, dict):
            raise ValueError("Invalid payment response")

    st = (pay_ent.get("status") or "").lower()
    if st != "captured":
        log.info(
            "verify_capture: payment %s status=%s (not captured)",
            razorpay_payment_id,
            st,
        )
        return "not_captured"

    oid = pay_ent.get("order_id")
    if oid is not None and str(oid) != str(razorpay_order_id):
        raise ValueError("Payment order_id does not match request")

    amt = pay_ent.get("amount")
    if amt is not None and int(amt) != int(row.amount):
        raise ValueError("Payment amount does not match order")

    pay_ent = await merge_payment_notes_from_order(pay_ent)
    merged = pay_ent.get("notes") or {}
    if not isinstance(merged, dict) or not merged.get("content_id"):
        raise ValueError("Razorpay order/payment is missing content_id in notes")

    await _fulfill_payment_captured(pg, pay_ent)

    # Same table as Razorpay POST /webhooks/razorpay — records server-side fulfillment when
    # tunnel/webhooks are unavailable (verify-capture path). Idempotent per payment id.
    synthetic_eid = f"verify_capture:{razorpay_payment_id}"
    synthetic_payload: dict[str, Any] = {
        "event": "payment.captured",
        "source": "server_verify_capture",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {"payment": {"entity": pay_ent}},
    }
    try:
        await record_webhook_event(pg, synthetic_eid, synthetic_payload)
    except Exception:
        log.exception(
            "billing_webhook_events insert failed after verify_capture (payment already fulfilled)"
        )

    return "fulfilled"


async def record_webhook_event(
    pg: AsyncSession, event_id: str, payload: dict[str, Any]
) -> bool:
    """
    Insert idempotency row. Returns True if this is the first time we see event_id.
    """
    r = await pg.execute(
        text("""
            INSERT INTO billing_webhook_events (gateway, event_id, payload)
            VALUES ('razorpay', :eid, cast(:payload as jsonb))
            ON CONFLICT (gateway, event_id) DO NOTHING
            RETURNING id
        """),
        {"eid": event_id, "payload": json.dumps(payload)},
    )
    return r.fetchone() is not None
