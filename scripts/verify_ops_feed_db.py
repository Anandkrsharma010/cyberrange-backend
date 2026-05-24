"""One-shot DB checks for operations_feed (Phase 2.4 automation). Run in WSL from Backend with venv active."""

import asyncio

from sqlalchemy import text

from backend.pg import get_session


async def main() -> None:
    async with get_session() as s:
        r = await s.execute(
            text(
                """
                SELECT 1 FROM pg_constraint c
                JOIN pg_class t ON c.conrelid = t.oid
                WHERE t.relname = 'operations_feed'
                  AND c.conname = 'operations_feed_read_state_chk'
                """
            )
        )
        print("read_state_chk_present:", r.fetchone() is not None)
        r2 = await s.execute(
            text(
                """
                SELECT COUNT(*) FROM operations_feed
                WHERE (is_read AND (read_at IS NULL OR read_by IS NULL))
                   OR (NOT is_read AND (read_at IS NOT NULL OR read_by IS NOT NULL))
                """
            )
        )
        print("inconsistent_row_count:", int(r2.scalar() or 0))
        r3 = await s.execute(text("SELECT COUNT(*) FROM operations_feed"))
        print("total_feed_rows:", int(r3.scalar() or 0))


if __name__ == "__main__":
    asyncio.run(main())
