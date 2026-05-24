from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

VPC_OCTET_PREFIX = "10.20."
RESERVED_OCTETS = {0, 1}  # 10.20.0.0/24 and 10.20.1.0/24 reserved
MIN_TENANT_OCTET = 2
MAX_TENANT_OCTET = 254  # keep 255 unused


async def get_or_allocate_subnet(pg: AsyncSession, user_id: str) -> str:

    # 1) Return existing allocation if present
    result = await pg.execute(
        text("""
            SELECT subnet_cidr
            FROM subnet_allocations
            WHERE user_id = :uid
        """),
        {"uid": user_id},
    )
    row = result.fetchone()
    if row:
        return row.subnet_cidr

    # 2) Allocate new subnet with row lock for concurrency safety
    try:
        result = await pg.execute(
            text("""
                SELECT last_assigned_octet
                FROM subnet_tracker
                WHERE id = 'counter'
                FOR UPDATE
            """)
        )
        tracker = result.fetchone()
        if not tracker:
            raise RuntimeError("subnet_tracker not initialized (missing row id='counter')")

        current_octet = tracker.last_assigned_octet
        next_octet = max(current_octet + 1, MIN_TENANT_OCTET)

        while next_octet in RESERVED_OCTETS:
            next_octet += 1

        if next_octet > MAX_TENANT_OCTET:
            raise RuntimeError("Subnet pool exhausted for VPC 10.20.0.0/16")

        subnet_cidr = f"{VPC_OCTET_PREFIX}{next_octet}.0/24"

        await pg.execute(
            text("""
                UPDATE subnet_tracker
                SET last_assigned_octet = :octet
                WHERE id = 'counter'
            """),
            {"octet": next_octet},
        )

        await pg.execute(
            text("""
                INSERT INTO subnet_allocations (user_id, subnet_cidr)
                VALUES (:uid, :cidr)
            """),
            {"uid": user_id, "cidr": subnet_cidr},
        )

        await pg.commit()
        return subnet_cidr

    except IntegrityError:
        # A concurrent request for the same user won the race and already
        # inserted a row. Roll back our transaction and return whatever they
        # committed — both callers end up with the correct subnet.
        await pg.rollback()

        result = await pg.execute(
            text("""
                SELECT subnet_cidr
                FROM subnet_allocations
                WHERE user_id = :uid
            """),
            {"uid": user_id},
        )
        row = result.fetchone()
        if not row:
            # Should be unreachable: IntegrityError means the row exists.
            raise RuntimeError(
                f"IntegrityError on subnet insert for user {user_id} "
                "but no allocation found on re-fetch — this is a bug."
            )
        return row.subnet_cidr