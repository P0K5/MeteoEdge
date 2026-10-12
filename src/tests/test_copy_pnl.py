"""Unit tests for src/data/copy_pnl.py (issue #1131)."""
import pytest

from src.data.copy_pnl import (
    compute_realized_pnl_usd,
    copy_position_won,
    is_guaranteed_loss_pair,
    opposite_side_pair_locked_loss_usd,
    summarize_opposite_side_overlap,
)


class TestCopyPositionWon:
    def test_outcome_index_0_wins_when_yes_won(self):
        assert copy_position_won(outcome_index=0, yes_won=True) is True
        assert copy_position_won(outcome_index=0, yes_won=False) is False

    def test_outcome_index_1_wins_when_yes_did_not_win(self):
        assert copy_position_won(outcome_index=1, yes_won=False) is True
        assert copy_position_won(outcome_index=1, yes_won=True) is False


class TestComputeRealizedPnlUsd:
    def test_known_numeric_example_win(self):
        """entry_price=0.40, stake_usd=10, win -> pnl == 15.0."""
        pnl = compute_realized_pnl_usd(
            entry_price=0.40, stake_usd=10.0, outcome_index=0, yes_won=True,
        )
        assert pnl == pytest.approx(15.0)

    def test_known_numeric_example_loss(self):
        """entry_price=0.40, stake_usd=10, loss -> pnl == -10.0."""
        pnl = compute_realized_pnl_usd(
            entry_price=0.40, stake_usd=10.0, outcome_index=0, yes_won=False,
        )
        assert pnl == pytest.approx(-10.0)

    def test_winning_position_outcome_index_0(self):
        pnl = compute_realized_pnl_usd(
            entry_price=0.25, stake_usd=20.0, outcome_index=0, yes_won=True,
        )
        # shares = 20 / 0.25 = 80; payout = 80; pnl = 80 - 20 = 60
        assert pnl == pytest.approx(60.0)

    def test_winning_position_outcome_index_1(self):
        pnl = compute_realized_pnl_usd(
            entry_price=0.25, stake_usd=20.0, outcome_index=1, yes_won=False,
        )
        assert pnl == pytest.approx(60.0)

    def test_losing_position_outcome_index_0(self):
        pnl = compute_realized_pnl_usd(
            entry_price=0.25, stake_usd=20.0, outcome_index=0, yes_won=False,
        )
        assert pnl == pytest.approx(-20.0)

    def test_losing_position_outcome_index_1(self):
        pnl = compute_realized_pnl_usd(
            entry_price=0.25, stake_usd=20.0, outcome_index=1, yes_won=True,
        )
        assert pnl == pytest.approx(-20.0)

    def test_winning_position_at_entry_price_zero_raises(self):
        """A winning position can't happen at entry_price == 0 (no real
        fill occurs at price 0) -- computing its P&L would divide by zero,
        so this must raise a clear ValueError instead of ZeroDivisionError."""
        with pytest.raises(ValueError):
            compute_realized_pnl_usd(
                entry_price=0.0, stake_usd=10.0, outcome_index=0, yes_won=True,
            )

    def test_losing_position_at_entry_price_zero_is_fine(self):
        """The loss branch never divides by entry_price, so it stays valid
        at the same boundary that raises on a win."""
        pnl = compute_realized_pnl_usd(
            entry_price=0.0, stake_usd=10.0, outcome_index=0, yes_won=False,
        )
        assert pnl == pytest.approx(-10.0)

    def test_losing_position_pnl_never_exceeds_stake(self):
        """A loss always costs exactly the stake -- never more, never less,
        regardless of entry_price."""
        for price in (0.01, 0.5, 0.99):
            pnl = compute_realized_pnl_usd(
                entry_price=price, stake_usd=42.0, outcome_index=0, yes_won=False,
            )
            assert pnl == pytest.approx(-42.0)


class TestIsGuaranteedLossPair:
    """Issue #1316: a combined entry cost >= $1.00 across two opposite-
    outcome legs of the same market is a mathematically guaranteed loss."""

    def test_exactly_one_dollar_is_guaranteed_loss(self):
        assert is_guaranteed_loss_pair(0.60, 0.40) is True

    def test_just_under_one_dollar_is_not(self):
        assert is_guaranteed_loss_pair(0.60, 0.39) is False

    def test_above_one_dollar_is_guaranteed_loss(self):
        assert is_guaranteed_loss_pair(0.9135, 0.8221) is True

    def test_well_below_one_dollar_is_not(self):
        assert is_guaranteed_loss_pair(0.20, 0.20) is False


class TestOppositeSidePairLockedLossUsd:
    def test_zero_when_sub_parity(self):
        assert opposite_side_pair_locked_loss_usd(0.20, 5.0, 0.20, 5.0) == 0.0

    def test_equal_stakes_matches_issues_own_formula(self):
        """(price_0 + price_1 - 1.00) x stake, per issue #1316's own
        $5/leg measurement."""
        loss = opposite_side_pair_locked_loss_usd(0.60, 5.0, 0.50, 5.0)
        assert loss == pytest.approx((0.60 + 0.50 - 1.0) * 5.0)

    def test_boundary_exactly_one_dollar_is_zero_loss(self):
        """Exactly at parity: breaks even, locks in $0.00 -- not a loss,
        but still correctly distinguished from a sub-parity pair by
        is_guaranteed_loss_pair (the boundary is still refused at entry;
        this is just the resulting dollar amount at that boundary)."""
        loss = opposite_side_pair_locked_loss_usd(0.60, 5.0, 0.40, 5.0)
        assert loss == pytest.approx(0.0)

    def test_unequal_stakes_uses_average(self):
        loss = opposite_side_pair_locked_loss_usd(0.70, 10.0, 0.60, 5.0)
        assert loss == pytest.approx((0.70 + 0.60 - 1.0) * 7.5)


class TestSummarizeOppositeSideOverlap:
    def _pair(self, **overrides):
        row = dict(
            address="0xabc", market="0xmarket1",
            a_entry_price=0.60, b_entry_price=0.50,
            a_stake_usd=5.0, b_stake_usd=5.0,
            a_status="open", b_status="open",
        )
        row.update(overrides)
        return row

    def test_sub_parity_pair_contributes_nothing(self):
        summary = summarize_opposite_side_overlap([self._pair(a_entry_price=0.20, b_entry_price=0.20)])
        assert summary == {}

    def test_above_parity_open_pair_counted_in_both_totals(self):
        summary = summarize_opposite_side_overlap([self._pair()])
        row = summary["0xabc"]
        assert row["n_above_parity_pairs"] == 1
        assert row["n_above_parity_pairs_still_open"] == 1
        assert row["locked_loss_usd"] == pytest.approx(0.5)
        assert row["locked_loss_usd_still_open"] == pytest.approx(0.5)

    def test_above_parity_fully_settled_pair_excluded_from_still_open(self):
        """Issue #1316: once both legs of a pair have settled, the loss is
        already reflected in realized P&L -- nothing further to disclose,
        so it drops out of the *_still_open totals but stays in the
        historical totals (for one-off reporting)."""
        summary = summarize_opposite_side_overlap(
            [self._pair(a_status="settled", b_status="settled")]
        )
        row = summary["0xabc"]
        assert row["n_above_parity_pairs"] == 1
        assert row["n_above_parity_pairs_still_open"] == 0
        assert row["locked_loss_usd"] == pytest.approx(0.5)
        assert row["locked_loss_usd_still_open"] == pytest.approx(0.0)

    def test_one_leg_still_open_counts_as_still_open(self):
        summary = summarize_opposite_side_overlap(
            [self._pair(a_status="settled", b_status="open")]
        )
        assert summary["0xabc"]["n_above_parity_pairs_still_open"] == 1

    def test_multiple_pairs_aggregate_per_wallet(self):
        pairs = [
            self._pair(),
            self._pair(a_entry_price=0.9135, b_entry_price=0.8221),
        ]
        row = summarize_opposite_side_overlap(pairs)["0xabc"]
        assert row["n_above_parity_pairs"] == 2
        assert row["locked_loss_usd"] == pytest.approx(
            0.5 + (0.9135 + 0.8221 - 1.0) * 5.0
        )

    def test_wallet_with_no_above_parity_pairs_is_absent(self):
        summary = summarize_opposite_side_overlap(
            [self._pair(address="0xclean", a_entry_price=0.2, b_entry_price=0.2)]
        )
        assert "0xclean" not in summary
        assert summary == {}
