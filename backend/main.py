"""
backend/main.py  (updated)

WIP: Branch feature/mainwebsite-ui-updates — do not merge to main without owner approval.
See repo root WIP_BRANCH_NOTICE.md.

Changes vs original:
- Redis connection opened/closed in lifespan.
- CloudWatch metric publisher task started in lifespan.
- GET /health/workers endpoint added — reads worker_status heartbeats.
- FastAPI docs disabled (set docs_url/redoc_url=None for production).
  To re-enable for local dev, set ENABLE_DOCS=true in .env.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from backend.config import get_settings
from backend.limiter import limiter
from backend.logging_config import setup_logging
from backend.pg import close_engine, get_engine, get_pg, _session_factory
from backend.routers import (
    auth,
    labs,
    tailnet,
    admin,
    course,
    course_invites,
    billing,
    catalog,
    workshops,
    public_invites,
    aws_labs,
)
from backend.utils.blocklist import close_redis
from backend.utils.cloudwatch import run_metric_publisher
from backend.utils.headscale_client import close_headscale_client
from datetime import datetime, timezone, timedelta
from backend.utils.security import decode_token

settings = get_settings()

# Worker is considered stale if last heartbeat is older than this
_WORKER_STALE_THRESHOLD_S = 60


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    setup_logging()
    log = logging.getLogger("main")

    get_engine()  # warm the DB connection pool

    # Start CloudWatch metric publisher as a background task
    stop_event = asyncio.Event()

    async def _pg_factory():
        """Thin async context manager wrapping the session factory."""
        from backend.pg import _session_factory
        async with _session_factory() as session:
            yield session

    publisher_task = asyncio.create_task(
        run_metric_publisher(stop_event, _pg_factory)
    )

    worker_tasks = []
    if os.environ.get("RUN_WORKERS_IN_APP") == "true":
        log.info("Starting background workers inside FastAPI app process...")
        from backend.workers.lab_worker import lab_provisioning_worker
        from backend.workers.lab_cleanup_worker import lab_cleanup_worker
        worker_tasks.append(asyncio.create_task(lab_provisioning_worker(stop_event)))
        worker_tasks.append(asyncio.create_task(lab_cleanup_worker(stop_event)))

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    stop_event.set()
    
    # Wait for all background tasks to finish
    all_tasks = [publisher_task] + worker_tasks
    for task in all_tasks:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()

    await close_engine()
    await close_headscale_client()
    await close_redis()


app = FastAPI(
    title="CyberRange API",
    version="1.0.0",
    lifespan=lifespan,
    # Disable interactive docs in production.
    # Set ENABLE_DOCS=true in .env to re-enable for local development.
    docs_url="/docs" if settings.ENABLE_DOCS else None,
    redoc_url="/redoc" if settings.ENABLE_DOCS else None,
    openapi_url="/openapi.json" if settings.ENABLE_DOCS else None,
)

# ── Rate limiting ─────────────────────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ──────────────────────────────────────────────────────────────────────
if settings.CORS_ALLOWED_ORIGINS:
    import json
    origins = []
    val = settings.CORS_ALLOWED_ORIGINS.strip()
    if val.startswith("[") and val.endswith("]"):
        try:
            origins = json.loads(val)
        except Exception:
            pass
    if not origins:
        origins = [x.strip() for x in val.split(",") if x.strip()]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(public_invites.router)
app.include_router(labs.router)
app.include_router(tailnet.router)
app.include_router(admin.router)
app.include_router(course.router)
app.include_router(course_invites.router)
app.include_router(billing.router)
app.include_router(billing.webhook_router)
app.include_router(catalog.router)
app.include_router(workshops.router)
app.include_router(aws_labs.router)


# ── Health endpoints ──────────────────────────────────────────────────────────

@app.get("/", tags=["ops"])
async def root():
    return {"status": "ok", "service": "CyberRange API", "version": "84c80db"}


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}


class ValidateTokenRequest(BaseModel):
    token: str


@app.post("/api/validate-token", tags=["auth"])
async def validate_token(
    body: ValidateTokenRequest,
    pg: AsyncSession = Depends(get_pg)
):
    try:
        payload = await decode_token(body.token)
    except Exception as e:
        return {
            "valid": False,
            "error": "Invalid or expired token"
        }
    
    user_id = payload.get("sub")
    if not user_id:
        return {
            "valid": False,
            "error": "Invalid token subject"
        }

    # Fetch user email
    user_res = await pg.execute(
        text("SELECT email FROM users WHERE id = :uid LIMIT 1"),
        {"uid": user_id}
    )
    user_row = user_res.fetchone()
    if not user_row:
        return {
            "valid": False,
            "error": "User not found"
        }
    
    email = user_row.email

    # Fetch entitlement for AWS lab
    ent_res = await pg.execute(
        text("""
            SELECT id, valid_until, status
            FROM entitlements
            WHERE user_id = :user_id AND content_id = :content_id
            LIMIT 1
        """),
        {"user_id": user_id, "content_id": "c7e66c0d-d421-4f9e-a89c-5b23e7f80da3"}
    )
    ent_row = ent_res.fetchone()
    
    if not ent_row or ent_row.status != "active":
        return {
            "valid": False,
            "error": "No active entitlement for AWS Security Labs"
        }

    valid_until = ent_row.valid_until
    if valid_until:
        if valid_until.tzinfo is None:
            valid_until = valid_until.replace(tzinfo=timezone.utc)
        expires_timestamp = int(valid_until.timestamp() * 1000)
    else:
        expires_timestamp = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp() * 1000)

    return {
        "valid": True,
        "userId": user_id,
        "email": email,
        "labId": "aws-security-labs",
        "expiresAt": expires_timestamp,
        "purchaseId": str(ent_row.id)
    }


@app.get("/health/ready", tags=["ops"])
async def readiness():
    try:
        async for session in get_pg():
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        logging.getLogger("health").error("Readiness check failed: %s", exc)
        return Response(
            content='{"status": "unavailable", "detail": "database unreachable"}',
            status_code=503,
            media_type="application/json",
        )


@app.get("/health/workers", tags=["ops"])
async def worker_health():
    """
    Returns the heartbeat age for each background worker.
    Status is 'ok' if all workers checked in within the stale threshold,
    'degraded' if any worker is stale, 'unknown' if no heartbeat rows exist.

    Used by CloudWatch and monitoring dashboards.
    """
    log = logging.getLogger("health")

    try:
        async for session in get_pg():
            result = await session.execute(
                text("SELECT id, last_seen FROM worker_status")
            )
            rows = result.fetchall()
    except Exception as exc:
        log.error("Worker health check DB error: %s", exc)
        return Response(
            content='{"status": "unavailable", "detail": "database unreachable"}',
            status_code=503,
            media_type="application/json",
        )

    if not rows:
        return Response(
            content='{"status": "unknown", "detail": "no worker heartbeats found"}',
            status_code=503,
            media_type="application/json",
        )

    now = datetime.now(timezone.utc)
    workers = []
    any_stale = False

    for row in rows:
        last_seen = row.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        age_s = (now - last_seen).total_seconds()
        stale = age_s > _WORKER_STALE_THRESHOLD_S
        if stale:
            any_stale = True
        workers.append({
            "id": row.id,
            "last_seen": last_seen.isoformat(),
            "age_seconds": round(age_s, 1),
            "status": "stale" if stale else "ok",
        })

    overall = "degraded" if any_stale else "ok"
    status_code = 503 if any_stale else 200

    import json
    return Response(
        content=json.dumps({"status": overall, "workers": workers}),
        status_code=status_code,
        media_type="application/json",
    )