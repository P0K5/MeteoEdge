"""Unit tests for src/scripts/copy_trade_backtest.py (copy-trading
hypothesis spike). All Polymarket I/O is mocked -- no network calls.
"""
from unittest.mock import patch

import pytest

from src.scripts.copy_trade_backtest import (
    apply_slippage,
    backtest_wallet,
    build_report,
    collect_distinct_buy_markets,
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

    def test_outcome_index_zero_wins_regardless_of_text_label(self):
        # Real markets label outcomes "Up"/"Down", team names, etc. -- not
        # literally "Yes"/"No". outcome_index=0 is the winning position here
        # (matches fetch_market_resolution's True), so this must pay 1.0
        # even though the text "Up" would fail a naive "yes" string match.
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True):
            assert resolve_payout("0xabc", "Up", {}, outcome_index=0) == 1.0

    def test_outcome_index_one_loses_when_index_zero_won(self):
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True):
            assert resolve_payout("0xabc", "Down", {}, outcome_index=1) == 0.0

    def test_outcome_index_takes_priority_over_text_label(self):
        # outcome text says "No" but outcome_index says the winning (0)
        # position -- outcome_index must win the disagreement.
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True):
            assert resolve_payout("0xabc", "No", {}, outcome_index=0) == 1.0

    def test_falls_back_to_text_match_when_index_missing(self):
        with patch("src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True):
            assert resolve_payout("0xabc", "Yes", {}, outcome_index=None) == 1.0


def _raw_trade(market, side, price, size, outcome, ts=1700000000, outcome_index=None):
    raw = {
        "conditionId": market, "side": side, "price": str(price),
        "size": str(size), "timestamp": str(ts), "outcome": outcome,
    }
    if outcome_index is not None:
        raw["outcomeIndex"] = outcome_index
    return raw


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

    def test_winning_buy_on_non_yes_no_labeled_market_scores_correctly(self):
        # Regression test for the bug found against live data: a market
        # whose outcome text is "Up" (not "Yes") but whose outcomeIndex=0 is
        # the winning position must still score as a win end-to-end.
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Up", outcome_index=0)]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150)

        assert result["n_resolved"] == 1
        assert result["trader"]["dollar_pnl"] == pytest.approx((1.0 - 0.50) * 10)

    def test_flat_stake_omitted_by_default(self):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150)
        assert "copier_flat" not in result

    def test_flat_stake_dollar_pnl_independent_of_trader_size(self):
        # Trader bought 10 shares at 0.50; a $5-flat copier buys a different
        # share count (5 / copier_price), so flat $ PnL must NOT scale with
        # the trader's size -- it's driven only by copier_roi * flat_stake.
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150, flat_stake=5.0)

        copier_price = 0.50 * 1.015
        expected_copier_roi = (1.0 - copier_price) / copier_price
        # dollar_pnl is rounded to cents in backtest_wallet.
        assert result["copier_flat"]["dollar_pnl"] == pytest.approx(
            5.0 * expected_copier_roi, abs=0.01,
        )
        # ROI stats are size-independent -- identical to the mirrored scenario.
        assert result["copier_flat"]["mean_roi"] == result["copier"]["mean_roi"]
        assert result["copier_flat"]["win_rate"] == result["copier"]["win_rate"]

    def test_flat_stake_multiple_trades_sums_per_trade_roi(self):
        trades = [
            _raw_trade("0xabc", "BUY", 0.50, 10, "Yes"),
            _raw_trade("0xdef", "BUY", 0.20, 1000, "No"),
        ]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150, flat_stake=5.0)

        p1 = 0.50 * 1.015
        roi1 = (1.0 - p1) / p1
        p2 = 0.20 * 1.015
        roi2 = (0.0 - p2) / p2  # "No" trade loses when the market resolves True/"Yes"
        assert result["copier_flat"]["dollar_pnl"] == pytest.approx(
            5.0 * (roi1 + roi2), abs=0.01,
        )


class TestBacktestWalletCache:
    """issue #1221: backtest_wallet() accepts an optional shared cache so a
    caller (copy_wallet_screening.run()) can reuse one resolution cache
    across every wallet in a run, instead of each wallet paying for its own.
    """

    def test_no_cache_argument_still_works(self):
        # Standalone-CLI path (copy_trade_backtest.py's own run()) never
        # passes a cache -- must keep working exactly as before.
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ) as mock_fn:
            result = backtest_wallet("0xwallet", slippage_bps=150)
        assert result["n_resolved"] == 1
        mock_fn.assert_called_once_with("0xabc")

    def test_shared_cache_across_two_wallets_fetches_market_once(self):
        # Two different wallets' trade tapes hitting the same market, via a
        # cache shared across both backtest_wallet() calls -- the run-scoped
        # cache tier 1 is meant to serve (issue #1221).
        trades = [_raw_trade("0xshared", "BUY", 0.50, 10, "Yes")]
        cache: dict = {}
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ) as mock_fn:
            backtest_wallet("0xwallet1", slippage_bps=150, cache=cache)
            backtest_wallet("0xwallet2", slippage_bps=150, cache=cache)
        mock_fn.assert_called_once_with("0xshared")

    def test_prepopulated_cache_produces_identical_stats_as_cold_cache(self):
        # Same fixture trades, once with a cold (empty) cache and once with
        # a cache already warmed with the correct resolution -- median_roi
        # and dollar_pnl must be identical either way (issue #1221's "no
        # behavioural change" acceptance criterion).
        trades = [
            _raw_trade("0xabc", "BUY", 0.50, 10, "Yes"),
            _raw_trade("0xdef", "BUY", 0.20, 5, "No"),
        ]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            cold_result = backtest_wallet("0xwallet", slippage_bps=150, cache={})

        warm_cache = {"0xabc": True, "0xdef": True}
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution",
        ) as mock_fn:
            warm_result = backtest_wallet("0xwallet", slippage_bps=150, cache=warm_cache)
        mock_fn.assert_not_called()  # every market was already in the warm cache

        assert warm_result["copier"]["median_roi"] == pytest.approx(
            cold_result["copier"]["median_roi"]
        )
        assert warm_result["copier"]["dollar_pnl"] == pytest.approx(
            cold_result["copier"]["dollar_pnl"]
        )
        assert warm_result["trader"]["dollar_pnl"] == pytest.approx(
            cold_result["trader"]["dollar_pnl"]
        )


class TestCollectDistinctBuyMarkets:
    """collect_distinct_buy_markets() (issue #1227) -- the pre-scan
    backtest_wallet's batch_resolve=True path uses to know which markets to
    batch-resolve before the per-trade loop runs."""

    def test_distinct_markets_in_first_seen_order(self):
        trades = [
            _raw_trade("0xabc", "BUY", 0.5, 10, "Yes"),
            _raw_trade("0xdef", "BUY", 0.2, 5, "No"),
            _raw_trade("0xabc", "BUY", 0.6, 3, "Yes"),
        ]
        assert collect_distinct_buy_markets(trades) == ["0xabc", "0xdef"]

    def test_sell_trades_excluded(self):
        trades = [_raw_trade("0xabc", "SELL", 0.5, 10, "Yes")]
        assert collect_distinct_buy_markets(trades) == []

    def test_unnormalizable_trade_dropped(self):
        trades = [{"side": "BUY"}]  # missing conditionId/price/etc.
        assert collect_distinct_buy_markets(trades) == []


class TestBacktestWalletBatchResolve:
    """backtest_wallet(..., batch_resolve=True) (issue #1227) -- screening's
    cold-cache path. Default (batch_resolve=False, every existing caller)
    is covered by TestBacktestWallet/TestBacktestWalletCache above and is
    intentionally untouched by any of these tests.
    """

    def test_default_batch_resolve_false_never_calls_batch_fetcher(self):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
        ) as mock_batch:
            result = backtest_wallet("0xwallet", slippage_bps=150)
        mock_batch.assert_not_called()
        assert result["n_resolved"] == 1

    def test_batch_resolve_true_resolves_distinct_markets_in_one_call(self):
        trades = [
            _raw_trade("0xabc", "BUY", 0.50, 10, "Yes"),
            _raw_trade("0xdef", "BUY", 0.20, 5, "No"),
            _raw_trade("0xabc", "BUY", 0.60, 3, "Yes"),  # same market again
        ]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution",
        ) as mock_single, patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
            return_value={"0xabc": True, "0xdef": False},
        ) as mock_batch:
            result = backtest_wallet("0xwallet", slippage_bps=150, batch_resolve=True)
        mock_batch.assert_called_once_with(["0xabc", "0xdef"])
        mock_single.assert_not_called()
        assert result["n_resolved"] == 3

    def test_batch_resolve_true_skips_markets_already_in_cache(self):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        cache = {"0xabc": True}
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
        ) as mock_batch:
            result = backtest_wallet(
                "0xwallet", slippage_bps=150, cache=cache, batch_resolve=True,
            )
        mock_batch.assert_not_called()
        assert result["n_resolved"] == 1

    def test_batch_resolve_unresolved_market_not_cached_as_resolved(self):
        # Poisoning-invariant regression at the backtest_wallet layer
        # (issue #1221 invariant, exercised through the #1227 batched path):
        # a market the batch fetcher reports as None (unresolved) must be
        # dropped from this run's results, not silently treated as a loss.
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        cache: dict = {}
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
            return_value={"0xabc": None},
        ):
            result = backtest_wallet(
                "0xwallet", slippage_bps=150, cache=cache, batch_resolve=True,
            )
        assert result["n_resolved"] == 0
        assert result["n_unresolved_dropped"] == 1
        assert cache["0xabc"] is None

    def test_batch_resolve_produces_identical_stats_to_unbatched_path(self):
        # Same fixture trades and resolutions, once through the unbatched
        # per-market path and once through batch_resolve=True -- results
        # must be identical (issue #1227's "batching is additive, not a
        # rewrite" acceptance criterion).
        trades = [
            _raw_trade("0xabc", "BUY", 0.50, 10, "Yes"),
            _raw_trade("0xdef", "BUY", 0.20, 5, "No"),
        ]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            unbatched = backtest_wallet("0xwallet", slippage_bps=150, cache={})

        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
            return_value={"0xabc": True, "0xdef": True},
        ):
            batched = backtest_wallet(
                "0xwallet", slippage_bps=150, cache={}, batch_resolve=True,
            )

        assert batched == unbatched


class TestNoDatabaseDependency:
    """issue #1221: copy_trade_backtest.py must stay runnable as a
    standalone CLI with no Database available -- the persistent cache is
    entirely owned/optional at the copy_wallet_screening.py caller layer.
    """

    def test_module_does_not_import_database(self):
        import src.scripts.copy_trade_backtest as mod
        assert not hasattr(mod, "Database")

    def test_backtest_wallet_runs_with_no_cache_and_no_db(self):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            result = backtest_wallet("0xwallet", slippage_bps=150)
        assert result["n_resolved"] == 1


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
        assert "median ROI" in report
        assert "15.0%" in report  # copier median_roi rendered in the table

    def test_flat_stake_column_omitted_when_not_used(self):
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
        assert "flat stake" not in report

    def test_flat_stake_column_rendered_when_present(self):
        results = [{
            "address": "0x1234567890",
            "n_buy_trades": 5, "n_sell_excluded": 1, "n_resolved": 4,
            "n_unresolved_dropped": 1,
            "trader": {"n": 4, "win_rate": 0.75, "mean_roi": 0.3, "median_roi": 0.2,
                       "dollar_pnl": 12.5},
            "copier": {"n": 4, "win_rate": 0.75, "mean_roi": 0.25, "median_roi": 0.15,
                       "dollar_pnl": 10.0},
            "copier_flat": {"n": 4, "win_rate": 0.75, "mean_roi": 0.25, "median_roi": 0.15,
                             "dollar_pnl": 3.33},
        }]
        report = build_report("2026-09-16", 150.0, results, flat_stake=5.0)
        assert "flat stake" in report
        assert "$5.00/trade" in report
        assert "$3.33" in report


class TestRun:
    def test_flat_stake_wired_through_to_report(self, tmp_path):
        trades = [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")]
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            rc = run(
                window="month", top=20, wallets=["0xwallet"], slippage_bps=150.0,
                out_dir=tmp_path, run_date="2026-09-16", flat_stake=5.0,
            )
        assert rc == 0
        report = (tmp_path / "copy_trade_backtest_2026-09-16.md").read_text()
        assert "flat stake" in report
        assert "$5.00/trade" in report

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

    def test_min_trades_filters_out_small_sample_wallets(self, tmp_path):
        trades_by_wallet = {
            "0xbig": [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")] * 5,
            "0xsmall": [_raw_trade("0xabc", "BUY", 0.50, 10, "Yes")],
        }
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades",
            side_effect=lambda address, *a, **kw: trades_by_wallet[address],
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            rc = run(
                window="month", top=20, wallets=["0xbig", "0xsmall"], slippage_bps=150.0,
                out_dir=tmp_path, run_date="2026-09-16", min_trades=3,
            )
        assert rc == 0
        report = (tmp_path / "copy_trade_backtest_2026-09-16.md").read_text()
        assert "0xbig" in report
        assert "0xsmall" not in report

    def test_results_sorted_by_copier_median_roi_not_mean(self, tmp_path):
        # 0xtail_mean: 9 losers (-100% ROI each) + one huge longshot winner
        # -> mean ROI is enormous (~880%) but median ROI is -100% (the
        # typical trade loses). 0xsteady_median: 10 uniform, modest winners
        # -> mean == median (~64%). Regression test for the real bug found
        # against live data: sorting by mean alone ranks the lottery wallet
        # #1; median correctly ranks the steady wallet #1 instead.
        tail_trades = (
            [_raw_trade("0xm", "BUY", 0.90, 10, "No")] * 9
            + [_raw_trade("0xm", "BUY", 0.01, 10, "Yes")]
        )
        steady_trades = [_raw_trade("0xm", "BUY", 0.60, 10, "Yes")] * 10
        trades_by_wallet = {"0xtail_mean": tail_trades, "0xsteady_median": steady_trades}
        with patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades",
            side_effect=lambda address, *a, **kw: trades_by_wallet[address],
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True
        ):
            rc = run(
                window="month", top=20, wallets=["0xtail_mean", "0xsteady_median"],
                slippage_bps=150.0, out_dir=tmp_path, run_date="2026-09-16",
            )
        assert rc == 0
        report = (tmp_path / "copy_trade_backtest_2026-09-16.md").read_text()
        # build_report truncates addresses to 10 chars in the table.
        assert report.index("0xsteady_m") < report.index("0xtail_mea")
