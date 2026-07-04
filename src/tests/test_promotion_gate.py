"""Tests for promotion gate prerequisites."""
import pytest
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime, timedelta, date as date_cls

from src.model.promotion_gate import (
    check_promotion_prerequisites,
    wilson_lower_bound,
    breakeven_win_rate,
    compute_promotion_bar,
)
from src.strategy.fee import estimate_fee_cents


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


# ----------------------------------------------------------------------
# Statistical promotion bar (issue #559)
# ----------------------------------------------------------------------

class TestWilsonLowerBound:
    """Wilson score lower-bound vs hand-computed cases (95% CI, z=1.96).

    Expected values below were derived by hand from the standard Wilson
    score interval formula:
        denom  = 1 + z^2/n
        center = phat + z^2/(2n)
        margin = z * sqrt((phat*(1-phat) + z^2/(4n)) / n)
        lower  = (center - margin) / denom
    and cross-checked against known reference values (e.g. 50/100 at 95% CI
    is a textbook example with lower bound ~0.4038). The test calls the real
    production function — it does not reimplement the formula.
    """

    def test_20_of_30(self):
        # phat=0.66667, z=1.96 -> hand-computed lower bound = 0.487797...
        result = wilson_lower_bound(wins=20, n=30, z=1.96)
        assert result == pytest.approx(0.487797, abs=1e-5)

    def test_50_of_100_textbook_case(self):
        # Well-known reference value: 50/100 at 95% Wilson CI has a lower
        # bound of approximately 0.4038.
        result = wilson_lower_bound(wins=50, n=100, z=1.96)
        assert result == pytest.approx(0.403830, abs=1e-5)

    def test_pure_wins_small_n_stays_below_one(self):
        # 30/30 (100% observed win rate): hand-computed lower bound = 0.886483.
        # Wilson correctly refuses to report near-certainty from a small pure
        # win streak -- this is the statistical fix for the #80 loophole
        # (>=5 trades / 100% WR was gameable by noise).
        result = wilson_lower_bound(wins=30, n=30, z=1.96)
        assert result == pytest.approx(0.886483, abs=1e-5)
        assert result < 1.0

    def test_zero_wins(self):
        # 0/30 -> lower bound is exactly 0.
        result = wilson_lower_bound(wins=0, n=30, z=1.96)
        assert result == pytest.approx(0.0, abs=1e-9)

    def test_zero_trades_returns_zero_not_error(self):
        assert wilson_lower_bound(wins=0, n=0) == 0.0


class TestBreakevenWinRate:
    """Break-even threshold must derive from entry price + the real fee model."""

    def test_60_cents_hand_computed(self):
        # fee(60) = max(1.0, 7*0.6*0.4) = max(1.0, 1.68) = 1.68
        # breakeven = (60 + 1.68) / 100 = 0.6168
        assert breakeven_win_rate(60) == pytest.approx(0.6168, abs=1e-9)

    def test_75_cents_hand_computed(self):
        # fee(75) = max(1.0, 7*0.75*0.25) = max(1.0, 1.3125) = 1.3125
        # breakeven = (75 + 1.3125) / 100 = 0.763125
        assert breakeven_win_rate(75) == pytest.approx(0.763125, abs=1e-9)

    def test_derives_from_real_fee_module(self):
        # Cross-check against the actual src.strategy.fee.estimate_fee_cents
        # output (not a re-implementation) for a case not hand-verified above.
        price = 92
        expected = (price + estimate_fee_cents(price)) / 100.0
        assert breakeven_win_rate(price) == pytest.approx(expected, abs=1e-12)

    def test_is_not_a_bare_half(self):
        # Regression guard against reverting to a hardcoded 0.5 bar --
        # the whole point of #559 is that the bar must clear costs.
        assert breakeven_win_rate(60) != pytest.approx(0.5, abs=1e-3)
        assert breakeven_win_rate(90) > 0.5


class _FakeDB:
    """Minimal fake DB for compute_promotion_bar — avoids per-call config reads
    by returning a fixed config snapshot, and serves bulk trades/settlements
    exactly like the real Database's get_trades/get_all_settlements."""

    def __init__(self, trades, settlements, config=None):
        self._trades = trades
        self._settlements = settlements
        self._config = config or {}

    def get_all_config(self):
        return self._config

    def get_trades(self, limit=50, mode=None, direction=None):
        return [t for t in self._trades if mode is None or t.get("mode") == mode]

    def get_all_settlements(self, since, direction=None):
        return self._settlements


def _trade(station, side, ticker, actual_price, ts="2026-06-01T12:00:00", mode="shadow",
           direction="high"):
    return {
        "station": station, "side": side, "ticker": ticker,
        "actual_price": actual_price, "ts": ts, "mode": mode, "direction": direction,
    }


def _settlement(ticker, resolved_yes):
    return {"ticker": ticker, "resolved_yes": resolved_yes}


class TestComputePromotionBar:
    """compute_promotion_bar() -- calls the real wilson_lower_bound/breakeven_win_rate
    internally; expected numbers below were independently verified against those
    functions' hand-computed cases above, not reimplemented inline."""

    def test_db_none_returns_empty_list(self):
        assert compute_promotion_bar(None) == []

    def test_green_eligible_station(self):
        # WSSS/NO: 30 settled trades, 29 wins (resolved_yes=0 for NO wins), avg
        # entry 65c. wilson_lower_bound(29,30)=0.833292 > breakeven(65)=0.665925,
        # and n=30 >= default PROMOTION_MIN_SETTLED_TRADES=30 -> green/eligible.
        trades = [_trade("WSSS", "NO", f"wsss-{i}", 65) for i in range(30)]
        settlements = [_settlement(f"wsss-{i}", resolved_yes=0) for i in range(29)]
        settlements.append(_settlement("wsss-29", resolved_yes=1))  # 1 loss
        db = _FakeDB(trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "WSSS" and r["side"] == "NO")

        assert row["n"] == 30
        assert row["wins"] == 29
        assert row["win_rate"] == pytest.approx(29 / 30)
        assert row["wilson_lower_bound"] == pytest.approx(0.833292, abs=1e-5)
        assert row["breakeven_win_rate"] == pytest.approx(0.665925, abs=1e-9)
        assert row["price_valid"] is True
        assert row["eligible"] is True
        assert row["status"] == "green"

    def test_amber_insufficient_sample_size(self):
        # ZGSZ/NO: only 10 settled trades, all wins, avg entry 65c.
        # wilson_lower_bound(10,10)=0.722460 > breakeven(65)=0.665925 (clears
        # the statistical bar) but n=10 < 30 -> amber, not eligible.
        trades = [_trade("ZGSZ", "NO", f"zgsz-{i}", 65) for i in range(10)]
        settlements = [_settlement(f"zgsz-{i}", resolved_yes=0) for i in range(10)]
        db = _FakeDB(trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "ZGSZ")

        assert row["n"] == 10
        assert row["wilson_lower_bound"] == pytest.approx(0.722460, abs=1e-5)
        assert row["eligible"] is False
        assert row["status"] == "amber"
        assert "10/30" in row["reason"]

    def test_red_does_not_clear_breakeven(self):
        # RKSI/NO: 30 settled trades, 18 wins (60%), avg entry 65c.
        # wilson_lower_bound(18,30)=0.423201 <= breakeven(65)=0.665925 -> red,
        # even though n=30 meets the minimum sample size.
        trades = [_trade("RKSI", "NO", f"rksi-{i}", 65) for i in range(30)]
        settlements = [_settlement(f"rksi-{i}", resolved_yes=0) for i in range(18)]
        settlements += [_settlement(f"rksi-{i}", resolved_yes=1) for i in range(18, 30)]
        db = _FakeDB(trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "RKSI")

        assert row["n"] == 30
        assert row["wilson_lower_bound"] == pytest.approx(0.423201, abs=1e-5)
        assert row["eligible"] is False
        assert row["status"] == "red"

    def test_amber_price_below_min_price_cents(self):
        # EGLC/NO: 35 settled trades, 30 wins, avg entry 50c (below the
        # default MIN_PRICE_CENTS=60). wilson_lower_bound(30,35)=0.706241 >
        # breakeven(50)=0.5175, and n=35 >= 30, so eligible=True per the
        # strict rule -- but status is downgraded to amber because the low
        # entry price means the shadow data may not reflect live conditions.
        trades = [_trade("EGLC", "NO", f"eglc-{i}", 50) for i in range(35)]
        settlements = [_settlement(f"eglc-{i}", resolved_yes=0) for i in range(30)]
        settlements += [_settlement(f"eglc-{i}", resolved_yes=1) for i in range(30, 35)]
        db = _FakeDB(trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "EGLC")

        assert row["n"] == 35
        assert row["wilson_lower_bound"] == pytest.approx(0.706241, abs=1e-5)
        assert row["price_valid"] is False
        assert row["eligible"] is True
        assert row["status"] == "amber"

    def test_red_no_settled_trades_yet(self):
        # MPMG/YES: one shadow trade logged but not yet settled -> n=0, red.
        trades = [_trade("MPMG", "YES", "mpmg-1", 70)]
        db = _FakeDB(trades, settlements=[])

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "MPMG")

        assert row["n"] == 0
        assert row["eligible"] is False
        assert row["status"] == "red"
        assert "no settled shadow trades" in row["reason"]

    def test_yes_side_win_condition(self):
        # YES side wins when resolved_yes==1 (opposite of NO).
        trades = [_trade("WMKK", "YES", f"wmkk-{i}", 65) for i in range(30)]
        settlements = [_settlement(f"wmkk-{i}", resolved_yes=1) for i in range(29)]
        settlements.append(_settlement("wmkk-29", resolved_yes=0))
        db = _FakeDB(trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "WMKK" and r["side"] == "YES")

        assert row["wins"] == 29
        assert row["status"] == "green"

    def test_min_trades_threshold_is_config_wired(self):
        # Lowering PROMOTION_MIN_SETTLED_TRADES via config should make an
        # otherwise-amber (insufficient-n) station eligible, proving the
        # threshold is read from config rather than hardcoded.
        trades = [_trade("ZGSZ", "NO", f"zgsz-{i}", 65) for i in range(10)]
        settlements = [_settlement(f"zgsz-{i}", resolved_yes=0) for i in range(10)]
        db = _FakeDB(trades, settlements, config={"PROMOTION_MIN_SETTLED_TRADES": "5"})

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "ZGSZ")

        assert row["eligible"] is True
        assert row["status"] == "green"


class TestComputePromotionBarExcludesNonHighDirection:
    """Issue #610: compute_promotion_bar must filter to direction='high' so
    residual mislabeled low-side rows (direction dropped at insert) cannot
    poison the Wilson promotion stats."""

    def test_low_direction_rows_excluded_from_station_group(self):
        # 30 high-direction NO trades (all wins) form a green row for LFPB.
        # 5 additional direction='low' rows for the SAME station+side, all
        # wins, would inflate n to 35 and change the stats if not filtered.
        high_trades = [_trade("LFPB", "NO", f"lfpb-h{i}", 65) for i in range(30)]
        low_trades = [
            _trade("LFPB", "NO", f"lfpb-l{i}", 65, direction="low") for i in range(5)
        ]
        settlements = [_settlement(f"lfpb-h{i}", resolved_yes=0) for i in range(30)]
        settlements += [_settlement(f"lfpb-l{i}", resolved_yes=0) for i in range(5)]
        db = _FakeDB(high_trades + low_trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "LFPB" and r["side"] == "NO")

        assert row["n"] == 30, "direction='low' rows must not be counted"
        assert row["wins"] == 30

    def test_station_with_only_low_direction_rows_is_absent(self):
        # A station/side pair with ONLY direction='low' shadow rows should not
        # appear in the output at all (no direction='high' rows to group).
        low_trades = [
            _trade("KMIA", "NO", f"kmia-l{i}", 65, direction="low") for i in range(10)
        ]
        settlements = [_settlement(f"kmia-l{i}", resolved_yes=0) for i in range(10)]
        db = _FakeDB(low_trades, settlements)

        rows = compute_promotion_bar(db)
        assert not any(r["station"] == "KMIA" for r in rows)


class TestDaysCoverageLocalDate:
    """days_coverage should count station-local dates, not UTC dates."""

    def test_days_coverage_counts_local_dates(self):
        # KATL is UTC-4 in June. Create trades that span a UTC midnight but are
        # a single local date:
        # - 2026-06-15T03:30:00 UTC = 2026-06-14T23:30:00 EDT (local date 2026-06-14)
        # - 2026-06-15T04:30:00 UTC = 2026-06-15T00:30:00 EDT (local date 2026-06-15)
        # Both are different local dates, so days_coverage should be 2.
        trades = [
            _trade("KATL", "NO", "katl-1", 65, ts="2026-06-15T03:30:00+00:00"),
            _trade("KATL", "NO", "katl-2", 65, ts="2026-06-15T04:30:00+00:00"),
        ]
        settlements = [
            _settlement("katl-1", resolved_yes=0),
            _settlement("katl-2", resolved_yes=0),
        ]
        db = _FakeDB(trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "KATL" and r["side"] == "NO")

        # Both trades should be counted as separate local dates
        assert row["days_coverage"] == 2

    def test_days_coverage_same_local_date_different_utc_dates(self):
        # WMKK is UTC+8. Create trades that are the same local date but
        # different UTC dates:
        # - 2026-06-14T16:00:00 UTC = 2026-06-15T00:00:00 MYT (local date 2026-06-15)
        # - 2026-06-15T04:00:00 UTC = 2026-06-15T12:00:00 MYT (local date 2026-06-15)
        # Both are the same local date, so days_coverage should be 1.
        trades = [
            _trade("WMKK", "NO", "wmkk-1", 65, ts="2026-06-14T16:00:00+00:00"),
            _trade("WMKK", "NO", "wmkk-2", 65, ts="2026-06-15T04:00:00+00:00"),
        ]
        settlements = [
            _settlement("wmkk-1", resolved_yes=0),
            _settlement("wmkk-2", resolved_yes=0),
        ]
        db = _FakeDB(trades, settlements)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "WMKK" and r["side"] == "NO")

        # Both trades are on the same local date 2026-06-15
        assert row["days_coverage"] == 1
