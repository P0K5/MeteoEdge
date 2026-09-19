"""Unit tests for src/scripts/copy_wallet_screening.py (epic #1099 story 2).
Database and Polymarket I/O are mocked -- no real network/DB calls, mirroring
test_copy_trade_backtest.py's mocking style.
"""
from unittest.mock import MagicMock, patch

from src.scripts.copy_wallet_screening import (
    MAX_WALLETS_PER_RUN,
    check_stability,
    run,
)


def _leaderboard(n: int) -> "list[dict]":
    return [{"proxyWallet": f"0xwallet{i}"} for i in range(n)]


def _backtest_result(
    address, n_buy_trades=5, n_resolved=5, win_rate=0.6, mean_roi=0.1,
    median_roi=0.1, dollar_pnl=10.0, flat_dollar_pnl=None,
) -> dict:
    stats = {
        "n": n_resolved, "win_rate": win_rate, "mean_roi": mean_roi,
        "median_roi": median_roi, "dollar_pnl": dollar_pnl,
    }
    result = {
        "address": address,
        "n_buy_trades": n_buy_trades,
        "n_resolved": n_resolved,
        "copier": dict(stats),
        "trader": dict(stats),
    }
    if flat_dollar_pnl is not None:
        result["copier_flat"] = {**stats, "dollar_pnl": flat_dollar_pnl}
    return result


class TestCheckStability:
    def test_first_run_is_unstable(self):
        assert check_stability({"median_roi": 0.1, "n_resolved": 10}, None) is False

    def test_stable_when_sign_matches_and_within_tolerance(self):
        previous = {"median_roi": 0.10, "n_resolved": 100}
        current = {"median_roi": 0.12, "n_resolved": 110}
        assert check_stability(current, previous) is True

    def test_reversal_fixture_0xd3b034d7(self):
        # Regression fixture: the spike's own instability finding that
        # motivated this story -- n_resolved 7,498 -> 2,271, median ROI
        # +33.4% -> -100% across two runs 15 hours apart.
        previous = {"median_roi": 0.334, "n_resolved": 7498}
        current = {"median_roi": -1.0, "n_resolved": 2271}
        assert check_stability(current, previous) is False

    def test_sign_only_flip_is_unstable(self):
        previous = {"median_roi": 0.05, "n_resolved": 100}
        current = {"median_roi": -0.05, "n_resolved": 100}
        assert check_stability(current, previous) is False

    def test_volume_only_swing_is_unstable(self):
        previous = {"median_roi": 0.10, "n_resolved": 100}
        current = {"median_roi": 0.10, "n_resolved": 200}
        assert check_stability(current, previous) is False

    def test_volume_swing_exactly_at_boundary_is_stable(self):
        previous = {"median_roi": 0.10, "n_resolved": 100}
        current = {"median_roi": 0.10, "n_resolved": 125}  # exactly 25%
        assert check_stability(current, previous) is True

    def test_zero_median_roi_never_matches_another_zero(self):
        previous = {"median_roi": 0.0, "n_resolved": 100}
        current = {"median_roi": 0.0, "n_resolved": 100}
        assert check_stability(current, previous) is False

    def test_zero_previous_n_resolved_does_not_divide_by_zero(self):
        previous = {"median_roi": 0.10, "n_resolved": 0}
        current = {"median_roi": 0.10, "n_resolved": 1}
        # max(previous_n, 1) floors the denominator -- must not raise.
        assert check_stability(current, previous) is False  # 1/1 = 100% > 25%


class TestRunPersistence:
    def test_inserts_one_row_per_screened_wallet(self):
        leaderboard = _leaderboard(3)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=lambda addr, *a, **kw: _backtest_result(addr),
        ):
            rc = run(
                window="month", top=20, slippage_bps=150.0, db=db,
                screened_at="2026-09-19T00:00:00+00:00",
            )
        assert rc == 0
        assert db.insert_wallet_screening.call_count == 3

    def test_insert_call_args_match_expected_fields(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result(
                "0xwallet0", n_buy_trades=8, n_resolved=6, win_rate=0.5,
                mean_roi=0.2, median_roi=0.15, dollar_pnl=12.34,
            ),
        ):
            rc = run(
                window="week", top=1, slippage_bps=200.0, flat_stake=None, db=db,
                screened_at="2026-09-19T00:00:00+00:00",
            )
        assert rc == 0
        db.insert_wallet_screening.assert_called_once_with(
            address="0xwallet0", window="week", screened_at="2026-09-19T00:00:00+00:00",
            n_buy_trades=8, n_resolved=6, win_rate=0.5, mean_roi=0.2, median_roi=0.15,
            mirrored_dollar_pnl=12.34, flat_dollar_pnl=None, flat_stake=None,
            slippage_bps=200.0, eligible_to_follow=0,
        )

    def test_flat_dollar_pnl_extracted_from_copier_flat_when_flat_stake_set(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0", flat_dollar_pnl=3.21),
        ):
            run(window="month", top=1, slippage_bps=150.0, flat_stake=5.0, db=db)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["flat_dollar_pnl"] == 3.21
        assert kwargs["flat_stake"] == 5.0

    def test_flat_dollar_pnl_none_when_flat_stake_not_set(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0"),
        ):
            run(window="month", top=1, slippage_bps=150.0, flat_stake=None, db=db)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["flat_dollar_pnl"] is None
        assert kwargs["flat_stake"] is None

    def test_stability_check_uses_immediately_prior_row(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []  # first-ever run
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0"),
        ):
            run(window="month", top=1, slippage_bps=150.0, db=db)
        db.get_recent_wallet_screenings.assert_called_once_with("0xwallet0", limit=1)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0

    def test_eligible_to_follow_set_when_stable(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = [
            {"median_roi": 0.10, "n_resolved": 100},
        ]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0", n_resolved=105, median_roi=0.11),
        ):
            run(window="month", top=1, slippage_bps=150.0, db=db)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 1

    def test_wallet_persisted_even_when_unstable_not_a_skipped_write(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = [
            {"median_roi": -0.5, "n_resolved": 50},
        ]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0", median_roi=0.3, n_resolved=50),
        ):
            run(window="month", top=1, slippage_bps=150.0, db=db)
        db.insert_wallet_screening.assert_called_once()
        assert db.insert_wallet_screening.call_args.kwargs["eligible_to_follow"] == 0

    def test_empty_leaderboard_returns_error_and_persists_nothing(self):
        db = MagicMock()
        with patch("src.scripts.copy_wallet_screening.get_leaderboard", return_value=[]):
            rc = run(window="month", top=20, slippage_bps=150.0, db=db)
        assert rc == 1
        db.insert_wallet_screening.assert_not_called()


class TestMinTrades:
    def test_min_trades_skips_persistence_for_small_sample_wallets(self):
        leaderboard = _leaderboard(2)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        results_by_addr = {
            "0xwallet0": _backtest_result("0xwallet0", n_resolved=10),
            "0xwallet1": _backtest_result("0xwallet1", n_resolved=1),
        }
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=lambda addr, *a, **kw: results_by_addr[addr],
        ):
            rc = run(window="month", top=2, slippage_bps=150.0, min_trades=5, db=db)
        assert rc == 0
        assert db.insert_wallet_screening.call_count == 1
        assert db.insert_wallet_screening.call_args.kwargs["address"] == "0xwallet0"

    def test_default_min_trades_does_not_filter_anything(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0", n_resolved=0),
        ):
            rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0
        db.insert_wallet_screening.assert_called_once()


class TestMaxWalletsPerRun:
    def test_top_above_cap_is_clamped_before_calling_leaderboard(self):
        leaderboard = _leaderboard(MAX_WALLETS_PER_RUN)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ) as mock_lb, patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=lambda addr, *a, **kw: _backtest_result(addr),
        ):
            rc = run(window="month", top=500, slippage_bps=150.0, db=db)
        assert rc == 0
        mock_lb.assert_called_once_with(window="month", limit=MAX_WALLETS_PER_RUN)
        assert db.insert_wallet_screening.call_count == MAX_WALLETS_PER_RUN

    def test_oversized_leaderboard_response_is_still_truncated_to_cap(self):
        # Defensive: even if get_leaderboard ignores `limit` and returns more
        # than MAX_WALLETS_PER_RUN addresses, persistence still stops at the cap.
        leaderboard = _leaderboard(MAX_WALLETS_PER_RUN + 20)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=lambda addr, *a, **kw: _backtest_result(addr),
        ):
            rc = run(window="month", top=20, slippage_bps=150.0, db=db)
        assert rc == 0
        assert db.insert_wallet_screening.call_count == MAX_WALLETS_PER_RUN

    def test_top_within_cap_is_not_clamped(self):
        leaderboard = _leaderboard(10)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ) as mock_lb, patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=lambda addr, *a, **kw: _backtest_result(addr),
        ):
            rc = run(window="month", top=10, slippage_bps=150.0, db=db)
        assert rc == 0
        mock_lb.assert_called_once_with(window="month", limit=10)
