"""Tests for promotion gate prerequisites."""
import pytest
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime, timedelta, date as date_cls

from src.model.promotion_gate import check_promotion_prerequisites


def _db():
    """Return an in-memory database for testing."""
    from src.data.db import Database
    return Database(":memory:")


class TestPromotionGateAllPass:
    """Test case: all gates pass → promotable=True."""

    def test_all_gates_pass(self):
        """When all prerequisites are met, promotable=True and reason is empty."""
        mock_db = MagicMock()
        mock_db.get_forecast_log.return_value = [
            {"model": "nws", "date": datetime.now().date().isoformat()},
            {"model": "gfs", "date": datetime.now().date().isoformat()},
        ]
        mock_db.get_taf_windows.return_value = [
            {"valid_from": datetime.now().isoformat()} for _ in range(70)
        ]
        mock_db.get_observations.return_value = [
            {"source": "amos", "ts": datetime.now().isoformat()},
        ]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = [
            {"ticker": "t1", "side": "YES", "outcome": "filled"},
        ]

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "WSSS", "Singapore")

        assert result['promotable'] is True
        assert result['reason'] == ''
        assert result['climb_rate'] is True
        assert result['model_count'] is True
        assert result['taf_coverage'] is True
        assert result['secondary_obs'] is True
        assert result['has_settled_loss'] is True


class TestClimbRateGate:
    """Test climb-rate history gate."""

    def test_climb_rate_no_data(self):
        """Station with no climb-rate data fails the gate."""
        mock_db = MagicMock()
        mock_db.get_forecast_log.return_value = [
            {"model": "nws", "date": datetime.now().date().isoformat()},
            {"model": "gfs", "date": datetime.now().date().isoformat()},
        ]
        mock_db.get_taf_windows.return_value = [{"valid_from": "x"} for _ in range(70)]
        mock_db.get_observations.return_value = [{"source": "amos"}]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = []

        with patch("src.model.promotion_gate._check_climb_rate", return_value=False):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "ZGSZ", "Shenzhen")

        assert result['promotable'] is False
        assert result['climb_rate'] is False
        assert 'no climb-rate data' in result['reason']


class TestModelCountGate:
    """Test model count gate (≥2 models required)."""

    def test_only_one_model(self):
        """Station with only 1 model fails the gate."""
        mock_db = MagicMock()
        mock_db.get_forecast_log.return_value = [
            {"model": "nws", "date": datetime.now().date().isoformat()},
        ]
        mock_db.get_taf_windows.return_value = [{"valid_from": "x"} for _ in range(70)]
        mock_db.get_observations.return_value = [{"source": "amos"}]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = []

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=1):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "WSSS", "Singapore")

        assert result['promotable'] is False
        assert result['model_count'] is False
        assert '1/2' in result['reason']

    def test_zero_models(self):
        """Station with 0 models fails the gate."""
        mock_db = MagicMock()
        mock_db.get_forecast_log.return_value = []
        mock_db.get_taf_windows.return_value = [{"valid_from": "x"} for _ in range(70)]
        mock_db.get_observations.return_value = [{"source": "amos"}]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = []

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=0):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "KORD", "Chicago")

        assert result['promotable'] is False
        assert result['model_count'] is False

    def test_exactly_two_models_pass(self):
        """Station with exactly 2 models passes the gate."""
        mock_db = MagicMock()
        mock_db.get_forecast_log.return_value = [
            {"model": "nws", "date": datetime.now().date().isoformat()},
            {"model": "gfs", "date": datetime.now().date().isoformat()},
        ]
        mock_db.get_taf_windows.return_value = [{"valid_from": "x"} for _ in range(70)]
        mock_db.get_observations.return_value = [{"source": "amos"}]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = []

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "WMKK", "Kuala Lumpur")

        assert result['model_count'] is True


class TestTafCoverageGate:
    """Test TAF coverage gate (≥60 windows by default)."""

    def test_zero_taf_windows(self):
        """Station with 0 TAF windows fails the gate."""
        mock_db = MagicMock()
        mock_db.get_forecast_log.return_value = [
            {"model": "nws"}, {"model": "gfs"}
        ]
        mock_db.get_taf_windows.return_value = []
        mock_db.get_observations.return_value = [{"source": "amos"}]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = []

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=0):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "RKSI", "Seoul")

        assert result['promotable'] is False
        assert result['taf_coverage'] is False
        assert '0/60' in result['reason']

    def test_insufficient_taf_windows(self):
        """Station with < 60 TAF windows fails the gate."""
        mock_db = MagicMock()
        mock_db.get_taf_windows.return_value = [{"valid_from": f"2024-01-{i:02d}"} for i in range(1, 31)]

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=30):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "ZGSZ", "Shenzhen")

        assert result['promotable'] is False
        assert result['taf_coverage'] is False

    def test_exactly_min_taf_windows_pass(self):
        """Station with exactly 60 TAF windows passes the gate."""
        mock_db = MagicMock()
        mock_db.get_taf_windows.return_value = [{"valid_from": "x"} for _ in range(60)]

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=60):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "RKPK", "Busan")

        assert result['taf_coverage'] is True


class TestSecondaryObsGate:
    """Test secondary observation source gate (non-metar required)."""

    def test_no_secondary_obs(self):
        """Station with only METAR observations fails the gate."""
        mock_db = MagicMock()
        mock_db.get_observations.return_value = [
            {"source": "metar", "ts": datetime.now().isoformat()},
            {"source": "metar", "ts": datetime.now().isoformat()},
        ]

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=False):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "KORD", "Chicago")

        assert result['promotable'] is False
        assert result['secondary_obs'] is False
        assert 'no secondary observation source' in result['reason']

    def test_has_amos_source(self):
        """Station with AMOS observations passes the gate."""
        mock_db = MagicMock()
        mock_db.get_observations.return_value = [
            {"source": "amos", "ts": datetime.now().isoformat()},
        ]

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "WMKK", "Kuala Lumpur")

        assert result['secondary_obs'] is True

    def test_empty_observations(self):
        """Station with no observations fails the gate."""
        mock_db = MagicMock()
        mock_db.get_observations.return_value = []

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=False):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "ZGSZ", "Shenzhen")

        assert result['secondary_obs'] is False


class TestSettledLossGate:
    """Test settled loss gate (rejects pure win streaks)."""

    def test_no_settled_loss_pure_wins(self):
        """Station with 100% win rate but no losses fails the gate."""
        mock_db = MagicMock()
        mock_db.get_observations.return_value = [{"source": "amos"}]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = []

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=False):
                            result = check_promotion_prerequisites(mock_db, "WSSS", "Singapore")

        assert result['promotable'] is False
        assert result['has_settled_loss'] is False
        assert 'no settled loss' in result['reason']

    def test_has_at_least_one_loss(self):
        """Station with at least one loss passes the gate."""
        mock_db = MagicMock()
        mock_db.get_observations.return_value = [{"source": "amos"}]
        mock_db._conn = MagicMock()
        mock_db._conn.execute.return_value.fetchall.return_value = [
            {"ticker": "t1", "side": "YES"},
        ]

        with patch("src.model.promotion_gate._check_climb_rate", return_value=True):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=2):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=70):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=True):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=True):
                            result = check_promotion_prerequisites(mock_db, "MPMG", "Panama City")

        assert result['has_settled_loss'] is True


class TestDatabaseNone:
    """Test behavior when database is None."""

    def test_db_none_returns_failure(self):
        """When db is None, all gates fail and promotable=False."""
        result = check_promotion_prerequisites(None, "WSSS", "Singapore")

        assert result['promotable'] is False
        assert 'Database unavailable' in result['reason']


class TestMultipleGateFail:
    """Test case: multiple gates fail."""

    def test_multiple_gates_fail(self):
        """When multiple gates fail, reason includes all failures."""
        mock_db = MagicMock()

        with patch("src.model.promotion_gate._check_climb_rate", return_value=False):
            with patch("src.model.promotion_gate._count_distinct_models", return_value=1):
                with patch("src.model.promotion_gate._count_taf_windows", return_value=30):
                    with patch("src.model.promotion_gate._has_secondary_observation_source", return_value=False):
                        with patch("src.model.promotion_gate._has_settled_loss", return_value=False):
                            result = check_promotion_prerequisites(mock_db, "ZGSZ", "Shenzhen")

        assert result['promotable'] is False
        assert 'no climb-rate data' in result['reason']
        assert '1/2' in result['reason']
        assert '30/60' in result['reason']
        assert 'no secondary observation source' in result['reason']
        assert 'no settled loss' in result['reason']
