import asyncio
import logging
import signal

from backend.logging_config import setup_logging
from backend.workers.lab_cleanup_worker import lab_cleanup_worker
from backend.pg import close_engine

setup_logging()
log = logging.getLogger("run_lab_cleanup_worker")

_GRACEFUL_SHUTDOWN_TIMEOUT_S = 120


async def main():
    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Shutdown signal received — stopping cleanup worker gracefully.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    log.info("Lab cleanup worker process starting.")
    worker_task = asyncio.create_task(lab_cleanup_worker(stop_event))

    await stop_event.wait()
    log.info(
        "Stop event set — giving cleanup worker up to %ds to finish current Terraform destroy...",
        _GRACEFUL_SHUTDOWN_TIMEOUT_S,
    )

    try:

        await asyncio.wait_for(
            asyncio.shield(worker_task),
            timeout=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )
        log.info("Lab cleanup worker finished cleanly within grace period.")
    except asyncio.TimeoutError:

        log.warning(
            "Grace period elapsed (%ds) — cancelling cleanup worker task. "
            "The in-flight Terraform destroy may have left AWS resources running. "
            "Check 'terminating' deployments in lab_deployments and verify AWS console.",
            _GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    except asyncio.CancelledError:
        pass

    await close_engine()
    log.info("Lab cleanup worker shut down cleanly.")


if __name__ == "__main__":
    asyncio.run(main())