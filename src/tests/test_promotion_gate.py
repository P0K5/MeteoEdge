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
        # fee(60) = 100 * 0.05 * 0.60 * 0.40 = 1.2
        # breakeven = (60 + 1.2) / 100 = 0.612
        assert breakeven_win_rate(60) == pytest.approx(0.612, abs=1e-9)

    def test_75_cents_hand_computed(self):
        # fee(75) = 100 * 0.05 * 0.75 * 0.25 = 0.9375
        # breakeven = (75 + 0.9375) / 100 = 0.759375
        assert breakeven_win_rate(75) == pytest.approx(0.759375, abs=1e-9)

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
    by returning a fixed config snapshot, and serves bulk trades exactly like
    the real Database's get_trades()."""

    def __init__(self, trades, config=None):
        self._trades = trades
        self._config = config or {}

    def get_all_config(self):
        return self._config

    def get_trades(self, limit=50, mode=None, direction=None, is_next_day=None):
        return [
            t for t in self._trades
            if (mode is None or t.get("mode") == mode)
            and (is_next_day is None or t.get("is_next_day", 0) == is_next_day)
        ]


def _trade(station, side, ticker, actual_price, won: "bool | None" = None,
           ts="2026-06-01T12:00:00", mode="shadow", direction="high",
           is_next_day=0, p_yes_raw: "float | None" = None):
    """Build a shadow trade row the way settle_shadow_trades() leaves it:
    pnl set directly on the row (win: (100-ask)/100, loss: -ask/100), no
    settlements-table join (issue #655 — shadow trades are NEVER written to
    the settlements table, only live-trade markets are). won=None means the
    trade is still unsettled: pnl stays None, matching a real pending row.

    is_next_day=0 by default (issue #704) -- matches every pre-#704 row.
    p_yes_raw=None by default -- matches historical rows predating the
    column (issue #551); pass 0.0 to simulate a certainty-shortcut row
    (issue #823).
    """
    if won is None:
        pnl = None
    else:
        pnl = (100 - actual_price) / 100 if won else -actual_price / 100
    return {
        "station": station, "side": side, "ticker": ticker,
        "actual_price": actual_price, "pnl": pnl, "ts": ts, "mode": mode,
        "direction": direction, "is_next_day": is_next_day,
        "p_yes_raw": p_yes_raw,
    }


class TestComputePromotionBar:
    """compute_promotion_bar() -- calls the real wilson_lower_bound/breakeven_win_rate
    internally; expected numbers below were independently verified against those
    functions' hand-computed cases above, not reimplemented inline."""

    def test_db_none_returns_empty_list(self):
        assert compute_promotion_bar(None) == []

    def test_settled_trades_counted_without_any_settlements_table_row(self):
        """Issue #655 regression: a shadow trade settled by the real
        settle_shadow_trades() flow has NO row in the settlements table at
        all (it writes pnl directly onto the trade). The bar must still
        count it as settled -- the old code silently reported n=0 for
        essentially every station because it joined shadow trades against
        settlements, which only ever holds live-trade markets."""
        trades = [_trade("KATL", "NO", "katl-only-on-trade-row", 65, won=True)]
        db = _FakeDB(trades)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "KATL" and r["side"] == "NO")

        assert row["n"] == 1
        assert row["wins"] == 1

    def test_green_eligible_station(self):
        # WSSS/NO: 30 settled trades, 29 wins, avg entry 65c.
        # fee(65) = 100 * 0.05 * 0.65 * 0.35 = 1.1375
        # breakeven(65) = (65 + 1.1375) / 100 = 0.661375
        # wilson_lower_bound(29,30)=0.833292 > breakeven(65)=0.661375,
        # and n=30 >= default PROMOTION_MIN_SETTLED_TRADES=30 -> green/eligible.
        trades = [_trade("WSSS", "NO", f"wsss-{i}", 65, won=True) for i in range(29)]
        trades.append(_trade("WSSS", "NO", "wsss-29", 65, won=False))  # 1 loss
        # Fixtures use 65c entries; pin the price floor they were computed
        # against (the code default moved to 70c in #644).
        db = _FakeDB(trades, config={"MIN_PRICE_CENTS": "60"})

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "WSSS" and r["side"] == "NO")

        assert row["n"] == 30
        assert row["wins"] == 29
        assert row["win_rate"] == pytest.approx(29 / 30)
        assert row["wilson_lower_bound"] == pytest.approx(0.833292, abs=1e-5)
        assert row["breakeven_win_rate"] == pytest.approx(0.661375, abs=1e-9)
        assert row["price_valid"] is True
        assert row["eligible"] is True
        assert row["status"] == "green"

    def test_amber_insufficient_sample_size(self):
        # ZGSZ/NO: only 10 settled trades, all wins, avg entry 65c.
        # breakeven(65) = 0.661375 (from new fee model: 100*0.05*0.65*0.35=1.1375)
        # wilson_lower_bound(10,10)=0.722460 > breakeven(65)=0.661375 (clears
        # the statistical bar) but n=10 < 30 -> amber, not eligible.
        trades = [_trade("ZGSZ", "NO", f"zgsz-{i}", 65, won=True) for i in range(10)]
        db = _FakeDB(trades)

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
        trades = [_trade("RKSI", "NO", f"rksi-{i}", 65, won=True) for i in range(18)]
        trades += [_trade("RKSI", "NO", f"rksi-{i}", 65, won=False) for i in range(18, 30)]
        db = _FakeDB(trades)

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
        trades = [_trade("EGLC", "NO", f"eglc-{i}", 50, won=True) for i in range(30)]
        trades += [_trade("EGLC", "NO", f"eglc-{i}", 50, won=False) for i in range(30, 35)]
        db = _FakeDB(trades)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "EGLC")

        assert row["n"] == 35
        assert row["wilson_lower_bound"] == pytest.approx(0.706241, abs=1e-5)
        assert row["price_valid"] is False
        assert row["eligible"] is True
        assert row["status"] == "amber"

    def test_red_no_settled_trades_yet(self):
        # MPMG/YES: one shadow trade logged but not yet settled -> n=0, red.
        trades = [_trade("MPMG", "YES", "mpmg-1", 70, won=None)]
        db = _FakeDB(trades)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "MPMG")

        assert row["n"] == 0
        assert row["eligible"] is False
        assert row["status"] == "red"
        assert "no settled shadow trades" in row["reason"]

    def test_yes_side_win_condition(self):
        # YES side wins when the bracket was hit -- opposite polarity of NO,
        # but here that's just a matter of which `won` flag was passed in
        # when the row's pnl was computed.
        trades = [_trade("WMKK", "YES", f"wmkk-{i}", 65, won=True) for i in range(29)]
        trades.append(_trade("WMKK", "YES", "wmkk-29", 65, won=False))
        db = _FakeDB(trades, config={"MIN_PRICE_CENTS": "60"})

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "WMKK" and r["side"] == "YES")

        assert row["wins"] == 29
        assert row["status"] == "green"

    def test_min_trades_threshold_is_config_wired(self):
        # Lowering PROMOTION_MIN_SETTLED_TRADES via config should make an
        # otherwise-amber (insufficient-n) station eligible, proving the
        # threshold is read from config rather than hardcoded.
        trades = [_trade("ZGSZ", "NO", f"zgsz-{i}", 65, won=True) for i in range(10)]
        db = _FakeDB(trades, config={
            "PROMOTION_MIN_SETTLED_TRADES": "5", "MIN_PRICE_CENTS": "60",
        })

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
        high_trades = [_trade("LFPB", "NO", f"lfpb-h{i}", 65, won=True) for i in range(30)]
        low_trades = [
            _trade("LFPB", "NO", f"lfpb-l{i}", 65, won=True, direction="low")
            for i in range(5)
        ]
        db = _FakeDB(high_trades + low_trades)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "LFPB" and r["side"] == "NO")

        assert row["n"] == 30, "direction='low' rows must not be counted"
        assert row["wins"] == 30

    def test_station_with_only_low_direction_rows_is_absent(self):
        # A station/side pair with ONLY direction='low' shadow rows should not
        # appear in the output at all (no direction='high' rows to group).
        low_trades = [
            _trade("KMIA", "NO", f"kmia-l{i}", 65, won=True, direction="low")
            for i in range(10)
        ]
        db = _FakeDB(low_trades)

        rows = compute_promotion_bar(db)
        assert not any(r["station"] == "KMIA" for r in rows)


class TestComputePromotionBarExcludesNextDay:
    """Issue #704 (Gap 1): compute_promotion_bar must exclude next-day shadow
    rows (is_next_day=1) so the different sigma/lead-time regime introduced
    by next-day evaluation (#687) cannot contaminate the win-rate/Wilson
    bound stats this bar is built on."""

    def test_next_day_rows_excluded_from_station_group(self):
        # 30 same-day NO trades (all wins) form a green row for RJTT.
        # 5 additional is_next_day=1 rows for the SAME station+side, all
        # wins, would inflate n to 35 and change the stats if not filtered.
        same_day = [_trade("RJTT", "NO", f"rjtt-s{i}", 65, won=True) for i in range(30)]
        next_day = [
            _trade("RJTT", "NO", f"rjtt-n{i}", 65, won=True, is_next_day=1)
            for i in range(5)
        ]
        db = _FakeDB(same_day + next_day)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "RJTT" and r["side"] == "NO")

        assert row["n"] == 30, "is_next_day=1 rows must not be counted"
        assert row["wins"] == 30

    def test_station_with_only_next_day_rows_is_absent(self):
        next_day = [
            _trade("ZSPD", "NO", f"zspd-n{i}", 65, won=True, is_next_day=1)
            for i in range(10)
        ]
        db = _FakeDB(next_day)

        rows = compute_promotion_bar(db)
        assert not any(r["station"] == "ZSPD" for r in rows)


class TestComputePromotionBarExcludesCertaintyShortcut:
    """Issue #823: compute_promotion_bar must exclude "certainty-shortcut"
    rows (p_yes_raw == 0.0 -- envelope.py's ``hi <= current_high_f`` branch,
    which returns a hard 0.0 without ever evaluating the probabilistic model)
    from its win-rate/Wilson stats, and surface the excluded count per
    station+side for auditability.

    Real production data showed ZGSZ and SBGR (~33% and ~37% of their settled
    shadow rows respectively) were certainty-shortcut calls -- rows that can
    only ever settle as a NO win by construction, silently inflating the
    apparent win rate/edge for those stations."""

    def test_certainty_shortcut_rows_excluded_from_station_group(self):
        # 30 real (non-shortcut) NO trades (all wins) form a green row for
        # ZGSZ. 15 additional p_yes_raw=0.0 rows for the SAME station+side,
        # also wins, would inflate n to 45 and change the stats if not
        # filtered -- mirroring the ~33% contamination seen in production.
        real_trades = [
            _trade("ZGSZ", "NO", f"zgsz-r{i}", 65, won=True, p_yes_raw=0.05)
            for i in range(30)
        ]
        shortcut_trades = [
            _trade("ZGSZ", "NO", f"zgsz-s{i}", 65, won=True, p_yes_raw=0.0)
            for i in range(15)
        ]
        db = _FakeDB(real_trades + shortcut_trades)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "ZGSZ" and r["side"] == "NO")

        assert row["n"] == 30, "p_yes_raw=0.0 rows must not be counted"
        assert row["wins"] == 30
        assert row["excluded_certainty_shortcut_count"] == 15

    def test_station_with_only_shortcut_rows_still_appears_with_n_zero(self):
        # Issue #823 explicitly requires surfacing the excluded count for
        # auditability -- a station+side made up ENTIRELY of shortcut rows
        # must still appear (n=0, red) rather than silently disappearing,
        # so a reviewer can see it was excluded rather than assuming there
        # was simply no shadow data at all.
        shortcut_only = [
            _trade("SBGR", "NO", f"sbgr-s{i}", 65, won=True, p_yes_raw=0.0)
            for i in range(8)
        ]
        db = _FakeDB(shortcut_only)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "SBGR" and r["side"] == "NO")

        assert row["n"] == 0
        assert row["excluded_certainty_shortcut_count"] == 8
        assert row["eligible"] is False
        assert row["status"] == "red"
        assert "8 certainty-shortcut row(s) excluded" in row["reason"]
        assert "#823" in row["reason"]

    def test_p_yes_raw_none_is_not_treated_as_shortcut(self):
        # Historical rows predating the p_yes_raw column (issue #551) carry
        # p_yes_raw=None, not 0.0 -- these must NOT be excluded.
        trades = [
            _trade("KATL", "NO", f"katl-{i}", 65, won=True, p_yes_raw=None)
            for i in range(5)
        ]
        db = _FakeDB(trades)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "KATL" and r["side"] == "NO")

        assert row["n"] == 5
        assert row["excluded_certainty_shortcut_count"] == 0

    def test_green_station_unaffected_when_no_shortcut_rows_present(self):
        # Regression guard: a station with zero certainty-shortcut rows must
        # report excluded_certainty_shortcut_count == 0 and be unaffected.
        trades = [_trade("WSSS", "NO", f"wsss-{i}", 65, won=True, p_yes_raw=0.05)
                  for i in range(29)]
        trades.append(_trade("WSSS", "NO", "wsss-29", 65, won=False, p_yes_raw=0.05))
        db = _FakeDB(trades, config={"MIN_PRICE_CENTS": "60"})

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "WSSS" and r["side"] == "NO")

        assert row["n"] == 30
        assert row["excluded_certainty_shortcut_count"] == 0
        assert row["status"] == "green"


class TestHasSettledLossExcludesNextDay:
    """Issue #704: _has_settled_loss() (gate 5's real implementation, called
    unmocked here) must exclude next-day rows for the same reason
    compute_promotion_bar() does -- a next-day-only loss should not count
    toward "this station has proven it can lose" for same-day promotion.

    Issue #789: shadow settlement lives on the trade row's own `pnl` (written
    by settle_shadow_trades() via update_trade_by_id()), never in the
    `settlements` table -- that table is only ever populated for live-trade
    markets. These fixtures settle pnl directly on the shadow row, matching
    production, instead of the pre-#789 fixture that only wrote a
    `settlements` row the real code path never reads for shadow trades.
    """

    def _db_with_shadow_settlement(self, station, ticker, side, resolved_yes, is_next_day):
        from src.data.db import Database
        db = Database(":memory:")
        row_id, _ = db.upsert_shadow_trade(
            ts="2026-06-01T10:00:00Z", station=station, ticker=ticker,
            bracket_low=80.0, bracket_high=82.0, side=side,
            predicted_price=70, actual_price=70, predicted_edge=10.0,
            is_next_day=is_next_day,
        )
        # side=YES, resolved_yes=0 -> YES lost -> pnl < 0 (mirrors settle.py).
        won = (side == "YES") == bool(resolved_yes)
        pnl = (100 - 70) / 100 if won else -70 / 100
        db.update_trade_by_id(row_id, outcome="filled", pnl=round(pnl, 6),
                               settled_at="2026-06-01T12:00:00Z")
        return db

    def test_same_day_loss_counts(self):
        from src.model.promotion_gate import _has_settled_loss
        # side=YES, resolved_yes=0 -> YES lost.
        db = self._db_with_shadow_settlement("KJFK", "kjfk-sameday", "YES", 0, is_next_day=0)
        assert _has_settled_loss(db, "KJFK") is True

    def test_next_day_only_loss_does_not_count(self):
        from src.model.promotion_gate import _has_settled_loss
        db = self._db_with_shadow_settlement("KJFK", "kjfk-nextday", "YES", 0, is_next_day=1)
        assert _has_settled_loss(db, "KJFK") is False

    def test_pure_wins_do_not_count_as_loss(self):
        """Issue #789 regression: a station with only settled wins (no
        settlements-table row at all) must still return False, not True --
        the pre-fix ticker-join silently returned False here too (for the
        wrong reason: no settlements row existed to join against), so this
        pins the correct behavior now that pnl is read directly."""
        from src.model.promotion_gate import _has_settled_loss
        db = self._db_with_shadow_settlement("KJFK", "kjfk-win", "YES", 1, is_next_day=0)
        assert _has_settled_loss(db, "KJFK") is False


class TestDaysCoverageLocalDate:
    """days_coverage should count station-local dates, not UTC dates."""

    def test_days_coverage_counts_local_dates(self):
        # KATL is UTC-4 in June. Create trades that span a UTC midnight but are
        # a single local date:
        # - 2026-06-15T03:30:00 UTC = 2026-06-14T23:30:00 EDT (local date 2026-06-14)
        # - 2026-06-15T04:30:00 UTC = 2026-06-15T00:30:00 EDT (local date 2026-06-15)
        # Both are different local dates, so days_coverage should be 2.
        trades = [
            _trade("KATL", "NO", "katl-1", 65, won=True, ts="2026-06-15T03:30:00+00:00"),
            _trade("KATL", "NO", "katl-2", 65, won=True, ts="2026-06-15T04:30:00+00:00"),
        ]
        db = _FakeDB(trades)

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
            _trade("WMKK", "NO", "wmkk-1", 65, won=True, ts="2026-06-14T16:00:00+00:00"),
            _trade("WMKK", "NO", "wmkk-2", 65, won=True, ts="2026-06-15T04:00:00+00:00"),
        ]
        db = _FakeDB(trades)

        rows = compute_promotion_bar(db)
        row = next(r for r in rows if r["station"] == "WMKK" and r["side"] == "NO")

        # Both trades are on the same local date 2026-06-15
        assert row["days_coverage"] == 1


class TestPromotionBarNoSideBoughtCost737:
    """Issue #738: once #737 stores the NO cost in trades.actual_price, the
    promotion bar's NO-side breakeven and price_valid reflect the true ~0.78
    breakeven at ~77c NO entries -- not the ~0.25 the old yes_ask-in-actual_price
    convention produced (which made the NO bar far too easy to clear)."""

    def test_no_side_breakeven_reflects_bought_side_cost(self):
        # 20 settled NO rows at the corrected NO cost of 77c (mixed outcomes).
        trades = [
            _trade("RCSS", "NO", f"no{i}", actual_price=77, won=(i % 5 != 0))
            for i in range(20)
        ]
        rows = compute_promotion_bar(_FakeDB(trades))
        no_row = next(r for r in rows if r["side"] == "NO")
        assert no_row["avg_entry_price_cents"] == pytest.approx(77.0)
        # breakeven must equal the real (bought-side) breakeven at 77c ...
        assert no_row["breakeven_win_rate"] == pytest.approx(breakeven_win_rate(77))
        # ... i.e. ~0.78, NOT the ~0.25 the old yes_ask (~23c) basis produced.
        assert no_row["breakeven_win_rate"] > 0.75
        # price_valid was side-confused pre-#737 (avg_entry ~23c < MIN_PRICE_CENTS);
        # at the corrected 77c it is valid.
        assert no_row["price_valid"] is True
