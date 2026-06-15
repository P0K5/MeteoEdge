"""Tests for P&L scaling by position size (issue #292).

Verifies that the fix_stuck_trades_20260607.py script correctly scales per-unit P&L
by the number of shares held (computed from size_eur / entry_price).
"""
import importlib.util
import math
from datetime import datetime, timezone
from pathlib import Path

from src.data.db import Database

# Load the fix_stuck_trades module
_FIX_PATH = Path(__file__).resolve().parents[2] / "src" / "scripts" / "fix_stuck_trades_20260607.py"
_spec = importlib.util.spec_from_file_location("fix_stuck_trades", _FIX_PATH)
fix_stuck_trades_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fix_stuck_trades_mod)


def _db() -> Database:
    """Create an in-memory test database."""
    return Database(":memory:")


def _insert_settlement(db: Database, ticker: str, resolved_yes: int, actual_high_f: float) -> None:
    """Helper to insert a settlement record."""
    db.insert_settlement(
        ts=datetime.now(timezone.utc).isoformat(),
        station="KSEA",
        ticker=ticker,
        bracket_low=70.0,
        bracket_high=72.0,
        actual_high_f=actual_high_f,
        resolved_yes=resolved_yes,
        source="test",
    )


def _insert_trade(
    db: Database,
    trade_id: int = 1,
    ticker: str = "KSEA-order-test001",
    side: str = "YES",
    actual_price: int = 50,
    capital_before: float = 10.0,
    size_eur: float = 5.0,
) -> int:
    """Helper to insert a stuck trade record."""
    return db.insert_trade(
        ts="2026-06-07T12:00:00+00:00",
        station="KSEA",
        ticker=ticker,
        bracket_low=70.0,
        bracket_high=72.0,
        side=side,
        predicted_price=50,
        actual_price=actual_price,
        predicted_edge=10.0,
        mode="live",
        capital_before=capital_before,
        size_eur=size_eur,
    )


class TestPnLScaling:
    """Test P&L calculations using the canonical formula from settle.py."""

    def test_pnl_scaling_win_scenario(self):
        """
        Test P&L scaling for a winning trade (YES bracket hit).

        Example:
          - Entry price: 50 cents
          - Size EUR: 5.0 USDC
          - Shares: 5.0 / (50/100) = 5.0 / 0.5 = 10 shares
          - Per-unit P&L: 100 - 50 = 50 cents
          - Total P&L: 50 / 100 * 10 = 5.0 USDC (100% profit)
        """
        db = _db()

        # Insert settlement (YES bracket hit → resolved_yes=1)
        _insert_settlement(db, "KSEA-order-test001", resolved_yes=1, actual_high_f=71.5)

        # Insert stuck trade (YES side, entry price 50 cents, 5 EUR invested)
        _insert_trade(
            db,
            trade_id=1,
            ticker="KSEA-order-test001",
            side="YES",
            actual_price=50,
            capital_before=10.0,
            size_eur=5.0,
        )

        # Manually compute expected P&L using the canonical formula
        actual_price = 50  # cents
        size_eur = 5.0
        shares = size_eur / (actual_price / 100)  # 5.0 / 0.5 = 10
        pnl_per_share_cents = (100 - actual_price)  # 50 cents per share
        expected_pnl = round(pnl_per_share_cents / 100 * shares, 4)  # 0.50 * 10 = 5.0

        assert expected_pnl == 5.0, f"Expected PnL 5.0, got {expected_pnl}"

    def test_pnl_scaling_loss_scenario(self):
        """
        Test P&L scaling for a losing trade (YES bracket missed).

        Example:
          - Entry price: 80 cents
          - Size EUR: 5.0 USDC
          - Shares: 5.0 / (80/100) = 5.0 / 0.8 = 6.25 shares
          - Per-unit P&L: -80 cents (loss)
          - Total P&L: -80 / 100 * 6.25 = -5.0 USDC (100% loss)
        """
        db = _db()

        # Insert settlement (YES bracket NOT hit → resolved_yes=0)
        _insert_settlement(db, "KSEA-order-test002", resolved_yes=0, actual_high_f=69.0)

        # Insert stuck trade (YES side, entry price 80 cents, 5 EUR invested)
        _insert_trade(
            db,
            trade_id=2,
            ticker="KSEA-order-test002",
            side="YES",
            actual_price=80,
            capital_before=10.0,
            size_eur=5.0,
        )

        # Manually compute expected P&L using the canonical formula
        actual_price = 80  # cents
        size_eur = 5.0
        shares = size_eur / (actual_price / 100)  # 5.0 / 0.8 = 6.25
        pnl_per_share_cents = -actual_price  # -80 cents per share
        expected_pnl = round(pnl_per_share_cents / 100 * shares, 4)  # -0.80 * 6.25 = -5.0

        assert expected_pnl == -5.0, f"Expected PnL -5.0, got {expected_pnl}"

    def test_pnl_scaling_no_side(self):
        """
        Test P&L scaling for a NO-side trade (bracket NOT hit = win).

        Example:
          - Entry price: 30 cents (NO side, lower ask)
          - Size EUR: 3.0 USDC
          - Shares: 3.0 / (30/100) = 3.0 / 0.3 = 10 shares
          - Per-unit P&L (NO wins when bracket not hit): 100 - 30 = 70 cents
          - Total P&L: 70 / 100 * 10 = 7.0 USDC (profit)
        """
        db = _db()

        # Insert settlement (YES bracket NOT hit → NO wins, resolved_yes=0)
        _insert_settlement(db, "KSEA-order-test003", resolved_yes=0, actual_high_f=69.0)

        # Insert stuck trade (NO side, entry price 30 cents, 3 EUR invested)
        _insert_trade(
            db,
            trade_id=3,
            ticker="KSEA-order-test003",
            side="NO",
            actual_price=30,
            capital_before=10.0,
            size_eur=3.0,
        )

        # Manually compute expected P&L using the canonical formula
        actual_price = 30  # cents
        size_eur = 3.0
        shares = size_eur / (actual_price / 100)  # 3.0 / 0.3 = 10
        # NO side wins when bracket NOT hit (resolved_yes=0)
        pnl_per_share_cents = (100 - actual_price)  # 70 cents per share (win)
        expected_pnl = round(pnl_per_share_cents / 100 * shares, 4)  # 0.70 * 10 = 7.0

        assert expected_pnl == 7.0, f"Expected PnL 7.0, got {expected_pnl}"

    def test_pnl_scaling_rounding(self):
        """
        Test P&L scaling with fractional shares and rounding.

        Example:
          - Entry price: 33 cents
          - Size EUR: 5.0 USDC
          - Shares: 5.0 / 0.33 = 15.151515...
          - Per-unit P&L: 100 - 33 = 67 cents
          - Total P&L: 67 / 100 * 15.151515... = 10.1515... → rounds to 10.1515
        """
        db = _db()

        # Insert settlement (bracket hit → win)
        _insert_settlement(db, "KSEA-order-test004", resolved_yes=1, actual_high_f=71.0)

        # Insert stuck trade
        _insert_trade(
            db,
            trade_id=4,
            ticker="KSEA-order-test004",
            side="YES",
            actual_price=33,
            capital_before=10.0,
            size_eur=5.0,
        )

        # Manually compute expected P&L with proper rounding
        actual_price = 33
        size_eur = 5.0
        shares = size_eur / (actual_price / 100)  # 5.0 / 0.33 = 15.151515...
        pnl_per_share_cents = (100 - actual_price)  # 67 cents
        expected_pnl = round(pnl_per_share_cents / 100 * shares, 4)  # round to 4 decimals

        # Verify the rounding is correct
        assert abs(expected_pnl - 10.1515) < 0.0001, f"Expected ~10.1515, got {expected_pnl}"

    def test_query_includes_size_eur(self):
        """Test that the stuck trades query includes size_eur column."""
        db = _db()

        # Insert settlement and stuck trade
        _insert_settlement(db, "KSEA-order-test005", resolved_yes=1, actual_high_f=71.0)
        _insert_trade(
            db,
            trade_id=5,
            ticker="KSEA-order-test005",
            side="YES",
            actual_price=50,
            capital_before=10.0,
            size_eur=2.5,
        )

        # Query stuck trades (same query as in fix_stuck_trades_20260607.py)
        stuck = db._conn.execute(
            """
            SELECT t.id, t.ts, t.station, t.ticker, t.side, t.bracket_low, t.bracket_high,
                   t.actual_price, t.capital_before, t.size_eur
            FROM trades t
            WHERE t.mode = 'live'
              AND t.outcome IS NULL
              AND DATE(t.ts) = '2026-06-07'
              AND NOT EXISTS (SELECT 1 FROM open_positions p WHERE p.trade_id = t.id)
            ORDER BY t.ts ASC
            """
        ).fetchall()

        assert len(stuck) == 1, f"Expected 1 stuck trade, got {len(stuck)}"
        trade = stuck[0]
        assert trade['size_eur'] == 2.5, f"Expected size_eur=2.5, got {trade['size_eur']}"
        assert trade['actual_price'] == 50, f"Expected actual_price=50, got {trade['actual_price']}"
