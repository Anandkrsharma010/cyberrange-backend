# backend/logging_config.py
"""
Centralised logging configuration for the CyberRange API and workers.

All three processes (API, lab worker, cleanup worker) call setup_logging()
once at startup. After that, every module uses the standard:

    import logging
    log = logging.getLogger(__name__)   # or a named logger

and gets structured JSON output automatically — no per-module changes needed.

Output format (one JSON object per line):
    {
      "timestamp": "2025-03-06T10:23:41.123456Z",
      "level":     "INFO",
      "logger":    "auth",
      "message":   "SSO login successful",
      "provider":  "google",        ← extra fields passed to log.info(...)
      "user_id":   "uuid-..."
    }

Why JSON logs?
- CloudWatch Logs Insights, Loki, and Datadog can query individual fields
  without regex parsing.
- Structured fields (deployment_id, user_id, lab_type) make it trivial to
  trace a single deployment end-to-end across API and worker log streams.
- No information is lost vs plain-text — the message field still exists.

Dependency: python-json-logger (add to requirements.txt):
    python-json-logger==2.0.7
"""

import logging
import sys
from pythonjsonlogger import jsonlogger


class _CyberRangeFormatter(jsonlogger.JsonFormatter):
    """
    Extends the base JsonFormatter with:
    - A consistent field order (timestamp first, then level/logger/message).
    - Renamed fields: 'asctime' → 'timestamp', 'levelname' → 'level',
      'name' → 'logger' so downstream tooling doesn't need field aliases.
    - UTC timestamps in ISO-8601 format.
    """

    def add_fields(self, log_record: dict, record: logging.LogRecord, message_dict: dict) -> None:
        super().add_fields(log_record, record, message_dict)

        # Rename built-in fields to cleaner names
        log_record["timestamp"] = log_record.pop("asctime", None)
        log_record["level"]     = log_record.pop("levelname", record.levelname)
        log_record["logger"]    = log_record.pop("name", record.name)

        # Remove redundant fields that JsonFormatter includes by default
        log_record.pop("exc_info", None)


def setup_logging(level: int = logging.INFO) -> None:
    """
    Configure the root logger with a JSON handler writing to stdout.

    Call this exactly once per process, at the very top of the entrypoint
    (before any other imports that might trigger logging), so that every
    logger created anywhere in the process inherits this configuration.

    Args:
        level: Minimum log level. Defaults to INFO. Pass logging.DEBUG
               during local development if needed.

    Usage:
        # API (main.py lifespan):
        setup_logging()

        # Workers (run_lab_worker.py, run_lab_cleanup_worker.py):
        setup_logging()
        log = logging.getLogger("run_lab_worker")
    """
    formatter = _CyberRangeFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S.%fZ",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Remove any handlers that basicConfig or uvicorn may have already added
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Quiet noisy third-party loggers that would otherwise flood the stream
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)