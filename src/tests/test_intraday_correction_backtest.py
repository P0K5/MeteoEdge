"""Unit tests for src/scripts/intraday_correction_backtest.py."""
import math
import sys
from io import StringIO
from unittest.mock import MagicMock, patch

import pytest

from src.scripts.intraday_correction_backtest import (
    _build_daily_stats,
    _station_for_city,
    compute_rmse,
    main,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_correction_row(
    corrected_mu_f: float = 80.0,
    delta_f: float = 2.0,
    decay_factor: float = 0.5,
    obs_time: str = "2024-06-01T10:00:00+00:00",
) -> dict:
    return {
        "city": "Chicago",
        "date": "2024-06-01",
        "obs_time": obs_time,
        "obs_temp_f": 72.0,
        "model_temp_f": 70.0,
        "delta_f": delta_f,
        "corrected_mu_f": corrected_mu_f,
        "decay_factor": decay_factor,
    }


def _make_settlement(actual_high_f: float = 82.0, date_str: str = "2024-06-01") -> dict:
    return {
        "ts": f"{date_str}T20:00:00+00:00",
        "station": "KORD",
        "ticker": "TEST-TICKER",
        "bracket_low": 80.0,
        "bracket_high": 85.0,
        "actual_high_f": actual_high_f,
        "resolved_yes": 1,
        "market_final_price": 99,
        "source": "polymarket",
    }


# ---------------------------------------------------------------------------
# Test 1: test_no_data_exits_cleanly
# ---------------------------------------------------------------------------

class TestNoDataExitsCleanly:
    """When DB has no corrections, script logs the 'no data' message and exits 0."""

    def test_no_data_exits_cleanly(self, caplog):
        import logging
        mock_db = MagicMock()
        mock_db.get_intraday_corrections.return_value = []
        mock_db.get_settlements.return_value = []

        with caplog.at_level(logging.INFO):
            with (
                patch("src.scripts.intraday_correction_backtest.Database", return_value=mock_db),
                patch("sys.argv", ["backtest", "--city", "Chicago", "--days", "7"]),
                pytest.raises(SystemExit) as exc_info,
            ):
                main()

        assert exc_info.value.code == 0
        assert "No intraday_corrections data found" in caplog.text
        assert "Chicago" in caplog.text
        assert "7" in caplog.text

    def test_no_data_message_format(self, caplog):
        """The no-data message includes city name and days count."""
        import logging
        mock_db = MagicMock()
        mock_db.get_intraday_corrections.return_value = []
        mock_db.get_settlements.return_value = []

        with caplog.at_level(logging.INFO):
            with (
                patch("src.scripts.intraday_correction_backtest.Database", return_value=mock_db),
                patch("sys.argv", ["backtest", "--city", "Seoul", "--days", "14"]),
                pytest.raises(SystemExit) as exc_info,
            ):
                main()

        assert exc_info.value.code == 0
        assert "Seoul" in caplog.text
        assert "14" in caplog.text

    def test_corrections_without_matching_settlement(self, caplog):
        """Corrections exist but no settlement for those dates → no data."""
        import logging
        mock_db = MagicMock()
        mock_db.get_intraday_corrections.return_value = [_make_correction_row()]
        mock_db.get_settlements.return_value = []  # No settlements

        with caplog.at_level(logging.INFO):
            with (
                patch("src.scripts.intraday_correction_backtest.Database", return_value=mock_db),
                patch("sys.argv", ["backtest", "--city", "Chicago", "--days", "7"]),
                pytest.raises(SystemExit) as exc_info,
            ):
                main()

        assert exc_info.value.code == 0
        assert "No intraday_corrections data found" in caplog.text


# ---------------------------------------------------------------------------
# Test 2: test_rmse_calculation
# ---------------------------------------------------------------------------

class TestRmseCalculation:
    """Unit tests for compute_rmse() pure helper function."""

    def test_single_error(self):
        """RMSE of a single value equals that value."""
        assert compute_rmse([3.0]) == pytest.approx(3.0)

    def test_equal_errors(self):
        """RMSE of equal errors equals the error value."""
        assert compute_rmse([2.0, 2.0, 2.0]) == pytest.approx(2.0)

    def test_known_rmse(self):
        """Manual verification: RMSE([3, 4]) = sqrt((9+16)/2) = sqrt(12.5)."""
        expected = math.sqrt((9 + 16) / 2)
        assert compute_rmse([3.0, 4.0]) == pytest.approx(expected)

    def test_zero_errors(self):
        """RMSE of all-zero errors is 0."""
        assert compute_rmse([0.0, 0.0, 0.0]) == pytest.approx(0.0)

    def test_empty_raises(self):
        """compute_rmse raises ValueError for empty input."""
        with pytest.raises(ValueError, match="empty"):
            compute_rmse([])

    def test_build_daily_stats_errors(self):
        """_build_daily_stats produces correct corrected and baseline errors."""
        # corrected_mu_f = 82.0, delta_f = 2.0, decay_factor = 0.5
        # baseline_forecast = 82.0 - 2.0 * 0.5 = 81.0
        # actual = 83.0
        # corrected_error = |83.0 - 82.0| = 1.0
        # baseline_error  = |83.0 - 81.0| = 2.0
        row = _make_correction_row(corrected_mu_f=82.0, delta_f=2.0, decay_factor=0.5)
        result = _build_daily_stats([row], actual_high_f=83.0)

        assert result is not None
        assert result["corrected_error"] == pytest.approx(1.0)
        assert result["baseline_error"] == pytest.approx(2.0)
        assert result["corrected_mu_f"] == pytest.approx(82.0)
        assert result["baseline_forecast"] == pytest.approx(81.0)

    def test_build_daily_stats_uses_last_row(self):
        """When multiple correction rows exist, the last one is used."""
        rows = [
            _make_correction_row(corrected_mu_f=78.0, obs_time="2024-06-01T08:00:00+00:00"),
            _make_correction_row(corrected_mu_f=81.0, obs_time="2024-06-01T12:00:00+00:00"),
        ]
        result = _build_daily_stats(rows, actual_high_f=81.0)

        assert result is not None
        # Last row has corrected_mu_f=81.0 → corrected_error = |81.0 - 81.0| = 0.0
        assert result["corrected_mu_f"] == pytest.approx(81.0)
        assert result["corrected_error"] == pytest.approx(0.0)

    def test_build_daily_stats_empty_returns_none(self):
        """_build_daily_stats returns None for empty corrections list."""
        assert _build_daily_stats([], actual_high_f=80.0) is None

    def test_station_for_known_city(self):
        """_station_for_city returns correct METAR code for known city."""
        assert _station_for_city("Chicago") == "KORD"
        assert _station_for_city("Miami") == "KMIA"

    def test_station_for_unknown_city(self):
        """_station_for_city returns None for unknown city."""
        assert _station_for_city("UnknownCity_XYZ_99") is None


# ---------------------------------------------------------------------------
# Test 3: test_report_shows_improvement
# ---------------------------------------------------------------------------

class TestReportShowsImprovement:
    """With mock data where corrected_mu_f is closer to actual, report shows improvement."""

    def _run_with_mock_data(self, caplog, city="Chicago", days=3):
        """Run main() with injected DB data that shows correction improvement."""
        import logging
        mock_db = MagicMock()

        # Build correction rows where corrected is closer to actual than baseline
        # corrected_mu_f = 82.0, delta_f = 2.0, decay_factor = 0.5
        # baseline = 82.0 - 2.0 * 0.5 = 81.0
        # actual = 83.0
        # corrected_error = 1.0, baseline_error = 2.0 → improvement
        correction_rows = [
            _make_correction_row(corrected_mu_f=82.0, delta_f=2.0, decay_factor=0.5)
        ]

        from datetime import date, timedelta
        today = date.today()

        def fake_get_corrections(city_arg, date_arg):
            # Return corrections for every queried date
            return correction_rows

        def fake_get_settlements(station_arg, since_arg):
            # Return a settlement for each of the last `days` dates
            results = []
            for i in range(days):
                d = (today - timedelta(days=days - i)).isoformat()
                results.append(_make_settlement(actual_high_f=83.0, date_str=d))
            return results

        mock_db.get_intraday_corrections.side_effect = fake_get_corrections
        mock_db.get_settlements.side_effect = fake_get_settlements

        with caplog.at_level(logging.INFO):
            with (
                patch("src.scripts.intraday_correction_backtest.Database", return_value=mock_db),
                patch("sys.argv", ["backtest", f"--city", city, "--days", str(days)]),
            ):
                main()

        return caplog.text

    def test_report_shows_improvement(self, caplog):
        """Report shows positive improvement when corrected < baseline RMSE."""
        log_text = self._run_with_mock_data(caplog)

        assert "Overall baseline RMSE:" in log_text
        assert "Overall corrected RMSE:" in log_text
        assert "Improvement:" in log_text

        # Extract improvement line and verify it's positive
        for line in log_text.splitlines():
            if "Improvement:" in line:
                # The improvement value should be positive (corrected < baseline)
                # baseline_error=2.0, corrected_error=1.0, so improvement = 2.0-1.0 = 1.0
                assert "1.00" in line or "1." in line
                break
        else:
            pytest.fail("Improvement line not found in report output")

    def test_report_header_contains_city_and_days(self, caplog):
        """Report header includes city name and days count."""
        log_text = self._run_with_mock_data(caplog)

        assert "Chicago" in log_text
        assert "3" in log_text

    def test_report_dates_with_data_count(self, caplog):
        """Report shows correct count of dates with data."""
        log_text = self._run_with_mock_data(caplog, days=3)

        assert "Dates with data: 3/3" in log_text

    def test_improvement_check_marker_present(self, caplog):
        """Rows with improvement (delta < 0) show the 'ok' marker."""
        log_text = self._run_with_mock_data(caplog)
        assert " ok" in log_text

    def test_no_improvement_case(self, caplog):
        """When baseline is better than corrected, improvement is negative."""
        import logging
        mock_db = MagicMock()

        # corrected_mu_f = 85.0, delta_f = 3.0, decay_factor = 1.0
        # baseline = 85.0 - 3.0 * 1.0 = 82.0
        # actual = 82.5
        # corrected_error = |82.5 - 85.0| = 2.5, baseline_error = |82.5 - 82.0| = 0.5
        # baseline WINS → improvement negative
        correction_rows = [
            _make_correction_row(corrected_mu_f=85.0, delta_f=3.0, decay_factor=1.0)
        ]

        from datetime import date, timedelta
        today = date.today()
        days = 2

        def fake_corrections(city_arg, date_arg):
            return correction_rows

        def fake_settlements(station_arg, since_arg):
            results = []
            for i in range(days):
                d = (today - timedelta(days=days - i)).isoformat()
                results.append(_make_settlement(actual_high_f=82.5, date_str=d))
            return results

        mock_db.get_intraday_corrections.side_effect = fake_corrections
        mock_db.get_settlements.side_effect = fake_settlements

        with caplog.at_level(logging.INFO):
            with (
                patch("src.scripts.intraday_correction_backtest.Database", return_value=mock_db),
                patch("sys.argv", ["backtest", "--city", "Chicago", "--days", str(days)]),
            ):
                main()

        log_text = caplog.text
        assert "Improvement:" in log_text
        # negative improvement → baseline was better
        for line in log_text.splitlines():
            if "Improvement:" in line:
                assert "-" in line
                break
        else:
            pytest.fail("Improvement line not found in report output")
