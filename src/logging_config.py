"""Shared logging configuration for MeteoEdge.

Call setup_logging() once at each entrypoint (run.py main(), dashboard startup,
settle.py __main__). Subsequent getLogger() calls in any module will inherit
the root configuration.

Log level is INFO by default; set LOG_LEVEL=DEBUG in the environment to enable
debug output.
"""
import logging
import os


def setup_logging() -> None:
    """Configure the root logger with a standard format.

    Format: %(asctime)s %(levelname)s [%(name)s] %(message)s

    Level: INFO by default; DEBUG when LOG_LEVEL=DEBUG is set.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
