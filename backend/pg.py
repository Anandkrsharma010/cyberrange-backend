from typing import AsyncGenerator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
    async_sessionmaker,
)
from contextlib import asynccontextmanager


from backend.config import get_settings

# ── Lazy engine ───────────────────────────────────────────────────────────────
# The engine is NOT created at import time. Any process that imports this
# module (workers, test runners, CLI tools) will not attempt a DB connection
# until get_engine() is first called.
#
# The API calls get_engine() explicitly inside the FastAPI lifespan so the
# pool is warmed before the first request arrives. Workers call it implicitly
# on their first poll cycle.

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker] = None


def get_engine() -> AsyncEngine:
    """
    Returns the process-wide SQLAlchemy async engine, creating it on first
    call. Subsequent calls return the same instance — no lock needed because
    the GIL makes the assignment atomic and all callers end up with the same
    object even in the (unlikely) concurrent first-call case.
    """
    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.DATABASE_URL,
            echo=False,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def close_engine() -> None:
    """
    Dispose the connection pool and release all DB connections.
    Call this from the FastAPI lifespan shutdown or worker teardown.
    Safe to call even if the engine was never initialised.
    """
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_pg() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that yields an AsyncSession per request.
    Calls get_engine() internally so it triggers lazy initialisation
    on the first request after startup.
    """
    get_engine()  # ensure engine + factory exist
    async with _session_factory() as session:
        yield session

@asynccontextmanager
async def get_session():
    """Async context manager for use in workers. Usage: async with get_session() as session:"""
    get_engine()
    async with _session_factory() as session:
        yield session