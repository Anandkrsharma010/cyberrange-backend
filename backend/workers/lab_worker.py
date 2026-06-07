import asyncio
import json
import logging
import os
from sqlalchemy import text
from backend.pg import get_session
from backend.infrastructure.headscale import mint_preauth_key, delete_router_ssm_key
from backend.services.ops_feed import emit_lab_deployment_feed_for_transition
from backend.services.subnet_service import get_or_allocate_subnet

log = logging.getLogger("lab_worker")

POLL_INTERVAL = 5

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

CLAIM_SQL = """
UPDATE lab_deployments
SET status = 'provisioning',
    updated_at = now()
WHERE id = (
  SELECT id
  FROM lab_deployments
  WHERE status = 'queued'
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, user_id, content_id, lab_type, terraform_workspace, expires_at;
"""


async def claim_job(session):
    res = await session.execute(text(CLAIM_SQL))
    row = res.fetchone()
    if row:
        await emit_lab_deployment_feed_for_transition(
            session,
            deployment_id=str(row.id),
            transition="provisioning",
            emitter="lab_provisioning_worker",
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


async def run_terraform(lab_type, headscale_key, deployment_id, user_id, tenant_subnet_cidr):
    lab_dir = LAB_MAPPING.get(lab_type, DEFAULT_LAB_DIR)

    if not os.path.exists(lab_dir):
        raise Exception(f"Lab directory not found: {lab_dir}")

    log.info(
        "[%s] Starting Terraform | lab_type=%s lab_dir=%s user_id=%s subnet=%s",
        deployment_id, lab_type, lab_dir, user_id, tenant_subnet_cidr,
    )

    lock = _TERRAFORM_LOCKS.get(lab_dir) or _TERRAFORM_LOCKS[DEFAULT_LAB_DIR]
    if lock.locked():
        log.info("[%s] Waiting for Terraform lock on %s...", deployment_id, lab_dir)

    async with lock:
        return await _run_terraform_subprocess(
            lab_dir, headscale_key, deployment_id, user_id, tenant_subnet_cidr, lab_type
        )


async def _run_terraform_subprocess(lab_dir, headscale_key, deployment_id, user_id, tenant_subnet_cidr, lab_type):
    cmd = [
        "bash", "./deploy-lab.sh",
        lab_type, deployment_id, user_id, tenant_subnet_cidr,
    ]

    subprocess_env = os.environ.copy()
    subprocess_env["HEADSCALE_SSM_PARAM"] = headscale_key

    log.debug("[%s] Subprocess command: %s", deployment_id, " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=lab_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subprocess_env,
    )

    log.info("[%s] Terraform process started (PID %s) — streaming output...", deployment_id, proc.pid)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    await asyncio.gather(
        _stream_pipe(proc.stdout, "stdout", deployment_id, stdout_lines),
        _stream_pipe(proc.stderr, "stderr", deployment_id, stderr_lines),
    )

    await proc.wait()
    return_code = proc.returncode

    log.info("[%s] Terraform process exited with code %s", deployment_id, return_code)

    if return_code != 0:
        stderr_tail = "\n".join(stderr_lines[-40:])
        raise Exception(
            f"Terraform failed in {lab_dir} (exit {return_code}).\n"
            f"--- STDERR (last 40 lines) ---\n{stderr_tail}"
        )

    stdout_text = "\n".join(stdout_lines)
    try:
        parsed = json.loads(stdout_text)
        log.info("[%s] Terraform outputs parsed successfully: keys=%s", deployment_id, list(parsed.keys()))
        return parsed
    except json.JSONDecodeError:
        log.warning("[%s] Could not parse stdout as JSON. Raw output:\n%s", deployment_id, stdout_text)
        return {"raw_output": stdout_text}


# ── DB helpers ────────────────────────────────────────────────────────────────

async def mark_running(session, deployment_id, outputs):
    instances = (
        (outputs.get("lab_summary") or {})
        .get("value", {})
        .get("instances", {})
    )
    public_ip = None
    private_ip = None
    if "subnet_router" in instances:
        public_ip = (instances.get("subnet_router") or {}).get("public_ip")
        private_ip = (instances.get("domain_controller") or {}).get("private_ip")
    elif "attacker" in instances:
        private_ip = (instances.get("attacker") or {}).get("private_ip")

    await session.execute(
        text("""
            UPDATE lab_deployments
            SET status = 'running',
                instance_public_ip = :public_ip,
                instance_private_ip = :private_ip,
                terraform_outputs = cast(:outputs as jsonb),
                updated_at = now()
            WHERE id = :id
        """),
        {
            "id": deployment_id,
            "public_ip": public_ip,
            "private_ip": private_ip,
            "outputs": json.dumps(outputs),
        },
    )
    await emit_lab_deployment_feed_for_transition(
        session,
        deployment_id=deployment_id,
        transition="running",
        emitter="lab_provisioning_worker",
    )
    await session.commit()
    log.info("[%s] Deployment marked as 'running' | public_ip=%s private_ip=%s", deployment_id, public_ip, private_ip)


async def mark_failed(session, deployment_id, error):
    error_str = str(error)[:4000]
    await session.execute(
        text("""
            UPDATE lab_deployments
            SET status = 'failed',
                error_message = :error,
                updated_at = now()
            WHERE id = :id
        """),
        {"id": deployment_id, "error": error_str},
    )
    await emit_lab_deployment_feed_for_transition(
        session,
        deployment_id=deployment_id,
        transition="failed",
        emitter="lab_provisioning_worker",
    )
    await session.commit()
    log.error("[%s] Deployment marked as 'failed' | error=%s", deployment_id, error_str)


# ── Main worker loop ──────────────────────────────────────────────────────────

async def lab_provisioning_worker(stop_event: asyncio.Event | None = None):
    """
    Main loop. Accepts an optional stop_event so the process entry point
    can signal a graceful shutdown between jobs.
    """
    log.info("Lab provisioning worker started (poll_interval=%ss)", POLL_INTERVAL)

    while True:
        if stop_event and stop_event.is_set():
            log.info("Stop event set — exiting provisioning worker loop.")
            return

        try:
            # ── Heartbeat + claim job (short-lived session) ───────────────────
            async with get_session() as session:
                await session.execute(
                    text("""
                        INSERT INTO worker_status (id, last_seen)
                        VALUES ('lab_worker', now())
                        ON CONFLICT (id)
                        DO UPDATE SET last_seen = now()
                    """)
                )
                await session.commit()
                job = await claim_job(session)

            # Session closed — sleep outside it so we don't hold a connection
            if not job:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            # ── Process claimed job (fresh session) ───────────────────────────
            deployment_id = str(job.id)
            user_id = str(job.user_id)
            lab_type = job.lab_type

            log.info(
                "[%s] Claimed job | lab_type=%s user_id=%s expires_at=%s",
                deployment_id, lab_type, user_id, job.expires_at,
            )

            async with get_session() as session:
                try:
                    tenant_subnet_cidr = await get_or_allocate_subnet(session, user_id)
                    log.info("[%s] Allocated subnet: %s", deployment_id, tenant_subnet_cidr)

                    acl_tags = ["tag:router"]
                    log.info("[%s] Minting Headscale pre-auth key (tags=%s)...", deployment_id, acl_tags)

                    ssm_param_name = await mint_preauth_key(
                        pg=session,
                        user_id=user_id,
                        key_type="router",
                        deployment_id=deployment_id,
                        expires_at=job.expires_at,
                        acl_tags=acl_tags,
                        reusable=False,
                        ephemeral=False,
                    )
                    log.info("[%s] Headscale key minted and stored in SSM: %s", deployment_id, ssm_param_name)

                    outputs = await run_terraform(
                        lab_type, ssm_param_name, deployment_id, user_id, tenant_subnet_cidr,
                    )

                    # T-09: SSM key is NOT deleted here — the EC2 instance may still be booting.
                    # The key expires automatically via SSM parameter TTL (set at mint time).
                    # The cleanup worker will also attempt deletion after lab expiry.
                    log.info("[%s] SSM key left for instance to consume at boot", deployment_id)

                    await mark_running(session, deployment_id, outputs)
                    log.info("[%s] ✅ Provisioning complete", deployment_id)

                except Exception as e:
                    log.exception("[%s] ❌ Provisioning failed: %s", deployment_id, e)
                    await session.rollback()
                    async with get_session() as fail_session:
                        await mark_failed(fail_session, deployment_id, e)

        except asyncio.CancelledError:
            log.info("Lab provisioning worker task cancelled.")
            return

        except Exception as loop_error:
            log.exception("[WORKER LOOP ERROR] %s", loop_error)
            await asyncio.sleep(5)