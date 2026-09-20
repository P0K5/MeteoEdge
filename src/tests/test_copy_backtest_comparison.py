"""Unit tests for src/data/copy_backtest_comparison.py (issue #1133)."""
import pytest

from src.data.copy_backtest_comparison import (
    get_backtest_comparison_total,
    get_wallet_backtest_comparison,
)
from src.data.db import Database


def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


def _settled_position(db, address, pnl, **overrides):
    signal_id = db.insert_copy_signal(
        address=address,
        market=overrides.pop("market", "0xmarket1"),
        source_price=0.45,
        detected_at="2026-09-19T00:00:00+00:00",
    )
    kwargs = dict(
        signal_id=signal_id,
        address=address,
        market="0xmarket1",
        outcome_index=0,
        entry_price=0.45,
        stake_usd=25.0,
        entry_ts="2026-09-19T00:00:00+00:00",
    )
    kwargs.update(overrides)
    position_id = db.insert_copy_position(**kwargs)
    db.settle_copy_position(position_id, pnl, "2026-09-20T00:00:00+00:00")
    return position_id


def _open_position(db, address, **overrides):
    signal_id = db.insert_copy_signal(
        address=address,
        market=overrides.pop("market", "0xmarket1"),
        source_price=0.45,
        detected_at="2026-09-19T00:00:00+00:00",
    )
    kwargs = dict(
        signal_id=signal_id,
        address=address,
        market="0xmarket1",
        outcome_index=0,
        entry_price=0.45,
        stake_usd=25.0,
        entry_ts="2026-09-19T00:00:00+00:00",
    )
    kwargs.update(overrides)
    return db.insert_copy_position(**kwargs)


def _screening_kwargs(**overrides):
    kwargs = dict(
        address="0xaaa",
        window="30d",
        screened_at="2026-07-30T13:00:00+00:00",
        n_buy_trades=100,
        n_resolved=50,
        slippage_bps=25.0,
        win_rate=0.6,
        mean_roi=0.1,
        median_roi=0.2,
        mirrored_dollar_pnl=1000.0,
        flat_dollar_pnl=500.0,
        flat_stake=100.0,
        eligible_to_follow=1,
    )
    kwargs.update(overrides)
    return kwargs


class TestGetWalletBacktestComparison:
    def test_both_figures_present_computes_correctly(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))
        _settled_position(db, "0xaaa", 400.0)
        _settled_position(db, "0xaaa", 200.0)

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["address"] == "0xaaa"
        assert result["n_settled"] == 2
        assert result["realized_pnl_usd"] == pytest.approx(600.0)
        assert result["projected_flat_dollar_pnl"] == pytest.approx(500.0)
        # divergence_usd = realized - projected = 600 - 500 = 100
        assert result["divergence_usd"] == pytest.approx(100.0)
        # divergence_pct = 100 / abs(500) * 100 = 20.0
        assert result["divergence_pct"] == pytest.approx(20.0)

    def test_divergence_sign_matches_underperformance(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))
        _settled_position(db, "0xaaa", 100.0)

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["realized_pnl_usd"] == pytest.approx(100.0)
        assert result["divergence_usd"] == pytest.approx(-400.0)
        assert result["divergence_pct"] == pytest.approx(-80.0)

    def test_divergence_pct_uses_projection_magnitude_not_signed_value(self):
        """A negative backtest projection still yields a divergence_pct
        whose sign matches divergence_usd (not flipped by a negative
        denominator)."""
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=-100.0))
        _settled_position(db, "0xaaa", 50.0)

        result = get_wallet_backtest_comparison(db, "0xaaa")

        # divergence_usd = 50 - (-100) = 150 (realized beat the backtest)
        assert result["divergence_usd"] == pytest.approx(150.0)
        # divergence_pct = 150 / abs(-100) * 100 = 150.0 (positive, matching
        # the "beat the backtest" direction of divergence_usd)
        assert result["divergence_pct"] == pytest.approx(150.0)

    def test_screening_row_with_zero_settled_positions_returns_zero_realized(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["n_settled"] == 0
        assert result["realized_pnl_usd"] == 0.0
        assert result["projected_flat_dollar_pnl"] == pytest.approx(500.0)
        assert result["divergence_usd"] == pytest.approx(-500.0)
        assert result["divergence_pct"] == pytest.approx(-100.0)

    def test_screening_row_with_only_open_positions_returns_zero_realized(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))
        _open_position(db, "0xaaa")

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["n_settled"] == 0
        assert result["realized_pnl_usd"] == 0.0

    def test_settled_positions_with_no_screening_row_projected_is_none(self):
        db = _db()
        _settled_position(db, "0xaaa", 42.0)

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["n_settled"] == 1
        assert result["realized_pnl_usd"] == pytest.approx(42.0)
        assert result["projected_flat_dollar_pnl"] is None
        assert result["divergence_usd"] is None
        assert result["divergence_pct"] is None

    def test_no_data_at_all_returns_zero_realized_and_none_projected(self):
        db = _db()

        result = get_wallet_backtest_comparison(db, "0xnotarealwallet")

        assert result["n_settled"] == 0
        assert result["realized_pnl_usd"] == 0.0
        assert result["projected_flat_dollar_pnl"] is None
        assert result["divergence_usd"] is None
        assert result["divergence_pct"] is None

    def test_screening_row_with_null_flat_dollar_pnl_projected_is_none(self):
        """A screening row can exist with flat_dollar_pnl NULL (the column
        is nullable) -- treated the same as "no screening row" for this
        comparison, not as a projected figure of 0."""
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=None))
        _settled_position(db, "0xaaa", 42.0)

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["projected_flat_dollar_pnl"] is None
        assert result["divergence_usd"] is None
        assert result["divergence_pct"] is None

    def test_uses_latest_screening_row_not_an_earlier_one(self):
        db = _db()
        db.insert_wallet_screening(
            **_screening_kwargs(
                address="0xaaa", screened_at="2026-07-30T13:00:00+00:00", flat_dollar_pnl=100.0,
            )
        )
        db.insert_wallet_screening(
            **_screening_kwargs(
                address="0xaaa", screened_at="2026-08-30T13:00:00+00:00", flat_dollar_pnl=500.0,
            )
        )
        _settled_position(db, "0xaaa", 500.0)

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["projected_flat_dollar_pnl"] == pytest.approx(500.0)

    def test_only_filters_to_requested_address(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))
        db.insert_wallet_screening(**_screening_kwargs(address="0xbbb", flat_dollar_pnl=-999.0))
        _settled_position(db, "0xaaa", 500.0)
        _settled_position(db, "0xbbb", -999.0)

        result = get_wallet_backtest_comparison(db, "0xaaa")

        assert result["address"] == "0xaaa"
        assert result["realized_pnl_usd"] == pytest.approx(500.0)
        assert result["projected_flat_dollar_pnl"] == pytest.approx(500.0)


class TestGetBacktestComparisonTotal:
    def test_sums_match_sum_of_per_wallet_figures(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))
        db.insert_wallet_screening(**_screening_kwargs(address="0xbbb", flat_dollar_pnl=-200.0))
        _settled_position(db, "0xaaa", 400.0)
        _settled_position(db, "0xaaa", 200.0)
        _settled_position(db, "0xbbb", -50.0)

        aaa = get_wallet_backtest_comparison(db, "0xaaa")
        bbb = get_wallet_backtest_comparison(db, "0xbbb")
        total = get_backtest_comparison_total(db)

        assert total["n_wallets"] == 2
        assert total["n_settled"] == aaa["n_settled"] + bbb["n_settled"]
        assert total["realized_pnl_usd"] == pytest.approx(
            aaa["realized_pnl_usd"] + bbb["realized_pnl_usd"]
        )
        assert total["projected_flat_dollar_pnl"] == pytest.approx(
            aaa["projected_flat_dollar_pnl"] + bbb["projected_flat_dollar_pnl"]
        )
        assert total["divergence_usd"] == pytest.approx(
            total["realized_pnl_usd"] - total["projected_flat_dollar_pnl"]
        )

    def test_excludes_wallet_with_settled_positions_but_no_screening_row(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))
        _settled_position(db, "0xaaa", 400.0)
        # 0xbbb has realized P&L but was never screened.
        _settled_position(db, "0xbbb", 999.0)

        total = get_backtest_comparison_total(db)

        assert total["n_wallets"] == 1
        assert total["realized_pnl_usd"] == pytest.approx(400.0)
        assert total["projected_flat_dollar_pnl"] == pytest.approx(500.0)

    def test_includes_screened_wallet_with_zero_settled_positions_as_zero(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=500.0))

        total = get_backtest_comparison_total(db)

        assert total["n_wallets"] == 1
        assert total["n_settled"] == 0
        assert total["realized_pnl_usd"] == pytest.approx(0.0)
        assert total["projected_flat_dollar_pnl"] == pytest.approx(500.0)

    def test_no_wallets_at_all_returns_zero_totals_and_none_pct(self):
        db = _db()

        total = get_backtest_comparison_total(db)

        assert total == {
            "n_wallets": 0,
            "n_settled": 0,
            "realized_pnl_usd": 0,
            "projected_flat_dollar_pnl": 0,
            "divergence_usd": 0,
            "divergence_pct": None,
        }

    def test_divergence_pct_none_when_projected_sum_is_zero(self):
        db = _db()
        db.insert_wallet_screening(**_screening_kwargs(address="0xaaa", flat_dollar_pnl=200.0))
        db.insert_wallet_screening(**_screening_kwargs(address="0xbbb", flat_dollar_pnl=-200.0))

        total = get_backtest_comparison_total(db)

        assert total["projected_flat_dollar_pnl"] == pytest.approx(0.0)
        assert total["divergence_pct"] is None
