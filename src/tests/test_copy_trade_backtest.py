"""Unit tests for src/scripts/copy_trade_backtest.py (copy-trading
hypothesis spike). All Polymarket I/O is mocked -- no network calls.
"""
from unittest.mock import patch

import pytest

from src.scripts.copy_trade_backtest import (
    apply_slippage,
    backtest_wallet,
    build_report,
    resolve_payout,
    run,
)


class TestApplySlippage:
    def test_buy_gets_worse_fill_higher_price(self):
        assert apply_slippage(0.50, "BUY", 100) == pytest.approx(0.505)

    def test_sell_gets_worse_fill_lower_price(self):
        assert apply_slippage(0.50, "SELL", 100) == pytest.approx(0.495)

    def test_zero_slippage_is_identity(self):
        assert apply_slippage(0.42, "BUY", 0) == pytest.approx(0.42)

    def test_clipped_to_valid_probability_range(self):
        assert apply_slippage(0.999, "BUY", 100_000) <= 0.9999
        assert apply_slippage(0.001, "SELL", 100_000) >= 0.0001


class TestResolvePayout:
    def test_winning_yes_outcome_pays_one(self):
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True):
            assert resolve_payout("0xabc", "Yes", {}) == 1.0

    def test_losing_yes_outcome_pays_zero(self):
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=False):
            assert resolve_payout("0xabc", "Yes", {}) == 0.0

    def test_no_outcome_on_yes_won_market_pays_zero(self):
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True):
            assert resolve_payout("0xabc", "No", {}) == 0.0

    def test_unresolved_market_returns_none(self):
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=None):
            assert resolve_payout("0xabc", "Yes", {}) is None

    def test_missing_outcome_returns_none(self):
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True):
            assert resolve_payout("0xabc", None, {}) is None

    def test_result_cached_per_market_single_fetch(self):
        with patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ) as mock_fn:
            cache = {}
            resolve_payout("0xabc", "Yes", cache)
            resolve_payout("0xabc", "No", cache)
            resolve_payout("0xdef", "Yes", cache)
        assert mock_fn.call_count == 2  # one per distinct market, not per call


def _raw_trade(market, side, price, size, outcome, ts=1700000000):
    return {
        "conditionId": market, "side": side, "price": str(price),
        "size": str(size), "timestamp": str(ts), "outcome": outcome,
    }


class TestBacktestWallet:
    def test_winning_buy_is_profitable_for_both_trader_and_copier(self):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150)

        assert result["n_buy_trades"] == 1
        assert result["n_resolved"] == 1
        assert result["trader"]["dollar_pnl"] == pytest.approx((1.0 - 0.50) * 10)
        # Copier pays more (worse fill) so profits less than the trader.
        assert result["copier"]["dollar_pnl"] < result["trader"]["dollar_pnl"]
        assert result["copier"]["dollar_pnl"] > 0

    def test_sell_trades_excluded_from_scoring(self):
        trades = [_raw_trade("0xabc", "SELL", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150)

        assert result["n_buy_trades"] == 0
        assert result["n_sell_excluded"] == 1
        assert result["n_resolved"] == 0
        assert result["trader"]["dollar_pnl"] == 0.0

    def test_unresolved_market_dropped_not_zero_pnl(self):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=None
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150)

        assert result["n_buy_trades"] == 1
        assert result["n_resolved"] == 0
        assert result["n_unresolved_dropped"] == 1

    def test_losing_buy_hurts_copier_more_than_trader(self):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "No")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150)

        assert result["trader"]["dollar_pnl"] == pytest.approx((0.0 - 0.50) * 10)
        assert result["copier"]["dollar_pnl"] < result["trader"]["dollar_pnl"]


class TestBuildReport:
    def test_report_contains_key_sections(self):
        results = [{
            "address": "0x1234567890",
            "n_buy_trades": 5, "n_sell_excluded": 1, "n_resolved": 4,
            "n_unresolved_dropped": 1,
            "trader": {"n": 4, "win_rate": 0.75, "mean_roi": 0.3, "median_roi": 0.2,
                       "dollar_pnl": 12.5},
            "copier": {"n": 4, "win_rate": 0.75, "mean_roi": 0.25, "median_roi": 0.15,
                       "dollar_pnl": 10.0},
        }]
        report = build_report("2026-09-16", 150.0, results)
        assert "Copy-Trading Hypothesis Spike" in report
        assert "exploratory spike" in report
        assert "0x12345678" in report
        assert "150 bps" in report
        assert "Wallets evaluated: 1" in report


class TestRun:
    def test_explicit_wallets_bypass_leaderboard(self, tmp_path):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_leaderboard"
        ) as mock_leaderboard, patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            rc = run(
                window="month", top=20, wallets=["0xwallet"], slippage_bps=150.0,
                out_dir=tmp_path, run_date="2026-09-16",
            )
        assert rc == 0
        mock_leaderboard.assert_not_called()
        out_file = tmp_path / "copy_trade_backtest_2026-09-16.md"
        assert out_file.exists()

    def test_empty_leaderboard_and_no_wallets_returns_error(self, tmp_path):
        with patch("src.scripts.copy_trade_backtest.get_leaderboard", return_value=[]):
            rc = run(
                window="month", top=20, wallets=None, slippage_bps=150.0,
                out_dir=tmp_path, run_date="2026-09-16",
            )
        assert rc == 1
        assert not list(tmp_path.glob("*.md"))

    def test_no_resolved_trades_writes_nothing(self, tmp_path):
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=[]
        ):
            rc = run(
                window="month", top=20, wallets=["0xwallet"], slippage_bps=150.0,
                out_dir=tmp_path, run_date="2026-09-16",
            )
        assert rc == 0
        assert not list(tmp_path.glob("*.md"))
