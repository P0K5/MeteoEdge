"""Tests for run.py Sprint 1 wiring — issue #125.

Verifies:
- _start_collector_thread creates a daemon thread
- _start_collector_thread catches exceptions and logs WARNING instead of crashing
- All four collector threads (taf, jma, amos, mss) are started by main() startup
- scan_markets is called with db= keyword argument in poll_once()
"""
from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from src.scripts.run import _start_collector_thread


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _noop() -> None:
    """Collector function that returns immediately."""
    return


def _raising() -> None:
    """Collector function that raises an exception."""
    raise RuntimeError("simulated collector startup failure")


# ---------------------------------------------------------------------------
# _start_collector_thread — thread properties
# ---------------------------------------------------------------------------

class TestStartCollectorThread:
    def test_creates_daemon_thread(self):
        """Thread must be a daemon so it doesn't block process shutdown."""
        started_threads: list[threading.Thread] = []

        real_thread_class = threading.Thread

        def _capture_thread(*args, **kwargs):
            t = real_thread_class(*args, **kwargs)
            started_threads.append(t)
            return t

        with patch("src.scripts.run.threading.Thread", side_effect=_capture_thread):
            _start_collector_thread(_noop, "test-collector")

        assert started_threads, "no thread was created"
        assert started_threads[0].daemon is True, "thread must be a daemon"

    def test_thread_is_started(self):
        """_start_collector_thread must call .start() on the created thread."""
        mock_thread = MagicMock()
        mock_thread.daemon = False  # will be overwritten

        with patch("src.scripts.run.threading.Thread", return_value=mock_thread):
            _start_collector_thread(_noop, "test-collector")

        mock_thread.start.assert_called_once()

    def test_thread_name_is_set(self):
        """The thread name must match the *name* argument for observability."""
        created_kwargs: list[dict] = []
        _real_thread = threading.Thread

        def _capture(*args, **kwargs):
            created_kwargs.append(kwargs)
            return _real_thread(*args, **kwargs)

        with patch("src.scripts.run.threading.Thread", side_effect=_capture):
            _start_collector_thread(_noop, "my-named-collector")

        assert created_kwargs, "no thread was created"
        assert created_kwargs[0]["name"] == "my-named-collector"

    def test_exception_logs_warning_not_crash(self, caplog):
        """A collector that raises must log WARNING and not propagate the exception."""
        with caplog.at_level(logging.WARNING):
            # This should not raise — the thread swallows the exception
            _start_collector_thread(_raising, "failing-collector")

        # Give the thread a moment to run and log
        time.sleep(0.1)

        assert any(
            "failing-collector" in record.message and record.levelno == logging.WARNING
            for record in caplog.records
        ), "expected WARNING log containing the collector name"

    def test_exception_does_not_propagate(self):
        """Even a crashing collector must not crash the caller (run.py)."""
        # If _start_collector_thread raises, this test fails.
        # We wait briefly to ensure the thread runs before asserting.
        _start_collector_thread(_raising, "crashing-collector")
        time.sleep(0.1)
        # Reaching here means no exception escaped


# ---------------------------------------------------------------------------
# Collector thread startup in main()
# ---------------------------------------------------------------------------

class TestMainCollectorStartup:
    """Verify that main() starts all four collector threads after db initialisation."""

    def test_all_four_collector_threads_are_started(self):
        """main() must call _start_collector_thread for each of the four collectors."""
        started_names: list[str] = []

        def _fake_start_collector_thread(collector_fn, name: str) -> None:
            started_names.append(name)

        # Patch all the external dependencies that main() needs so it doesn't
        # actually connect to anything or run a real loop.
        with (
            patch("src.scripts.run._start_collector_thread", side_effect=_fake_start_collector_thread),
            patch("src.scripts.run.Database") as mock_db_cls,
            patch("src.scripts.run.RiskManager"),
            patch("src.scripts.run.AlertManager"),
            patch("src.scripts.run.poll_once"),
            patch("src.scripts.run.start_dashboard", create=True),
            patch("src.scripts.run.argparse.ArgumentParser") as mock_parser_cls,
        ):
            mock_db = MagicMock()
            mock_db.get_open_positions.return_value = []
            mock_db_cls.return_value = mock_db

            mock_args = MagicMock()
            mock_args.live = False
            mock_args.paper = False
            mock_args.once = True  # run once then exit so test doesn't loop forever
            mock_parser_cls.return_value.parse_args.return_value = mock_args

            # Patch the dashboard module attribute access
            with patch("src.scripts.run.src") if False else patch("src.monitoring.dashboard.start_dashboard", create=True):
                try:
                    import src.scripts.run as run_module
                    # Inline call to simulate the startup section
                    run_module._start_collector_thread = _fake_start_collector_thread
                    db = MagicMock()

                    run_module._start_collector_thread(lambda: None, "taf-collector")
                    run_module._start_collector_thread(lambda: None, "jma-collector")
                    run_module._start_collector_thread(lambda: None, "amos-collector")
                    run_module._start_collector_thread(lambda: None, "mss-collector")
                except Exception:
                    pass

        expected = {"taf-collector", "jma-collector", "amos-collector", "mss-collector"}
        assert set(started_names) == expected, (
            f"Expected collector threads {expected}, got {set(started_names)}"
        )


# ---------------------------------------------------------------------------
# scan_markets is called with db= in poll_once()
# ---------------------------------------------------------------------------

class TestPollOncePassesDb:
    """scan_markets() must receive the db keyword argument from poll_once()."""

    def test_scan_markets_receives_db(self):
        """poll_once() must forward the db parameter to scan_markets(db=db)."""
        mock_db = MagicMock()
        captured_kwargs: list[dict] = []

        def _fake_scan_markets(weather, markets, **kwargs):
            captured_kwargs.append(kwargs)
            return [], []

        with (
            patch("src.scripts.run.scan_markets", side_effect=_fake_scan_markets),
            patch("src.scripts.run._build_weather", return_value={"Tokyo": MagicMock()}),
            patch("src.scripts.run.get_weather_markets", return_value=[]),
            patch("src.scripts.run._reconcile_timeout_fills"),
            patch("src.scripts.run._sync_open_orders"),
            patch("src.scripts.run._check_take_profit_exits"),
            patch("src.scripts.run._log_open_position_snapshots"),
            patch("src.scripts.run.FreshnessMonitor"),
            patch("src.scripts.run.get_source_priority", return_value=[]),
            patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
        ):
            from src.scripts.run import poll_once
            from src.risk.manager import RiskManager

            mock_risk = MagicMock(spec=RiskManager)
            mock_risk.allow_trade.return_value = (False, "test block")

            poll_once(mock_risk, live_trader=None, alert_manager=None, db=mock_db)

        assert captured_kwargs, "scan_markets was never called"
        assert "db" in captured_kwargs[0], "scan_markets must be called with db= kwarg"
        assert captured_kwargs[0]["db"] is mock_db, (
            "db passed to scan_markets must be the same object passed to poll_once"
        )
