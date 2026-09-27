"""Unit tests for src/scripts/copy_wallet_screening.py (epic #1099 story 2).
Database and Polymarket I/O are mocked -- no real network/DB calls, mirroring
test_copy_trade_backtest.py's mocking style.
"""
import logging
from unittest.mock import MagicMock, patch

from src.scripts.copy_wallet_screening import (
    MAX_WALLETS_PER_RUN,
    QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO,
    QUALITY_MAX_TOTAL_TRADES,
    PersistentResolutionCache,
    check_quality,
    check_stability,
    run,
)


def _leaderboard(n: int) -> "list[dict]":
    return [{"proxyWallet": f"0xwallet{i}"} for i in range(n)]


def _raw_trade(market, side, price, size, outcome, ts=1700000000, outcome_index=None):
    """Mirrors test_copy_trade_backtest.py's own helper -- a raw trade
    record shaped as normalize_trade() (src/data/polymarket_traders.py)
    expects it, for the resolution-caching tests below that exercise the
    real backtest_wallet()/resolve_payout() path instead of mocking
    backtest_wallet wholesale.
    """
    raw = {
        "conditionId": market, "side": side, "price": str(price),
        "size": str(size), "timestamp": str(ts), "outcome": outcome,
    }
    if outcome_index is not None:
        raw["outcomeIndex"] = outcome_index
    return raw


def _backtest_result(
    address, n_buy_trades=5, n_sell_excluded=0, n_resolved=5, win_rate=0.6,
    mean_roi=0.1, median_roi=0.1, dollar_pnl=10.0, flat_dollar_pnl=None,
) -> dict:
    stats = {
        "n": n_resolved, "win_rate": win_rate, "mean_roi": mean_roi,
        "median_roi": median_roi, "dollar_pnl": dollar_pnl,
    }
    result = {
        "address": address,
        "n_buy_trades": n_buy_trades,
        "n_sell_excluded": n_sell_excluded,
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


def _quality_row(
    flat_dollar_pnl=10.0, median_roi=0.11, mean_roi=0.1, n_buy_trades=5,
    n_sell_excluded=0,
) -> dict:
    """A `current` dict that passes every check_quality() condition by
    default -- override individual fields to exercise one failure at a time.
    """
    return {
        "flat_dollar_pnl": flat_dollar_pnl,
        "median_roi": median_roi,
        "mean_roi": mean_roi,
        "n_buy_trades": n_buy_trades,
        "n_sell_excluded": n_sell_excluded,
    }


class TestCheckQuality:
    """check_quality() (issue #1209, truncation reproducibility relaxed by
    issue #1217) -- a composable, separate predicate from check_stability():
    reproducibility (stability) is necessary but not sufficient for
    eligibility, quality is the other half.

    None of conditions (a)-(c) below look at `previous` at all -- only
    condition (d)'s truncation branch does -- so every non-truncation test
    passes `previous=None` to prove that.
    """

    def test_all_conditions_pass(self):
        assert check_quality(_quality_row(), None) == (True, "ok")

    def test_flat_dollar_pnl_negative_fails(self):
        row = _quality_row(flat_dollar_pnl=-5612.90)
        assert check_quality(row, None) == (False, "flat_dollar_pnl_not_positive")

    def test_flat_dollar_pnl_zero_fails(self):
        row = _quality_row(flat_dollar_pnl=0.0)
        assert check_quality(row, None) == (False, "flat_dollar_pnl_not_positive")

    def test_flat_dollar_pnl_missing_fails(self):
        # e.g. the run was invoked without --flat-stake -- profitability
        # under flat-stake copying can't be confirmed, so it can't pass.
        row = _quality_row(flat_dollar_pnl=None)
        assert check_quality(row, None) == (False, "flat_dollar_pnl_not_positive")

    def test_median_roi_negative_one_fails_quality(self):
        # Regression fixture for the 2026-09-25 audit finding: 6 of 12
        # `eligible_to_follow=1` wallets had median_roi=-1.0 (catastrophic
        # but *consistently* catastrophic, so they passed stability alone).
        row = _quality_row(median_roi=-1.0, mean_roi=-1.0)
        assert check_quality(row, None) == (False, "median_roi_not_positive")

    def test_median_roi_zero_fails(self):
        row = _quality_row(median_roi=0.0, mean_roi=0.0)
        assert check_quality(row, None) == (False, "median_roi_not_positive")

    def test_tail_driven_pnl_0xa38a455b_numbers(self):
        # Regression fixture: 0xa38a455b was promoted on median_roi=+0.0157
        # but mean_roi=+0.0871 (a 5.5x mean/median ratio) and went on to
        # lose 84% of staked capital in paper.
        row = _quality_row(median_roi=0.0157, mean_roi=0.0871)
        assert check_quality(row, None) == (False, "tail_driven_pnl")

    def test_mean_median_ratio_exactly_at_boundary_passes(self):
        row = _quality_row(median_roi=0.10, mean_roi=0.30)  # exactly 3.0x
        assert check_quality(row, None) == (True, "ok")

    def test_mean_median_ratio_just_above_boundary_fails(self):
        row = _quality_row(median_roi=0.10, mean_roi=0.30001)
        assert check_quality(row, None) == (False, "tail_driven_pnl")

    def test_tail_ratio_not_applied_when_median_roi_not_positive(self):
        # median_roi <= 0 must fail on median_roi_not_positive, not
        # tail_driven_pnl, even though mean/median would exceed the ratio
        # threshold if it were computed naively (interaction documented in
        # check_quality()'s docstring).
        row = _quality_row(median_roi=-0.01, mean_roi=1.0)
        assert check_quality(row, None) == (False, "median_roi_not_positive")

    def test_total_trades_just_under_cap_passes(self):
        row = _quality_row(n_buy_trades=10500, n_sell_excluded=9499)
        assert check_quality(row, None) == (True, "ok")

    def test_high_n_buy_trades_alone_does_not_trigger_truncation(self):
        # Regression test for the unit-mismatch bug (PR #1211 review): a
        # wallet with n_buy_trades=10,500 (the observed ceiling for
        # genuinely-truncated wallets under a ~50/50 buy/sell split) but a
        # small n_sell_excluded was NOT actually truncated -- comparing
        # n_buy_trades alone against the cap would incorrectly pass this
        # wallet and reject nothing (the bug: the cap was always > 10,500).
        # This proves the gate discriminates rather than just rejecting
        # high-volume wallets.
        row = _quality_row(n_buy_trades=10500, n_sell_excluded=100)
        assert check_quality(row, None) == (True, "ok")

    def test_quality_max_mean_median_ratio_constant_is_three(self):
        # Guards against silently re-deriving the acceptance-criteria value.
        assert QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO == 3.0


class TestCheckQualityTruncationReproducibility:
    """issue #1217: a truncated wallet (condition (d)) is no longer an
    unconditional reject -- it is admitted only when its flat-stake edge
    also reproduced in the immediately-previous screening run.
    """

    def _truncated_row(self, flat_dollar_pnl=10.0):
        # n_buy_trades + n_sell_excluded == the fetch cap exactly.
        row = _quality_row(
            flat_dollar_pnl=flat_dollar_pnl, n_buy_trades=10500, n_sell_excluded=9500,
        )
        assert row["n_buy_trades"] + row["n_sell_excluded"] == QUALITY_MAX_TOTAL_TRADES
        return row

    def test_truncated_current_and_previous_both_positive_is_eligible(self):
        row = self._truncated_row(flat_dollar_pnl=10.0)
        previous = {"flat_dollar_pnl": 5.0}
        assert check_quality(row, previous) == (True, "ok")

    def test_truncated_0x5268527977_previous_negative_is_ineligible(self):
        # Regression fixture from issue #1217: 0x5268527977 passed
        # check_stability() (n_resolved steady at ~10,300) but its
        # flat_dollar_pnl swung -1455 -> -1258 -> -671 -> +1129 across runs
        # -- the exact case the unconditional truncation reject failed to
        # catch (check_stability() only looks at median_roi/n_resolved) and
        # the reproducibility rule must still reject.
        row = self._truncated_row(flat_dollar_pnl=1129.0)
        previous = {"flat_dollar_pnl": -671.0}
        assert check_quality(row, previous) == (False, "trade_history_truncated_unreproducible")

    def test_truncated_no_previous_run_is_ineligible_with_distinct_reason(self):
        row = self._truncated_row(flat_dollar_pnl=10.0)
        assert check_quality(row, None) == (False, "trade_history_truncated_no_previous_run")

    def test_truncated_previous_flat_dollar_pnl_none_is_ineligible_no_exception(self):
        # The previous run was invoked without --flat-stake -- flat_dollar_pnl
        # is null on that row. Must not raise, and must fail the same way as
        # having no previous run at all (nothing to compare against).
        row = self._truncated_row(flat_dollar_pnl=10.0)
        previous = {"flat_dollar_pnl": None}
        assert check_quality(row, previous) == (False, "trade_history_truncated_no_previous_run")

    def test_truncated_previous_flat_dollar_pnl_zero_is_ineligible(self):
        row = self._truncated_row(flat_dollar_pnl=10.0)
        previous = {"flat_dollar_pnl": 0.0}
        assert check_quality(row, previous) == (False, "trade_history_truncated_unreproducible")

    def test_truncated_but_failing_condition_a_fails_on_a_not_truncation(self):
        # Truncated AND current flat_dollar_pnl not positive -- must fail on
        # (a), which is checked first, not fall through to the truncation
        # branch at all.
        row = self._truncated_row(flat_dollar_pnl=-5.0)
        previous = {"flat_dollar_pnl": 5.0}
        assert check_quality(row, previous) == (False, "flat_dollar_pnl_not_positive")

    def test_truncated_but_failing_condition_c_fails_on_c_not_truncation(self):
        # Truncated AND median_roi not positive -- must fail on (c), before
        # ever reaching the truncation branch.
        row = self._truncated_row(flat_dollar_pnl=10.0)
        row["median_roi"] = 0.0
        row["mean_roi"] = 0.0
        previous = {"flat_dollar_pnl": 5.0}
        assert check_quality(row, previous) == (False, "median_roi_not_positive")


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
        # Stable AND passes every quality condition (issue #1209): positive
        # flat_dollar_pnl, positive median_roi, mean/median ratio within
        # QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO, n_buy_trades under the cap.
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = [
            {"median_roi": 0.10, "n_resolved": 100},
        ]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result(
                "0xwallet0", n_resolved=105, median_roi=0.11, mean_roi=0.1,
                flat_dollar_pnl=10.0,
            ),
        ):
            run(window="month", top=1, slippage_bps=150.0, flat_stake=5.0, db=db)
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


class TestEligibilityQualityGate:
    """End-to-end (through run()) coverage of the issue #1209 quality gate:
    eligible_to_follow now requires check_stability() AND check_quality()
    to both pass, and a row is always written regardless of the outcome.
    """

    def _run_stable(self, db, backtest_result, previous, flat_stake=5.0, caplog=None):
        leaderboard = _leaderboard(1)
        db.get_recent_wallet_screenings.return_value = [previous]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=backtest_result,
        ):
            rc = run(window="month", top=1, slippage_bps=150.0, flat_stake=flat_stake, db=db)
        return rc

    def test_stable_and_all_quality_conditions_pass_is_eligible(self):
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_resolved=105, median_roi=0.11, mean_roi=0.1,
            flat_dollar_pnl=10.0,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 1
        db.insert_wallet_screening.assert_called_once()

    def test_stable_but_negative_flat_dollar_pnl_is_ineligible_and_logged(self, caplog):
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_resolved=105, median_roi=0.11, mean_roi=0.1,
            flat_dollar_pnl=-5612.90,
        )
        with caplog.at_level("INFO", logger="src.scripts.copy_wallet_screening"):
            self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0
        db.insert_wallet_screening.assert_called_once()
        assert "flat_dollar_pnl_not_positive" in caplog.text
        assert "[copy-wallet-screening]" in caplog.text

    def test_stable_but_median_roi_negative_one_is_ineligible(self):
        # Regression test for the audit's 6-of-12 finding: consistently
        # catastrophic (stable) wallets must not be eligible.
        db = MagicMock()
        previous = {"median_roi": -1.0, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_resolved=105, median_roi=-1.0, mean_roi=-1.0,
            flat_dollar_pnl=10.0,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0
        db.insert_wallet_screening.assert_called_once()

    def test_stable_but_tail_driven_pnl_is_ineligible(self):
        # Regression fixture: the real 0xa38a455b numbers (median_roi
        # +0.0157, mean_roi +0.0871 -- a 5.5x ratio).
        db = MagicMock()
        previous = {"median_roi": 0.0157, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_resolved=105, median_roi=0.0157, mean_roi=0.0871,
            flat_dollar_pnl=10.0,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0
        db.insert_wallet_screening.assert_called_once()

    def test_stable_but_truncated_history_is_ineligible(self):
        # Realistic ceiling fixture (PR #1211 review): a genuinely-truncated
        # wallet observed in production sits at n_buy_trades=10,500 with a
        # large n_sell_excluded -- truncation is on the TOTAL fetched
        # trades, not n_buy_trades alone.
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_buy_trades=10500, n_sell_excluded=9500, n_resolved=105,
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=10.0,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0
        db.insert_wallet_screening.assert_called_once()

    def test_stable_high_n_buy_trades_but_not_truncated_is_eligible(self):
        # Discriminates the fix from the bug it replaces: a wallet with the
        # same high n_buy_trades=10,500 but a genuinely small
        # n_sell_excluded was NOT truncated and should pass.
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_buy_trades=10500, n_sell_excluded=100, n_resolved=105,
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=10.0,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 1
        db.insert_wallet_screening.assert_called_once()

    def test_truncated_history_eligible_when_previous_flat_pnl_also_positive(self):
        # issue #1217: a truncated wallet is no longer an unconditional
        # reject -- two consecutive positive flat-stake runs admit it.
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 100, "flat_dollar_pnl": 5.0}
        result = _backtest_result(
            "0xwallet0", n_buy_trades=10500, n_sell_excluded=9500, n_resolved=105,
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=10.0,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 1
        db.insert_wallet_screening.assert_called_once()

    def test_truncated_history_ineligible_0x5268527977_previous_negative(self):
        # Regression fixture from issue #1217: 0x5268527977 passed
        # check_stability() (n_resolved steady at ~10,300) but its
        # flat_dollar_pnl swung -1455 -> -1258 -> -671 -> +1129 across runs.
        # The unconditional truncation reject would have caught this by
        # accident; the reproducibility rule must still reject it, and for
        # the right reason (unreproducible, not "no previous run").
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 10300, "flat_dollar_pnl": -671.0}
        result = _backtest_result(
            "0xwallet0", n_buy_trades=10500, n_sell_excluded=9500, n_resolved=10350,
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=1129.0,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0
        db.insert_wallet_screening.assert_called_once()

    def test_quality_passes_but_stability_fails_is_ineligible(self):
        # Quality alone is not sufficient -- stability is still necessary.
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []  # first-ever run: unstable
        leaderboard = _leaderboard(1)
        result = _backtest_result(
            "0xwallet0", n_resolved=105, median_roi=0.11, mean_roi=0.1,
            flat_dollar_pnl=10.0,
        )
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet", return_value=result,
        ):
            run(window="month", top=1, slippage_bps=150.0, flat_stake=5.0, db=db)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0
        db.insert_wallet_screening.assert_called_once()


class TestFlatStakeWarning:
    """PR #1211 review: without --flat-stake, every wallet fails the quality
    gate on flat_dollar_pnl -- that must read as one bad invocation, not N
    bad wallets, so run() logs a single up-front WARNING.
    """

    def test_warns_once_when_flat_stake_not_set(self, caplog):
        leaderboard = _leaderboard(2)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with caplog.at_level("WARNING", logger="src.scripts.copy_wallet_screening"), patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=lambda addr, *a, **kw: _backtest_result(addr),
        ):
            run(window="month", top=2, slippage_bps=150.0, flat_stake=None, db=db)
        warnings = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "flat_stake is not set" in r.message
        ]
        assert len(warnings) == 1

    def test_no_warning_when_flat_stake_set(self, caplog):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with caplog.at_level("WARNING", logger="src.scripts.copy_wallet_screening"), patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0", flat_dollar_pnl=10.0),
        ):
            run(window="month", top=1, slippage_bps=150.0, flat_stake=5.0, db=db)
        assert "flat_stake is not set" not in caplog.text


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


class TestPersistentResolutionCache:
    """issue #1221: dict-compatible cache passed straight through as
    backtest_wallet()'s/resolve_payout()'s `cache` argument -- these tests
    exercise the class directly, in isolation from the full run() pipeline.
    """

    def test_starts_with_zero_hits_and_misses(self):
        cache = PersistentResolutionCache()
        assert cache.hits == 0
        assert cache.misses == 0

    def test_no_db_behaves_like_plain_dict_tier1_only(self):
        cache = PersistentResolutionCache()
        assert "0xabc" not in cache
        cache["0xabc"] = True
        assert "0xabc" in cache
        assert cache["0xabc"] is True
        assert cache.misses == 1
        assert cache.hits == 1

    def test_unresolved_market_not_persisted_to_db(self):
        # Single most important correctness property (issue #1221): an
        # unresolved market (None) must NEVER reach the persistent table --
        # a negative entry would permanently poison it once it does resolve.
        db = MagicMock()
        db.get_cached_market_resolution.return_value = None
        cache = PersistentResolutionCache(db)
        assert "0xunresolved" not in cache
        cache["0xunresolved"] = None
        db.cache_market_resolution.assert_not_called()

    def test_unresolved_market_still_cached_in_memory_for_this_run(self):
        # Ephemeral, run-scoped only -- avoids a second network call for the
        # same still-unresolved market later in the same run, but (per the
        # test above) is never written to the DB.
        db = MagicMock()
        db.get_cached_market_resolution.return_value = None
        cache = PersistentResolutionCache(db)
        cache["0xunresolved"] = None
        assert "0xunresolved" in cache
        assert cache["0xunresolved"] is None

    def test_resolved_market_is_persisted_to_db(self):
        db = MagicMock()
        cache = PersistentResolutionCache(db)
        cache["0xabc"] = True
        db.cache_market_resolution.assert_called_once_with("0xabc", True)

    def test_resolved_false_is_also_persisted_to_db(self):
        # False is a real resolved answer, not "missing" -- must be
        # persisted just like True.
        db = MagicMock()
        cache = PersistentResolutionCache(db)
        cache["0xabc"] = False
        db.cache_market_resolution.assert_called_once_with("0xabc", False)

    def test_db_hit_avoids_second_db_query_within_same_run(self):
        db = MagicMock()
        db.get_cached_market_resolution.return_value = True
        cache = PersistentResolutionCache(db)
        assert "0xabc" in cache  # first check -- consults db
        assert "0xabc" in cache  # second check -- served from the promoted in-memory entry
        db.get_cached_market_resolution.assert_called_once_with("0xabc")

    def test_db_hit_counts_as_a_hit_not_a_miss(self):
        db = MagicMock()
        db.get_cached_market_resolution.return_value = False
        cache = PersistentResolutionCache(db)
        assert "0xabc" in cache
        assert cache.hits == 1
        assert cache.misses == 0

    def test_db_miss_falls_through_and_counts_as_a_miss(self):
        db = MagicMock()
        db.get_cached_market_resolution.return_value = None
        cache = PersistentResolutionCache(db)
        assert "0xabc" not in cache
        assert cache.misses == 1
        assert cache.hits == 0


class TestResolutionCachingAcrossRun:
    """issue #1221 tiers 1 (run-scoped, cross-wallet) and 2 (persistent,
    cross-run), exercised through the real run() -> backtest_wallet() ->
    resolve_payout() path (only get_wallet_trades and fetch_market_resolution
    are mocked, at the network boundary).
    """

    def test_two_wallets_sharing_market_fetches_resolution_once(self):
        leaderboard = _leaderboard(2)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        db.get_cached_market_resolution.return_value = None
        trades = [_raw_trade("0xshared", "BUY", 0.5, 10, "Yes")]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades,
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True,
        ) as mock_fn:
            rc = run(window="month", top=2, slippage_bps=150.0, db=db)
        assert rc == 0
        mock_fn.assert_called_once_with("0xshared")

    def test_market_already_in_persistent_cache_makes_zero_network_calls(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        db.get_cached_market_resolution.return_value = True  # already resolved, tier 2
        trades = [_raw_trade("0xabc", "BUY", 0.5, 10, "Yes")]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades,
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution",
        ) as mock_fn:
            rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0
        mock_fn.assert_not_called()

    def test_unresolved_market_never_written_to_persistent_cache(self):
        # Regression/poisoning-risk test: a market that hasn't resolved yet
        # must never get a row in market_resolutions -- it would permanently
        # misclassify the market once it does resolve (issue #1221).
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        db.get_cached_market_resolution.return_value = None
        trades = [_raw_trade("0xabc", "BUY", 0.5, 10, "Yes")]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades,
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=None,
        ):
            rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0
        db.cache_market_resolution.assert_not_called()

    def test_hit_miss_counts_logged_once_per_run(self, caplog):
        leaderboard = _leaderboard(2)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        db.get_cached_market_resolution.return_value = None
        trades = [_raw_trade("0xshared", "BUY", 0.5, 10, "Yes")]
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_trade_backtest.get_wallet_trades", return_value=trades,
        ), patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution", return_value=True,
        ):
            with caplog.at_level(logging.INFO):
                rc = run(window="month", top=2, slippage_bps=150.0, db=db)
        assert rc == 0
        assert caplog.text.count("resolution cache:") == 1
        assert "1 hits, 1 misses" in caplog.text
