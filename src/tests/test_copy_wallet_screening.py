"""Unit tests for src/scripts/copy_wallet_screening.py (epic #1099 story 2).
Database and Polymarket I/O are mocked -- no real network/DB calls, mirroring
test_copy_trade_backtest.py's mocking style.
"""
import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.scripts.copy_wallet_screening import (
    MAX_WALLETS_PER_RUN,
    QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO,
    PersistentResolutionCache,
    check_quality,
    check_stability,
    lock_acquired,
    main,
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
    truncated=False,
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
        # Issue #1233: backtest_wallet()'s own truncated passthrough (see
        # test_copy_trade_backtest.py for that threading's own coverage) --
        # defaults to False so every existing caller of this helper that
        # doesn't care about truncation is unaffected.
        "truncated": truncated,
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

    def test_none_current_median_roi_is_unstable_not_raising(self):
        # Issue #1245: a wallet with zero resolved trades has
        # median_roi=None (_stats(), copy_trade_backtest.py) -- must be
        # treated as unstable, not raise inside sign().
        previous = {"median_roi": 0.10, "n_resolved": 100}
        current = {"median_roi": None, "n_resolved": 0}
        assert check_stability(current, previous) is False

    def test_none_previous_median_roi_is_unstable_not_raising(self):
        previous = {"median_roi": None, "n_resolved": 0}
        current = {"median_roi": 0.10, "n_resolved": 100}
        assert check_stability(current, previous) is False


def _quality_row(
    flat_dollar_pnl=10.0, median_roi=0.11, mean_roi=0.1, truncated=False,
) -> dict:
    """A `current` dict that passes every check_quality() condition by
    default -- override individual fields to exercise one failure at a time.

    ``truncated`` (issue #1233) replaces the old ``n_buy_trades``/
    ``n_sell_excluded`` count fields entirely -- condition (d) now consumes
    the fetcher's own reported flag directly, not an arithmetic comparison
    against a constant.
    """
    return {
        "flat_dollar_pnl": flat_dollar_pnl,
        "median_roi": median_roi,
        "mean_roi": mean_roi,
        "truncated": truncated,
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

    def test_untruncated_high_volume_wallet_passes_unconditionally(self):
        # Issue #1233's own discriminating test, at the check_quality()
        # level: a high trade count alone means nothing now -- only the
        # fetcher's own truncated=False observation matters. This is the
        # regression both previous fixes (#1209/#1211's wrong-units
        # constant, and this issue's own inert page-cap constant) missed:
        # a wallet fetched to genuine exhaustion (a short/empty final page)
        # must pass regardless of how many trades that turned out to be.
        row = _quality_row(truncated=False)
        assert check_quality(row, None) == (True, "ok")

    def test_quality_max_mean_median_ratio_constant_is_three(self):
        # Guards against silently re-deriving the acceptance-criteria value.
        assert QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO == 3.0


class TestCheckQualityTruncationReproducibility:
    """issue #1217: a truncated wallet (condition (d)) is no longer an
    unconditional reject -- it is admitted only when its flat-stake edge
    also reproduced in the immediately-previous screening run.

    Issue #1233: condition (d) now keys off `current["truncated"]` --
    reported by get_wallet_trades()/backtest_wallet() -- rather than a
    trade count compared against a constant. These tests exercise the same
    reproducibility behaviour as before, just via the new input shape.
    """

    def _truncated_row(self, flat_dollar_pnl=10.0):
        return _quality_row(flat_dollar_pnl=flat_dollar_pnl, truncated=True)

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
            slippage_bps=200.0, eligible_to_follow=0, truncated=0,
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

    def test_truncated_flag_persisted_from_backtest_result(self):
        # Issue #1233 acceptance criteria: the persisted row must carry
        # the flag through, visible retrospectively -- not just consumed
        # in-process by check_quality().
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0", truncated=True),
        ):
            run(window="month", top=1, slippage_bps=150.0, db=db)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["truncated"] == 1

    def test_untruncated_flag_persisted_as_zero(self):
        leaderboard = _leaderboard(1)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result("0xwallet0", truncated=False),
        ):
            run(window="month", top=1, slippage_bps=150.0, db=db)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["truncated"] == 0

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


class TestRunPerWalletErrorIsolation:
    """Issue #1245: one bad wallet must never abort the whole screening
    run. Two distinct defects: (1) a zero-resolved wallet's None stats
    crashing check_stability(), (2) no per-wallet try/except at all -- an
    ARBITRARY exception on any wallet used to destroy every wallet after
    it in the same run. The second is the fix that closes the class, not
    just this one instance -- see the live 0x2eaa693ca8 crash this issue
    is named for.
    """

    def test_zero_resolved_wallet_0x2eaa693ca8_skipped_without_raising(self, caplog):
        # Regression fixture for the live 2026-09-28 07:30 crash: a wallet
        # with n_resolved=0 has win_rate/mean_roi/median_roi all None
        # (_stats(), copy_trade_backtest.py). --min-trades defaults to 0,
        # so `n_resolved < min_trades` does NOT filter it out -- it must be
        # skipped on its own, before check_stability() ever sees it.
        leaderboard = [{"proxyWallet": "0x2eaa693ca8"}]
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result(
                "0x2eaa693ca8", n_resolved=0, win_rate=None, mean_roi=None,
                median_roi=None,
            ),
        ):
            with caplog.at_level("INFO", logger="src.scripts.copy_wallet_screening"):
                rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0
        db.insert_wallet_screening.assert_not_called()
        assert "0x2eaa693ca8" in caplog.text

    def test_min_trades_explicit_zero_does_not_crash_on_zero_resolved(self):
        # Do not paper over the None case by changing --min-trades' default
        # -- a caller explicitly passing min_trades=0 must still not crash.
        leaderboard = [{"proxyWallet": "0xzero"}]
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            return_value=_backtest_result(
                "0xzero", n_resolved=0, win_rate=None, mean_roi=None, median_roi=None,
            ),
        ):
            rc = run(window="month", top=1, slippage_bps=150.0, min_trades=0, db=db)
        assert rc == 0
        db.insert_wallet_screening.assert_not_called()

    def test_arbitrary_exception_on_one_wallet_does_not_abort_remaining_wallets(self):
        # This is the criterion that fixes the CLASS rather than the
        # instance: a wallet raising ANY exception (not just the None
        # case) must not prevent subsequent wallets from being screened
        # and persisted.
        leaderboard = _leaderboard(3)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []

        def flaky_backtest(addr, *a, **kw):
            if addr == "0xwallet1":
                raise RuntimeError("simulated network/parsing failure")
            return _backtest_result(addr)

        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet", side_effect=flaky_backtest,
        ):
            rc = run(window="month", top=3, slippage_bps=150.0, db=db)
        assert rc == 0
        # wallet0 and wallet2 still screened and persisted despite wallet1
        # raising.
        assert db.insert_wallet_screening.call_count == 2
        persisted_addresses = {
            call.kwargs["address"] for call in db.insert_wallet_screening.call_args_list
        }
        assert persisted_addresses == {"0xwallet0", "0xwallet2"}

    def test_error_logged_at_warning_with_address_and_exception(self, caplog):
        leaderboard = [{"proxyWallet": "0xbroken"}]
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=ValueError("malformed trade payload"),
        ):
            with caplog.at_level("WARNING", logger="src.scripts.copy_wallet_screening"):
                rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0
        assert "0xbroken" in caplog.text
        assert "malformed trade payload" in caplog.text

    def test_run_summary_reports_error_count(self, caplog):
        leaderboard = _leaderboard(2)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []

        def flaky_backtest(addr, *a, **kw):
            if addr == "0xwallet0":
                raise RuntimeError("boom")
            return _backtest_result(addr)

        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet", side_effect=flaky_backtest,
        ):
            with caplog.at_level("INFO", logger="src.scripts.copy_wallet_screening"):
                rc = run(window="month", top=2, slippage_bps=150.0, db=db)
        assert rc == 0
        assert "errors=1" in caplog.text

    def test_keyboard_interrupt_still_propagates(self):
        leaderboard = _leaderboard(2)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=KeyboardInterrupt,
        ):
            try:
                run(window="month", top=2, slippage_bps=150.0, db=db)
                assert False, "KeyboardInterrupt should have propagated"
            except KeyboardInterrupt:
                pass

    def test_clean_run_with_no_errors_is_unaffected(self):
        # Regression: the try/except wrapper must not change behaviour for
        # the common case of a run with zero errors.
        leaderboard = _leaderboard(3)
        db = MagicMock()
        db.get_recent_wallet_screenings.return_value = []
        with patch(
            "src.scripts.copy_wallet_screening.get_leaderboard", return_value=leaderboard,
        ), patch(
            "src.scripts.copy_wallet_screening.backtest_wallet",
            side_effect=lambda addr, *a, **kw: _backtest_result(addr),
        ):
            rc = run(window="month", top=3, slippage_bps=150.0, db=db)
        assert rc == 0
        assert db.insert_wallet_screening.call_count == 3


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
        # issue #1233: truncation is now the fetcher's own reported flag,
        # not a trade count -- a genuinely-truncated wallet (10,500 trades,
        # cut short by the server's own 400) with no reproducibility
        # evidence yet is ineligible.
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_buy_trades=10500, n_sell_excluded=9500, n_resolved=105,
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=10.0, truncated=True,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 0
        db.insert_wallet_screening.assert_called_once()

    def test_stable_high_trade_count_but_not_truncated_is_eligible(self):
        # Issue #1233's own discriminating test at the run()/persistence
        # level: a wallet with the same high trade count (10,500) but whose
        # fetch reached genuine exhaustion (get_wallet_trades() reported
        # truncated=False, e.g. a short final page) must be eligible.
        # Getting this backwards -- flagging exhaustion as truncation --
        # would be exactly as useless as the old always-inert gate, just
        # failing in the opposite direction.
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 100}
        result = _backtest_result(
            "0xwallet0", n_buy_trades=10500, n_sell_excluded=100, n_resolved=105,
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=10.0, truncated=False,
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
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=10.0, truncated=True,
        )
        self._run_stable(db, result, previous)
        kwargs = db.insert_wallet_screening.call_args.kwargs
        assert kwargs["eligible_to_follow"] == 1
        db.insert_wallet_screening.assert_called_once()

    def test_truncated_history_ineligible_0x5268527977_previous_negative(self):
        # Regression fixture from issue #1217 (and issue #1233's live
        # recurrence): 0x5268527977 passed check_stability() (n_resolved
        # steady at ~10,300) but its flat_dollar_pnl swung
        # -1455 -> -1258 -> -671 -> +1129 across runs, while
        # get_wallet_trades() reports this wallet's fetch as genuinely
        # truncated (10,500 trades then a 400). The reproducibility rule
        # must still reject it, and for the right reason (unreproducible,
        # not "no previous run").
        db = MagicMock()
        previous = {"median_roi": 0.10, "n_resolved": 10300, "flat_dollar_pnl": -671.0}
        result = _backtest_result(
            "0xwallet0", n_buy_trades=10500, n_sell_excluded=9500, n_resolved=10350,
            median_roi=0.11, mean_roi=0.1, flat_dollar_pnl=1129.0, truncated=True,
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
    resolve_payout() path. get_wallet_trades and fetch_market_resolutions_batch
    are mocked at the network boundary -- run() always passes
    batch_resolve=True (issue #1227), so a genuinely new market is resolved
    via fetch_market_resolutions_batch, not fetch_market_resolution;
    fetch_market_resolution is mocked too, only to prove it's the untaken
    fallback path except where a test explicitly forces a batch failure.
    """

    def test_two_wallets_sharing_market_fetches_resolution_in_one_batch_call(self):
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
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
            return_value={"0xshared": True},
        ) as mock_batch, patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution",
        ) as mock_single:
            rc = run(window="month", top=2, slippage_bps=150.0, db=db)
        assert rc == 0
        # Both wallets share the market, but only the FIRST wallet's
        # prefetch finds it genuinely new -- the second wallet's prefetch
        # sees it already cached and never calls the batch fetcher again.
        mock_batch.assert_called_once_with(["0xshared"])
        mock_single.assert_not_called()

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
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
        ) as mock_batch, patch(
            "src.scripts.copy_trade_backtest.fetch_market_resolution",
        ) as mock_single:
            rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0
        mock_batch.assert_not_called()
        mock_single.assert_not_called()

    def test_unresolved_market_never_written_to_persistent_cache(self):
        # Regression/poisoning-risk test: a market that hasn't resolved yet
        # must never get a row in market_resolutions -- it would permanently
        # misclassify the market once it does resolve (issue #1221). Still
        # holds when the resolution comes back through the batched path
        # (issue #1227) rather than the single-market one.
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
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
            return_value={"0xabc": None},
        ):
            rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0
        db.cache_market_resolution.assert_not_called()

    def test_batch_failure_falls_back_to_per_market_resolution(self):
        # fetch_market_resolutions_batch already degrades a failed/timed-out
        # chunk to sequential fetch_market_resolution calls internally (see
        # src/data/polymarket.py); this proves the run-level integration
        # doesn't lose the wallet when that happens -- it still screens
        # successfully off whatever fetch_market_resolution returns.
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
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
            return_value={"0xabc": True},  # as if the batch fetcher's own fallback ran
        ):
            rc = run(window="month", top=1, slippage_bps=150.0, db=db)
        assert rc == 0

    def test_hit_miss_counts_logged_once_per_run(self, caplog):
        # See PersistentResolutionCache's docstring ("Batching's effect on
        # these counts", issue #1227): batch_resolve=True's own priming
        # check earns the miss for a genuinely new market; every
        # resolve_payout() check afterwards -- including what used to be
        # the first trade's own miss -- now finds it primed and counts as a
        # hit. 2 wallets x 1 trade sharing one new market: 1 miss (wallet
        # A's priming check) + 3 hits (wallet A's priming-then-primed
        # per-trade check, plus wallet B's priming check finding it already
        # known, plus wallet B's per-trade check).
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
            "src.scripts.copy_trade_backtest.fetch_market_resolutions_batch",
            return_value={"0xshared": True},
        ):
            with caplog.at_level(logging.INFO):
                rc = run(window="month", top=2, slippage_bps=150.0, db=db)
        assert rc == 0
        assert caplog.text.count("resolution cache:") == 1
        assert "3 hits, 1 misses" in caplog.text


class TestLockAcquired:
    """issue #1247: single-instance lock prevents concurrent runs.
    lock_acquired() is crash-safe (file-level lock released on process exit)
    and returns None when another instance already holds the lock.

    On Windows (where fcntl is unavailable), lock_acquired() returns True
    as a no-op fallback, so the subprocess-based concurrency tests are
    skipped. The unit tests here exercise the non-blocking code path.
    """

    def test_first_acquisition_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "test.lock"
            lock = lock_acquired(lock_file)
            assert lock is not None
            # Clean up
            if hasattr(lock, "close"):
                lock.close()

    def test_reacquire_succeeds_after_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "test.lock"
            lock1 = lock_acquired(lock_file)
            assert lock1 is not None
            if hasattr(lock1, "close"):
                lock1.close()
            # After release, re-acquisition succeeds
            lock2 = lock_acquired(lock_file)
            assert lock2 is not None
            # Clean up
            if hasattr(lock2, "close"):
                lock2.close()

    def test_lock_file_contains_pid_when_real_lock(self):
        import os
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "test.lock"
            lock = lock_acquired(lock_file)
            assert lock is not None
            if hasattr(lock, "close"):
                # Real lock (not Windows no-op): verify PID was written
                content = lock_file.read_text()
                assert str(os.getpid()) in content
                lock.close()

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "subdir" / "test.lock"
            lock = lock_acquired(lock_file)
            assert lock is not None
            assert lock_file.parent.exists()
            # Clean up
            if hasattr(lock, "close"):
                lock.close()

    def test_second_acquisition_fails_with_subprocess(self):
        """Subprocess-based concurrency test: verify that a subprocess holding
        the lock prevents the parent from acquiring it (POSIX only; skipped on
        Windows where fcntl is unavailable).
        """
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_file = Path(tmpdir) / "test.lock"
            # Subprocess script that acquires and holds the lock
            subprocess_code = f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
from src.scripts.copy_wallet_screening import lock_acquired
lock = lock_acquired(Path({str(lock_file)!r}))
if lock is None:
    sys.exit(1)  # Failed to acquire (should not happen)
if lock is True:
    # Windows no-op fallback: skip this test
    sys.exit(2)
# Hold the lock and wait for signal
import time
time.sleep(2)
sys.exit(0)
"""
            proc = subprocess.Popen(
                [sys.executable, "-c", subprocess_code],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            import time
            time.sleep(0.5)  # Let subprocess acquire the lock
            # Try to acquire the same lock in parent
            lock = lock_acquired(lock_file)
            proc.terminate()
            proc.wait()
            # On Windows (fcntl unavailable), lock=True, which is not None
            # On POSIX with fcntl, lock should be None (held by subprocess)
            if lock is not True:  # Not Windows no-op
                assert lock is None, "Second acquisition should fail when subprocess holds it"


class TestMainLockHandling:
    """issue #1247: main() acquires the lock before calling run(),
    and exits with code 1 if another instance already holds it.
    """

    def test_main_exits_with_1_when_already_running(self, caplog):
        # Simulate another instance holding the lock by mocking lock_acquired
        # to return None (lock held).
        with patch(
            "src.scripts.copy_wallet_screening.lock_acquired", return_value=None,
        ):
            with caplog.at_level(logging.ERROR):
                rc = main([])
        assert rc == 1
        assert "already running" in caplog.text
        assert "[copy-wallet-screening]" in caplog.text

    def test_main_proceeds_when_lock_acquired(self):
        # Simulate successful lock acquisition (returns a file-like object or True).
        # The actual run() call will be mocked to avoid real API calls.
        with patch(
            "src.scripts.copy_wallet_screening.lock_acquired", return_value=True,
        ), patch(
            "src.scripts.copy_wallet_screening.run", return_value=0,
        ) as mock_run:
            rc = main(["--top", "1"])
        assert rc == 0
        mock_run.assert_called_once()
