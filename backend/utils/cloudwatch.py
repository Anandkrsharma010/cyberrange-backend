"""
backend/utils/cloudwatch.py

CloudWatch custom metric publisher for worker liveness monitoring.

Publishes one metric per worker to the CyberRange/Workers namespace:
    WorkerHeartbeatAge  — seconds since last heartbeat (Seconds unit)

A CloudWatch alarm triggers when this value exceeds the threshold defined
in the monitoring Terraform module (default: 60 seconds).

Usage
-----
Called by the background task in main.py every METRIC_PUBLISH_INTERVAL_S seconds.

The publisher runs as a lightweight asyncio task — it does not block the
API event loop because boto3 calls are offloaded to a thread pool executor.

Configuration
-------------
No new env vars needed — uses AWS_REGION from existing settings.
AWS credentials come from the EC2 instance profile (same as Terraform).

Add to requirements.txt:
    boto3==1.40.48  (already present)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from backend.config import get_settings

log = logging.getLogger("cloudwatch")

METRIC_NAMESPACE = "CyberRange/Workers"
METRIC_NAME = "WorkerHeartbeatAge"
METRIC_PUBLISH_INTERVAL_S = 30  # publish every 30 seconds

# Worker IDs — must match the `id` values inserted into worker_status table
WORKER_IDS = ["lab_worker", "lab_cleanup_worker"]


def _publish_metrics_sync(region: str, worker_ages: dict[str, float]) -> None:
    """
    Synchronous boto3 call — runs in a thread pool executor.
    Publishes one MetricDatum per worker.
    """
    client = boto3.client("cloudwatch", region_name=region)
    metric_data = [
        {
            "MetricName": METRIC_NAME,
            "Dimensions": [{"Name": "WorkerId", "Value": worker_id}],
            "Value": age_seconds,
            "Unit": "Seconds",
        }
        for worker_id, age_seconds in worker_ages.items()
    ]

    if not metric_data:
        return

    client.put_metric_data(Namespace=METRIC_NAMESPACE, MetricData=metric_data)
    log.debug("Published %d CloudWatch metrics", len(metric_data))


async def publish_worker_metrics(pg) -> None:
    """
    Query the worker_status table and publish heartbeat age metrics to CloudWatch.
    Runs as a background task — errors are logged but never propagate.
    """
    from sqlalchemy import text  # avoid circular import

    settings = get_settings()

    try:
        result = await pg.execute(
            text("SELECT id, last_seen FROM worker_status")
        )
        rows = result.fetchall()

        now = datetime.now(timezone.utc)
        worker_ages: dict[str, float] = {}

        # Workers present in DB
        for row in rows:
            last_seen = row.last_seen
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            age = (now - last_seen).total_seconds()
            worker_ages[row.id] = age

        # Workers completely absent from DB (never started or crashed at init)
        for worker_id in WORKER_IDS:
            if worker_id not in worker_ages:
                # Use a very large age so the alarm fires immediately
                worker_ages[worker_id] = 86400.0  # 24 hours
                log.warning("Worker %s has no heartbeat row in worker_status", worker_id)

        # Offload boto3 to thread pool
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            _publish_metrics_sync,
            settings.AWS_REGION,
            worker_ages,
        )

        for worker_id, age in worker_ages.items():
            log.debug("Worker %s heartbeat age: %.1fs", worker_id, age)

    except (BotoCoreError, ClientError) as exc:
        log.error("CloudWatch publish failed: %s", exc)
    except Exception as exc:
        log.error("Unexpected error in metric publisher: %s", exc)


async def run_metric_publisher(stop_event: asyncio.Event, pg_factory) -> None:
    """
    Long-running background task. Publishes metrics every METRIC_PUBLISH_INTERVAL_S
    seconds until stop_event is set.

    Args:
        stop_event:  asyncio.Event — set on shutdown to stop the loop.
        pg_factory:  Callable that returns an async context manager yielding a DB session.
                     Pass `get_pg` from backend.pg.
    """
    log.info(
        "CloudWatch metric publisher started (interval=%ds)", METRIC_PUBLISH_INTERVAL_S
    )

    while not stop_event.is_set():
        try:
            async for pg in pg_factory():
                await publish_worker_metrics(pg)
                break
        except Exception as exc:
            log.error("Metric publisher iteration failed: %s", exc)

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=METRIC_PUBLISH_INTERVAL_S,
            )
        except asyncio.TimeoutError:
            pass  # Normal — keep looping

    log.info("CloudWatch metric publisher stopped.")