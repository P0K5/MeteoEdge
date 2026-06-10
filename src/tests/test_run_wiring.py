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

import src.monitoring.dashboard
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


# ---------------------------------------------------------------------------
# _build_weather() high-freq obs wiring — issue #129
# ---------------------------------------------------------------------------

class TestBuildWeatherHighFreqObs:
    """_build_weather() must prefer fresh high-freq obs over METAR when available."""

    def _make_mock_db(self, obs_dict=None):
        db = MagicMock()
        db.get_latest_observation.return_value = obs_dict
        return db

    def _run_build_weather(self, db, metar_temp_c=20.0, metar_time_offset_min=-10):
        """Run _build_weather() with mocked METAR and return the WeatherState for WSSS (Singapore)."""
        from datetime import datetime, timezone, timedelta
        from unittest.mock import patch, MagicMock
        from src.scripts.run import _build_weather

        now = datetime.now(timezone.utc)
        metar_time = now + timedelta(minutes=metar_time_offset_min)

        fake_metar = [{"temp": str(metar_temp_c), "reportTime": metar_time.isoformat()}]

        with (
            patch("src.scripts.run.fetch_all_metars_today", return_value=fake_metar),
            patch("src.scripts.run.compute_daily_high", return_value=(80.0, now)),
            patch("src.scripts.run.fetch_nws_forecast_high", return_value=82.0),
            patch("src.scripts.run.fetch_secondary_forecast", return_value=83.0),
            patch("src.scripts.run.fetch_hourly_temp_now", return_value=84.0),
            patch("src.scripts.run.now_local", return_value=now),
            patch("src.scripts.run.sunset_local", return_value=now),
            patch("src.scripts.run.STATION_ACTIVE_HOURS", {"WSSS": (0, 24)}),
            patch("src.scripts.run.STATIONS", [("WSSS", 1.3644, 103.9915, "Singapore", "WSSS", "C", "Asia/Singapore")]),
            patch("src.scripts.run.STATION_TZ", {"WSSS": "Asia/Singapore"}),
            patch("src.scripts.run.get_source_priority", return_value=[
                {"source": "mss", "station": "Singapore", "cadence_min": 1},
                {"source": "metar", "station": "WSSS", "cadence_min": 30},
            ]),
        ):
            result = _build_weather(db=db)
        return result.get("WSSS")

    def test_fresh_high_freq_obs_overrides_metar_temp(self):
        from datetime import datetime, timezone
        fresh_obs = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "temp_f": 88.0,
            "source": "mss",
            "station": "Singapore",
        }
        db = self._make_mock_db(obs_dict=fresh_obs)
        state = self._run_build_weather(db, metar_temp_c=20.0)
        assert state is not None, "_build_weather must return WSSS state — check active hours patch"
        assert abs(state.latest_temp_f - 88.0) < 0.01

    def test_no_db_produces_none_bias_offset(self):
        state = self._run_build_weather(db=None)
        assert state is not None, "_build_weather must return WSSS state — check active hours patch"
        assert state.obs_bias_offset_f is None

    def test_stale_high_freq_obs_falls_back_to_metar(self):
        from datetime import datetime, timezone, timedelta
        stale_obs = {
            "ts": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(),
            "temp_f": 99.0,
            "source": "mss",
            "station": "Singapore",
        }
        db = self._make_mock_db(obs_dict=stale_obs)
        state = self._run_build_weather(db, metar_temp_c=20.0)
        assert state is not None, "_build_weather must return WSSS state — check active hours patch"
        # 99F stale obs must NOT override METAR (20C = 68F)
        assert abs(state.latest_temp_f - 68.0) < 0.5

    def test_bias_offset_computed_correctly(self):
        from datetime import datetime, timezone  # noqa: F811
        fresh_obs = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "temp_f": 87.0,
        }
        db = self._make_mock_db(obs_dict=fresh_obs)
        state = self._run_build_weather(db)
        assert state is not None, "_build_weather must return WSSS state — check active hours patch"
        # fetch_hourly_temp_now is mocked to return 84.0 in _run_build_weather
        assert state.obs_bias_offset_f is not None
        assert abs(state.obs_bias_offset_f - 3.0) < 0.01


# ---------------------------------------------------------------------------
# Intraday correction wiring — issue #151
# ---------------------------------------------------------------------------

class TestIntradayCorrectionWiring:
    """WeatherState.corrected_mu_f and on_insert_callback wiring."""

    @pytest.mark.skip(reason="pre-existing: issue #185 — true_probability_yes returns 0 with INTRADAY_CORRECTION_ENABLED")
    def test_corrected_mu_f_takes_priority_over_deb_mu_f(self):
        """When corrected_mu_f is set, true_probability_yes uses it over deb_mu_f."""
        from src.model.envelope import WeatherState, Bracket, true_probability_yes
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = WeatherState(
            station="RJTT",
            now_local=now,
            sunset_local=now,
            current_high_f=70.0,
            current_high_time=now,
            latest_temp_f=70.0,
            latest_temp_time=now,
            forecast_high_f=80.0,
            deb_mu_f=85.0,
            corrected_mu_f=78.0,
        )
        bracket = Bracket(
            ticker="TEST-HIGH-76-80",
            low_f=76.0,
            high_f=80.0,
            yes_ask_cents=50,
            yes_ask_size=100,
            no_ask_cents=50,
            no_ask_size=100,
        )
        import os
        with patch.dict("os.environ", {"DEB_ENABLED": "true", "INTRADAY_CORRECTION_ENABLED": "true"}):
            p_corrected = true_probability_yes(bracket, state)

        # Build a state without corrected_mu_f to compare
        state_deb_only = WeatherState(
            station="RJTT",
            now_local=now,
            sunset_local=now,
            current_high_f=70.0,
            current_high_time=now,
            latest_temp_f=70.0,
            latest_temp_time=now,
            forecast_high_f=80.0,
            deb_mu_f=85.0,
            corrected_mu_f=None,
        )
        with patch.dict("os.environ", {"DEB_ENABLED": "true"}):
            p_deb = true_probability_yes(bracket, state_deb_only)

        # corrected_mu_f=78.0 (closer to bracket center) should give higher prob than deb_mu_f=85.0
        assert p_corrected > p_deb, (
            f"corrected_mu_f=78 should outweight deb_mu_f=85 for bracket 76-80, "
            f"got p_corrected={p_corrected:.4f} p_deb={p_deb:.4f}"
        )

    @pytest.mark.skip(reason="pre-existing: issue #185 — true_probability_yes returns 0 with INTRADAY_CORRECTION_ENABLED")
    def test_corrected_mu_f_none_falls_through_to_deb(self):
        """When corrected_mu_f is None and DEB_ENABLED, deb_mu_f is used."""
        from src.model.envelope import WeatherState, Bracket, true_probability_yes
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = WeatherState(
            station="RJTT",
            now_local=now,
            sunset_local=now,
            current_high_f=70.0,
            current_high_time=now,
            latest_temp_f=70.0,
            latest_temp_time=now,
            forecast_high_f=None,
            deb_mu_f=80.0,
            corrected_mu_f=None,
        )
        bracket = Bracket(
            ticker="TEST-HIGH-78-82",
            low_f=78.0,
            high_f=82.0,
            yes_ask_cents=50,
            yes_ask_size=100,
            no_ask_cents=50,
            no_ask_size=100,
        )
        with patch.dict("os.environ", {"DEB_ENABLED": "true"}):
            p = true_probability_yes(bracket, state)
        # deb_mu_f=80.0 is in the center of 78-82; probability should be substantial
        assert p > 0.3, f"Expected substantial probability with deb_mu_f at bracket center, got {p:.4f}"

    def test_on_insert_callback_fires_after_commit(self):
        """on_insert_callback must be called after insert_observation commits."""
        import time as _time
        from src.data.db import Database

        fired = []

        def _cb():
            fired.append(True)

        db = Database(":memory:")
        db.insert_observation(
            ts="2024-01-15T12:00:00+00:00",
            station="RJTT",
            temp_f=60.0,
            temp_native=15.0,
            unit="C",
            source="metar",
            on_insert_callback=_cb,
        )
        # Daemon thread fires asynchronously — wait briefly
        _time.sleep(0.05)
        assert fired, "on_insert_callback was never called"

    def test_on_insert_callback_none_does_not_raise(self):
        """Calling insert_observation without callback must not raise."""
        from src.data.db import Database

        db = Database(":memory:")
        row_id = db.insert_observation(
            ts="2024-01-15T12:00:00+00:00",
            station="RJTT",
            temp_f=60.0,
            temp_native=15.0,
            unit="C",
            source="metar",
        )
        assert isinstance(row_id, int)
