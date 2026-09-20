"""Unit tests for src/data/copy_pnl.py (issue #1131)."""
import pytest

from src.data.copy_pnl import compute_realized_pnl_usd, copy_position_won


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
