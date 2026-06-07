import asyncio
import logging
import os
from sqlalchemy import text
from backend.pg import get_session
from backend.infrastructure.headscale import delete_nodes_for_deployment
from backend.services.ops_feed import emit_lab_deployment_feed_for_transition

log = logging.getLogger("lab_cleanup_worker")

POLL_INTERVAL = 10

LAB_MAPPING = {
    "windows": "./lab-1",
    "wazuh": "./lab-1",
    "aws": "./lab-2",
    "lab-2": "./lab-2",
}

DEFAULT_LAB_DIR = "./lab-1"

_TERRAFORM_LOCKS: dict[str, asyncio.Semaphore] = {
    lab_dir: asyncio.Semaphore(1) for lab_dir in LAB_MAPPING.values()
}

CLAIM_EXPIRED_SQL = """
UPDATE lab_deployments
SET status = 'terminating',
    updated_at = now()
WHERE id = (
  SELECT id
  FROM lab_deployments
  WHERE status = 'running'
    AND expires_at <= now()
  ORDER BY expires_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, user_id, lab_type;
"""


async def claim_expired_job(session):
    res = await session.execute(text(CLAIM_EXPIRED_SQL))
    row = res.fetchone()
    if row:
        await emit_lab_deployment_feed_for_transition(
            session,
            deployment_id=str(row.id),
            transition="terminating",
            emitter="lab_cleanup_worker",
        )
        await session.commit()
    return row


# ── Real-time stdout/stderr streaming ────────────────────────────────────────

async def _stream_pipe(stream, label: str, deployment_id: str, lines: list):
    """Read a subprocess pipe line-by-line and log each line immediately."""
    async for raw in stream:
        line = raw.decode(errors="replace").rstrip()
        if line:
            log.info("[%s] [%s] %s", deployment_id, label, line)
            lines.append(line)


async def run_terraform_destroy(lab_type, deployment_id):
    lab_dir = LAB_MAPPING.get(lab_type, DEFAULT_LAB_DIR)

    if not os.path.exists(lab_dir):
        raise Exception(f"Lab directory not found: {lab_dir}")

    log.info("[%s] Starting Terraform destroy | lab_type=%s lab_dir=%s", deployment_id, lab_type, lab_dir)

    lock = _TERRAFORM_LOCKS.get(lab_dir) or _TERRAFORM_LOCKS[DEFAULT_LAB_DIR]
    if lock.locked():
        log.info("[%s] Waiting for Terraform lock on %s...", deployment_id, lab_dir)

    async with lock:
        await _run_destroy_subprocess(lab_dir, lab_type, deployment_id)


async def _run_destroy_subprocess(lab_dir, lab_type, deployment_id):
    cmd = ["bash", "./destroy-lab.sh", lab_type, deployment_id]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=lab_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    log.info("[%s] Terraform destroy process started (PID %s) — streaming output...", deployment_id, proc.pid)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    await asyncio.gather(
        _stream_pipe(proc.stdout, "stdout", deployment_id, stdout_lines),
        _stream_pipe(proc.stderr, "stderr", deployment_id, stderr_lines),
    )

    await proc.wait()
    return_code = proc.returncode

    log.info("[%s] Terraform destroy exited with code %s", deployment_id, return_code)

    if return_code != 0:
        stderr_tail = "\n".join(stderr_lines[-40:])
        raise Exception(
            f"Terraform destroy failed in {lab_dir} (exit {return_code}).\n"
            f"--- STDERR (last 40 lines) ---\n{stderr_tail}"
        )


# ── DB helpers ────────────────────────────────────────────────────────────────

async def mark_expired(session, deployment_id):
    await session.execute(
        text("""
            UPDATE lab_deployments
            SET status = 'expired',
                updated_at = now()
            WHERE id = :id
        """),
        {"id": deployment_id},
    )
    await emit_lab_deployment_feed_for_transition(
        session,
        deployment_id=deployment_id,
        transition="expired",
        emitter="lab_cleanup_worker",
    )
    await session.commit()
    log.info("[%s] Deployment marked as 'expired'", deployment_id)


async def mark_cleanup_failed(session, deployment_id, error):
    error_str = str(error)[:4000]
    await session.execute(
        text("""
            UPDATE lab_deployments
            SET status = 'cleanup_failed',
                error_message = :error,
                updated_at = now()
            WHERE id = :id
        """),
        {"id": deployment_id, "error": error_str},
    )
    await emit_lab_deployment_feed_for_transition(
        session,
        deployment_id=deployment_id,
        transition="cleanup_failed",
        emitter="lab_cleanup_worker",
    )
    await session.commit()
    log.error("[%s] Cleanup marked as 'cleanup_failed' | error=%s", deployment_id, error_str)


# ── Main worker loop ──────────────────────────────────────────────────────────

async def lab_cleanup_worker(stop_event: asyncio.Event | None = None):
    """
    Main loop. Accepts an optional stop_event so the process entry point
    can signal a graceful shutdown between jobs.

    Claims only ``running`` rows past ``expires_at``. ``cleanup_failed`` is not
    re-claimed automatically (avoids log spam); fix the cause and set status
    manually if a retry is needed.
    """
    log.info("Lab cleanup worker started (poll_interval=%ss)", POLL_INTERVAL)

    while True:
        if stop_event and stop_event.is_set():
            log.info("Stop event set — exiting cleanup worker loop.")
            return

        try:
            # ── Heartbeat + claim job (short-lived session) ───────────────────
            async with get_session() as session:
                await session.execute(
                    text("""
                        INSERT INTO worker_status (id, last_seen)
                        VALUES ('lab_cleanup_worker', now())
                        ON CONFLICT (id)
                        DO UPDATE SET last_seen = now()
                    """)
                )
                await session.commit()
                job = await claim_expired_job(session)

            # Session closed — sleep outside it so we don't hold a connection
            if not job:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # ── Process claimed job (fresh session) ───────────────────────────
            deployment_id = str(job.id)
            user_id = str(job.user_id)
            lab_type = job.lab_type

            log.info(
                "[%s] Claimed expired job | lab_type=%s user_id=%s",
                deployment_id, lab_type, user_id,
            )

            async with get_session() as session:
                try:
                    await run_terraform_destroy(lab_type, deployment_id)

                    log.info("[%s] Deleting Headscale nodes for deployment...", deployment_id)
                    await delete_nodes_for_deployment(deployment_id)
                    log.info("[%s] Headscale nodes deleted", deployment_id)

                    await mark_expired(session, deployment_id)
                    log.info("[%s] Cleanup complete", deployment_id)

                except Exception as e:
                    log.exception("[%s] Cleanup failed: %s", deployment_id, e)
                    await mark_cleanup_failed(session, deployment_id, e)

        except asyncio.CancelledError:
            log.info("Lab cleanup worker task cancelled.")
            return

        except Exception as loop_error:
            log.exception("[CLEANUP LOOP ERROR] %s", loop_error)
            await asyncio.sleep(POLL_INTERVAL)