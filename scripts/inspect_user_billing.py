"""One-off: inspect user + billing rows by email. Run from Backend with PYTHONPATH=."""
import asyncio
import sys
from sqlalchemy import text

from backend.pg import close_engine, get_engine


async def main() -> None:
    email = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    if not email:
        print("Usage: PYTHONPATH=. python scripts/inspect_user_billing.py <email>")
        sys.exit(1)

    engine = get_engine()
    try:
        async with engine.connect() as conn:
            r = await conn.execute(
                text(
                    """
                    SELECT id, email, role, sso_provider, is_active, created_at
                    FROM users WHERE lower(email) = lower(:email)
                    """
                ),
                {"email": email},
            )
            users = r.mappings().all()
            print("=== USERS ===")
            for u in users:
                print(dict(u))
            if not users:
                print("No user found for", email)
                return
            uid = str(users[0]["id"])

            for label, q in [
                (
                    "PAYMENTS",
                    """
                    SELECT id, gateway, gateway_order_id, gateway_payment_id, amount, currency,
                           status, kind, created_at
                    FROM payments WHERE user_id = :uid ORDER BY created_at DESC
                    """,
                ),
                (
                    "PURCHASES",
                    """
                    SELECT id, content_id, payment_id, created_at
                    FROM purchases WHERE user_id = :uid ORDER BY created_at DESC
                    """,
                ),
                (
                    "ENTITLEMENTS",
                    """
                    SELECT id, content_id, status, valid_from, valid_until, created_at
                    FROM entitlements WHERE user_id = :uid ORDER BY created_at DESC
                    """,
                ),
            ]:
                r2 = await conn.execute(text(q), {"uid": uid})
                rows = r2.mappings().all()
                print(f"=== {label} ({len(rows)}) ===")
                for row in rows:
                    print(dict(row))

            r5 = await conn.execute(
                text(
                    """
                    SELECT id, gateway_order_id, status, gateway_payment_id, created_at
                    FROM payments WHERE user_id = :uid ORDER BY created_at DESC
                    """
                ),
                {"uid": uid},
            )
            pays = r5.mappings().all()
            print("=== WEBHOOK MATCH BY ORDER ID (per payment) ===")
            for pay in pays:
                oid = str(pay["gateway_order_id"])
                r6 = await conn.execute(
                    text(
                        """
                        SELECT id, gateway, event_id, processed_at
                        FROM billing_webhook_events
                        WHERE payload::text LIKE :pat
                        """
                    ),
                    {"pat": f"%{oid}%"},
                )
                ev = r6.mappings().all()
                print(f"order_id={oid} webhook_rows={len(ev)}")
                for e in ev:
                    print(dict(e))

            r7 = await conn.execute(
                text(
                    """
                    SELECT id, gateway, event_id, processed_at
                    FROM billing_webhook_events
                    ORDER BY processed_at DESC
                    LIMIT 20
                    """
                )
            )
            print("=== LAST 20 WEBHOOK EVENTS (global) ===")
            for row in r7.mappings().all():
                print(dict(row))
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
