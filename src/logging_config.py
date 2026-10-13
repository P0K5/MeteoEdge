"""Shared logging configuration for MeteoEdge.

Call setup_logging() once at each entrypoint (run.py main(), dashboard startup,
settle.py __main__). Subsequent getLogger() calls in any module will inherit
the root configuration.

Log format: %(asctime)s %(levelname)s [%(process)d] [%(name)s] %(message)s
  - %(asctime)s: ISO timestamp in UTC (YYYY-MM-DDTHH:MM:SS), matching DB timestamps
  - %(levelname)s: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
  - %(process)d: Process ID for distinguishing concurrent processes
  - %(name)s: Logger name (module path)
  - %(message)s: Log message

Log level is INFO by default; set LOG_LEVEL=DEBUG in the environment to enable
debug output.
"""
import logging
import os
import time


def setup_logging() -> None:
    """Configure the root logger with a standard format.

    Format: %(asctime)s %(levelname)s [%(process)d] [%(name)s] %(message)s

    %(asctime)s is rendered in UTC (not host-local time) so log lines can be
    compared directly against UTC database timestamps.

    Level: INFO by default; DEBUG when LOG_LEVEL=DEBUG is set.
    """
    logging.Formatter.converter = time.gmtime

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(process)d] [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
