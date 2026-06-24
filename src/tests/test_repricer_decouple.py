"""Tests for issue #425 — decouple held-position re-pricer from scanner active-hours gate.

Verifies:
- build_weather_for_scanning filters out-of-window stations (KHOU regression)
- build_weather_for_pricing does NOT filter by active hours (out-of-window station returns WeatherState)
- Stop-loss fires during off-hours: KORD synthetic position at local hour 02:00
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytz
import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAKE_METAR = [{"temp": "20.0", "reportTime": datetime.now(timezone.utc).isoformat()}]
_FAKE_HIGH = (75.0, datetime.now(timezone.utc))

_WSSS_STATION_TUPLE = ("WSSS", 1.3644, 103.9915, "Singapore", "WSSS", "C", "Asia/Singapore")
_KHOU_STATION_TUPLE = ("KHOU", 29.6454, -95.2789, "Houston", "KHOU", "F", "America/Chicago")
_KORD_STATION_TUPLE = ("KORD", 41.9742, -87.9073, "Chicago", "KORD", "F", "America/Chicago")

_COMMON_PATCHES = dict(
    fetch_all_metars=("src.weather.builder.fetch_all_metars_today", _FAKE_METAR),
    compute_high=("src.weather.builder.compute_daily_high", _FAKE_HIGH),
    fetch_nws=("src.weather.builder.fetch_nws_forecast_high", 80.0),
    fetch_secondary=("src.weather.builder.fetch_secondary_forecast", 81.0),
    fetch_gfs=("src.weather.builder.fetch_gfs_forecast_high", None),
    fetch_hourly=("src.weather.builder.fetch_hourly_temp_now", None),
    now_local=("src.weather.builder.now_local", datetime.now(timezone.utc)),
    sunset_local=("src.weather.builder.sunset_local", datetime.now(timezone.utc)),
    get_source_priority=("src.weather.builder.get_source_priority", []),
    get_canonical_feeds=("src.weather.builder.get_canonical_station_feeds", []),
    get_weights=("src.weather.builder.get_weights", {"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}),
    refresh_weights=("src.weather.builder.refresh_weights", None),
    log_forecast=("src.weather.builder.log_forecast", None),
    compute_correction=("src.weather.builder.compute_correction", None),
    apply_residual=("src.weather.builder.apply_residual_correction", (None, {})),
)


def _apply_common_patches(extra_ctx_mgrs=()):
    """Return a list of (patch_target, return_value) for common builder mocks."""
    return [(path, rv) for path, rv in _COMMON_PATCHES.values()]


# ---------------------------------------------------------------------------
# KHOU regression: build_weather_for_scanning must exclude out-of-window stations
# ---------------------------------------------------------------------------

class TestBuildWeatherForScanningActiveHoursGate:
    """build_weather_for_scanning must respect the active-hours gate.

    KHOU regression: KHOU 2026-05-27 incident — overnight carryover was fooling
    bracket logic.  The scanner must NOT include stations outside their window.
    """

    def _build_scanning(self, station_tuple, local_hour: int):
        """Run build_weather_for_scanning with the local clock set to local_hour."""
        from src.weather.builder import build_weather_for_scanning

        station_code = station_tuple[0]
        tz_name = station_tuple[6]
        tz = pytz.timezone(tz_name)
        fake_local = tz.localize(datetime(2026, 5, 27, local_hour, 0))

        with (
            patch("src.weather.builder.STATION_TZ", {station_code: tz_name}),
            patch("src.weather.builder.STATION_ACTIVE_HOURS", {station_code: (6, 23)}),
            patch("src.weather.builder.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_local
            result = build_weather_for_scanning(stations=[station_tuple], db=None)
        return result

    def test_khou_at_local_23_excluded_from_scanner(self):
        """KHOU at local hour 23 must not appear in scanner output — KHOU 2026-05-27 regression."""
        result = self._build_scanning(_KHOU_STATION_TUPLE, local_hour=23)
        assert "KHOU" not in result, (
            "KHOU at local 23:00 must be excluded from scanner weather (active window 06:00-23:00). "
            "Regressing this breaks the fix for the 2026-05-27 bracket-blanketing incident."
        )

    def test_khou_at_local_03_excluded_from_scanner(self):
        """KHOU at local hour 03 must be excluded — overnight window."""
        result = self._build_scanning(_KHOU_STATION_TUPLE, local_hour=3)
        assert "KHOU" not in result, "KHOU at local 03:00 must be excluded from scanner weather"

    def test_station_inside_window_included_in_scanner(self):
        """A station at local hour 12 (inside 06-23 window) must be included."""
        from src.weather.builder import build_weather_for_scanning

        station_code = "KHOU"
        tz_name = "America/Chicago"
        tz = pytz.timezone(tz_name)
        fake_local = tz.localize(datetime(2026, 5, 27, 12, 0))

        with (
            patch("src.weather.builder.STATION_TZ", {station_code: tz_name}),
            patch("src.weather.builder.STATION_ACTIVE_HOURS", {station_code: (6, 23)}),
            patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR),
            patch("src.weather.builder.compute_daily_high", return_value=_FAKE_HIGH),
            patch("src.weather.builder.fetch_nws_forecast_high", return_value=80.0),
            patch("src.weather.builder.fetch_secondary_forecast", return_value=81.0),
            patch("src.weather.builder.fetch_gfs_forecast_high", return_value=None),
            patch("src.weather.builder.fetch_hourly_temp_now", return_value=None),
            patch("src.weather.builder.now_local", return_value=fake_local),
            patch("src.weather.builder.sunset_local", return_value=fake_local),
            patch("src.weather.builder.get_source_priority", return_value=[]),
            patch("src.weather.builder.get_canonical_station_feeds", return_value=[station_code]),
            patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}),
            patch("src.weather.builder.compute_correction", return_value=None),
            patch("src.weather.builder.apply_residual_correction", return_value=(None, {})),
            patch("src.weather.builder.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_local
            result = build_weather_for_scanning(stations=[_KHOU_STATION_TUPLE], db=None)
        assert "KHOU" in result, "KHOU at local 12:00 must be included in scanner weather"

    def test_health_out_reports_degraded_for_out_of_window(self):
        """health_out must record a 'degraded' entry for out-of-window stations."""
        from src.weather.builder import build_weather_for_scanning

        station_code = "KHOU"
        tz = pytz.timezone("America/Chicago")
        fake_local = tz.localize(datetime(2026, 5, 27, 23, 0))
        health: list = []

        with (
            patch("src.weather.builder.STATION_TZ", {station_code: "America/Chicago"}),
            patch("src.weather.builder.STATION_ACTIVE_HOURS", {station_code: (6, 23)}),
            patch("src.weather.builder.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_local
            build_weather_for_scanning(stations=[_KHOU_STATION_TUPLE], db=None, health_out=health)

        assert len(health) == 1
        assert health[0]["station"] == "KHOU"
        assert health[0]["status"] == "degraded"
        assert "active window" in health[0]["reason"]


# ---------------------------------------------------------------------------
# build_weather_for_pricing: no active-hours gate — out-of-window stations priced
# ---------------------------------------------------------------------------

class TestBuildWeatherForPricingNoGate:
    """build_weather_for_pricing must return WeatherState for out-of-window stations.

    The re-pricer runs 24h; the gate exists only for the scanner.
    """

    def _build_pricing(self, station_tuple, local_hour: int):
        """Run build_weather_for_pricing with local clock at local_hour.

        Note: build_weather_for_pricing does NOT check the local hour — it has
        no active-hours gate.  The local_hour parameter is accepted only for
        symmetry with _build_scanning tests, but does not affect the result.
        """
        from src.weather.builder import build_weather_for_pricing

        station_code = station_tuple[0]
        tz_name = station_tuple[6]

        with (
            patch("src.weather.builder.STATION_TZ", {station_code: tz_name}),
            patch("src.weather.builder.STATION_ACTIVE_HOURS", {station_code: (6, 23)}),
            patch("src.weather.builder.fetch_all_metars_today", return_value=_FAKE_METAR),
            patch("src.weather.builder.compute_daily_high", return_value=_FAKE_HIGH),
            patch("src.weather.builder.fetch_nws_forecast_high", return_value=80.0),
            patch("src.weather.builder.fetch_secondary_forecast", return_value=81.0),
            patch("src.weather.builder.fetch_gfs_forecast_high", return_value=None),
            patch("src.weather.builder.fetch_hourly_temp_now", return_value=None),
            patch("src.weather.builder.now_local", return_value=datetime.now(timezone.utc)),
            patch("src.weather.builder.sunset_local", return_value=datetime.now(timezone.utc)),
            patch("src.weather.builder.get_source_priority", return_value=[]),
            patch("src.weather.builder.get_canonical_station_feeds", return_value=[station_code]),
            patch("src.weather.builder.get_weights", return_value={"nws": 0.5, "open_meteo": 0.5, "gfs": 0.0}),
            patch("src.weather.builder.compute_correction", return_value=None),
            patch("src.weather.builder.apply_residual_correction", return_value=(None, {})),
        ):
            result = build_weather_for_pricing(stations=[station_tuple], db=None)
        return result

    def test_out_of_window_station_included_in_pricing(self):
        """An out-of-window station must still appear in pricing weather (no gate)."""
        result = self._build_pricing(_KHOU_STATION_TUPLE, local_hour=23)
        assert "KHOU" in result, (
            "build_weather_for_pricing must return WeatherState for KHOU at local 23:00 — "
            "the active-hours gate must NOT apply to the re-pricer."
        )

    def test_overnight_station_produces_valid_weather_state(self):
        """A station at 02:00 local must produce a valid WeatherState with current_high_f set."""
        result = self._build_pricing(_KORD_STATION_TUPLE, local_hour=2)
        assert "KORD" in result
        state = result["KORD"]
        assert state.current_high_f == 75.0, "WeatherState must carry the METAR-derived daily high"
        assert state.latest_temp_f is not None

    def test_build_weather_for_pricing_with_no_metar_returns_empty(self):
        """When METAR data is unavailable the station must be omitted (not crash)."""
        from src.weather.builder import build_weather_for_pricing

        with patch("src.weather.builder.fetch_all_metars_today", return_value=[]):
            result = build_weather_for_pricing(stations=[_WSSS_STATION_TUPLE], db=None)

        assert result == {}, "Empty METAR must produce empty dict, not a crash"


# ---------------------------------------------------------------------------
# Integration: stop-loss fires during off-hours (KORD at 02:00 local)
# ---------------------------------------------------------------------------

class TestStopLossFiresDuringOffHours:
    """Stop-loss must fire during overnight windows when fair_value < entry.

    Before issue #425, _log_open_position_snapshots received the scanner weather
    which excluded off-hours stations, producing NULL fair_value rows and
    silently no-op'ing stop-loss.  After the fix, pricing weather is always-on.
    """

    def _make_kord_weather_state(self):
        """Build a synthetic WeatherState for KORD that triggers stop-loss."""
        from src.model.envelope import WeatherState

        now = datetime.now(timezone.utc)
        return WeatherState(
            station="KORD",
            now_local=now,
            sunset_local=now,
            current_high_f=72.0,
            current_high_time=now,
            latest_temp_f=70.0,
            latest_temp_time=now,
            forecast_high_f=75.0,
            secondary_forecast_f=76.0,
            obs_bias_offset_f=None,
            deb_mu_f=74.0,
        )

    def _make_fills(self, station="KORD", bracket_low=73.0, bracket_high=76.0, price_cents=80):
        return [{
            "no_token_id": "tok_kord_test",
            "station": station,
            "bracket_low": bracket_low,
            "bracket_high": bracket_high,
            "price_cents": price_cents,
            "size_eur": 5.0,
            "ticker": "TEST",
            "question": "Will KORD hit 73-76F?",
            "side": "NO",
        }]

    def test_snapshot_fair_value_not_null_during_off_hours(self):
        """_log_open_position_snapshots must produce non-NULL fair_value when pricing weather supplied.

        This is the core regression: before #425, passing scanner weather (which
        excluded KORD at 02:00 local) left fair_value_now = None in every snapshot.
        """
        from src.execution.position_tracker import _log_open_position_snapshots

        pricing_weather = {"KORD": self._make_kord_weather_state()}
        fills = self._make_fills()
        token_id = "tok_kord_test"

        mock_positions = [dict(fills[0], **{"no_token_id": token_id})]

        mock_ob = {
            "bids": [{"price": "0.35", "size": "10"}],
            "asks": [{"price": "0.65", "size": "10"}],
        }

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
            snap_path = tf.name
        try:
            with (
                patch("src.execution.position_tracker._load_open_all_positions", return_value=mock_positions),
                patch("src.execution.position_tracker.get_orderbook", return_value=mock_ob),
                patch("src.execution.position_tracker.POSITION_SNAPSHOTS_JSONL", snap_path),
                patch("src.execution.position_tracker.rotated_path", return_value=snap_path),
                patch("src.execution.position_tracker.housekeep"),
            ):
                ts = datetime.now(timezone.utc).isoformat()
                states = _log_open_position_snapshots(pricing_weather, ts, db=None)

            assert len(states) == 1, "Expected one position state"
            snap = states[0]["snap"]
            assert snap["fair_value_now"] is not None, (
                "fair_value_now must not be None when pricing weather is supplied — "
                "this is the core fix for issue #425 (off-hours fair_value NULL gap)."
            )
            assert snap["weather_missing"] is False
        finally:
            os.unlink(snap_path)

    def test_snapshot_fair_value_null_when_weather_missing(self):
        """When weather dict is empty (no station entry), fair_value_now must be NULL.

        This is the pre-fix behaviour for comparison and must remain consistent
        so callers can detect the weather-missing case.
        """
        from src.execution.position_tracker import _log_open_position_snapshots

        empty_weather: dict = {}
        fills = self._make_fills()
        token_id = "tok_kord_empty"

        mock_positions = [dict(fills[0], **{"no_token_id": token_id})]
        mock_ob = {
            "bids": [{"price": "0.35", "size": "10"}],
            "asks": [{"price": "0.65", "size": "10"}],
        }

        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
            snap_path = tf.name
        try:
            with (
                patch("src.execution.position_tracker._load_open_all_positions", return_value=mock_positions),
                patch("src.execution.position_tracker.get_orderbook", return_value=mock_ob),
                patch("src.execution.position_tracker.POSITION_SNAPSHOTS_JSONL", snap_path),
                patch("src.execution.position_tracker.rotated_path", return_value=snap_path),
                patch("src.execution.position_tracker.housekeep"),
            ):
                ts = datetime.now(timezone.utc).isoformat()
                states = _log_open_position_snapshots(empty_weather, ts, db=None)

            assert len(states) == 1
            snap = states[0]["snap"]
            assert snap["fair_value_now"] is None, "fair_value must be None when weather is missing"
            assert snap["weather_missing"] is True
        finally:
            os.unlink(snap_path)

    def test_stop_loss_fires_with_off_hours_pricing_weather(self):
        """Stop-loss must fire when fair_value < entry, even during overnight window.

        Simulates KORD at local 02:00: pricing weather is available (always-on),
        fair_value_now < avg_entry, STOP_LOSS_CONSECUTIVE_POLLS strikes accumulated.
        """
        from src.execution.position_tracker import _check_stop_loss_exits
        from src.model.envelope import WeatherState
        from src.config import STOP_LOSS_CONSECUTIVE_POLLS

        now = datetime.now(timezone.utc)
        state = self._make_kord_weather_state()  # current_high=72, fair_value for [73-76] bracket ~low

        token_id = "tok_kord_sl"
        fills = self._make_fills(price_cents=75)  # entry at 75c

        # fair_value_now = 20 (bracket [73-76], high=72, temp=70 → NO unlikely → fair low)
        # avg_entry = 75 > 20 → stop-loss should fire
        snap = {
            "station": "KORD",
            "bracket_low": 73.0,
            "bracket_high": 76.0,
            "fair_value_now": 20,     # well below 75c entry
            "no_best_bid": 65,
            "no_best_bid_size": 20.0,
            "current_high": 72.0,
            "forecast_nws": 74.0,
            "forecast_secondary": 75.0,
        }
        position_states = [{"token_id": token_id, "fills": fills, "snap": snap}]

        mock_live_trader = MagicMock()
        mock_live_trader.sell_position_immediate.return_value = ("sell-order-001", 65)

        import importlib
        import sys

        # Ensure src.scripts.run is importable for the lazy import inside
        # _check_stop_loss_exits.  Stub it out if it can't fully import.
        if "src.scripts.run" not in sys.modules:
            import types
            run_stub = types.ModuleType("src.scripts.run")
            run_stub._append_live_trade = MagicMock()
            sys.modules["src.scripts.run"] = run_stub

        from src.execution import position_tracker as _pt

        _orig_sold = _pt._order_manager._sold_positions.copy()
        _orig_strikes = dict(_pt._order_manager._stop_loss_strikes)
        _orig_partial = dict(_pt._order_manager._partial_fill_shares)

        try:
            _pt._order_manager._sold_positions = set()
            _pt._order_manager._stop_loss_strikes = {}
            _pt._order_manager._partial_fill_shares = {}

            with (
                patch("src.execution.position_tracker.STOP_LOSS_CONSECUTIVE_POLLS", 1),
                patch("src.execution.position_tracker.STOP_LOSS_MIN_BID_CENTS", 10),
                patch("src.execution.position_tracker.STOP_LOSS_MIN_DEPTH_SHARES", 1.0),
                patch("src.execution.position_tracker.STOP_LOSS_MIN_BRACKET_PROXIMITY_F", 0),
                patch("src.execution.position_tracker.STOP_LOSS_RESPECT_FORECAST_OVERSHOOT", False),
                patch("src.execution.position_tracker.STOP_LOSS_SELL_AGGRESSION_CENTS", 2),
                patch("src.execution.position_tracker._record_sell_in_db"),
                patch("src.execution.position_tracker.estimate_fee_cents", return_value=1),
                patch.object(sys.modules["src.scripts.run"], "_append_live_trade", MagicMock()),
            ):
                _check_stop_loss_exits(
                    mock_live_trader,
                    now.isoformat(),
                    position_states,
                    db=None,
                    risk_manager=None,
                )
        finally:
            _pt._order_manager._sold_positions = _orig_sold
            _pt._order_manager._stop_loss_strikes = _orig_strikes
            _pt._order_manager._partial_fill_shares = _orig_partial

        mock_live_trader.sell_position_immediate.assert_called_once(), (
            "sell_position_immediate must be called when fair_value < entry during off-hours — "
            "stop-loss was silently no-op'd before issue #425 fix."
        )


# ---------------------------------------------------------------------------
# build_weather_for_scanning backward-compat: _build_weather delegates to it
# ---------------------------------------------------------------------------

class TestBuildWeatherBackwardCompat:
    """_build_weather() must still delegate to build_weather_for_scanning."""

    def test_build_weather_uses_active_hours_gate(self):
        """_build_weather (legacy) must exclude out-of-window stations via delegation."""
        from src.weather.builder import _build_weather

        station_code = "KHOU"
        tz = pytz.timezone("America/Chicago")
        fake_local = tz.localize(datetime(2026, 5, 27, 23, 0))

        with (
            patch("src.weather.builder.STATION_TZ", {station_code: "America/Chicago"}),
            patch("src.weather.builder.STATION_ACTIVE_HOURS", {station_code: (6, 23)}),
            patch("src.weather.builder.STATIONS", [_KHOU_STATION_TUPLE]),
            patch("src.weather.builder.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_local
            result = _build_weather(db=None)

        assert "KHOU" not in result, "_build_weather must exclude out-of-window stations (delegates to scanner)"
