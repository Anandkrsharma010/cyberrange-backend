from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

async def get_next_user_id(pg: AsyncSession):
    """
    Get next user ID using Postgres sequence.
    Expects 'user_id_seq' to exist in Postgres.
    """
    result = await pg.execute(text("SELECT nextval('user_id_seq')"))
    seq = result.scalar()
    return f"usr_{seq:03d}"