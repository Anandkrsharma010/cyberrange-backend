import asyncio
import logging
import signal

from backend.logging_config import setup_logging
from backend.workers.lab_worker import lab_provisioning_worker
from backend.pg import get_engine, close_engine

# Configure JSON logging before any other module emits a log line.
setup_logging()
log = logging.getLogger("run_lab_worker")


_GRACEFUL_SHUTDOWN_TIMEOUT_S = 60


async def main():
    loop = asyncio.get_running_loop()

    get_engine()

    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Shutdown signal received — stopping lab worker gracefully.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    log.info("Lab provisioning worker process starting.")
    worker_task = asyncio.create_task(lab_provisioning_worker(stop_event))

    await stop_event.wait()
    log.info(
        "Stop event set — giving worker up to %ds to finish current Terraform job...",
        _GRACEFUL_SHUTDOWN_TIMEOUT_S,
    )

    try:

        await asyncio.wait_for(
            asyncio.shield(worker_task),
            timeout=_GRACEFUL_SHUTDOWN_TIMEOUT_S,
        )
        log.info("Lab provisioning worker finished cleanly within grace period.")
    except asyncio.TimeoutError:

        log.warning(
            "Grace period elapsed (%ds) — cancelling worker task. "
            "The in-flight Terraform job may leave the deployment in 'provisioning' state; "
            "the cleanup worker will not touch it — manual intervention may be needed.",
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
    log.info("Lab provisioning worker shut down cleanly.")


if __name__ == "__main__":
    asyncio.run(main())