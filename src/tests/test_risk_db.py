"""Unit tests for RiskManager with Database persistence.

Tests the seeding of _daily_pnl from the DB and persistence of PnL changes.
"""
import pytest
from datetime import datetime, timezone, timedelta

from src.risk.manager import RiskManager
from src.data.db import Database


def _rm_with_db(db, **kwargs) -> RiskManager:
    """Create a RiskManager with an in-memory database and explicit limits."""
    defaults = dict(
        daily_loss_limit_eur=50.0,
        max_open_positions=15,
        drawdown_stop_pct=0.15,
        min_market_liquidity=50,
        starting_capital=500.0,
        db=db,
    )
    defaults.update(kwargs)
    return RiskManager(**defaults)


def _rm_no_db(**kwargs) -> RiskManager:
    """Create a RiskManager with no database for backward compatibility tests."""
    defaults = dict(
        daily_loss_limit_eur=50.0,
        max_open_positions=15,
        drawdown_stop_pct=0.15,
        min_market_liquidity=50,
        starting_capital=500.0,
        db=None,
    )
    defaults.update(kwargs)
    return RiskManager(**defaults)


class TestPnLSeededFromDB:
    """_daily_pnl is seeded from DB on __post_init__ when db is provided."""

    def test_pnl_seeded_from_db(self):
        """Insert PnL into DB and verify RiskManager seeds from it on init."""
        db = Database(":memory:")
        today = datetime.now(timezone.utc).date().isoformat()

        # Manually insert PnL: +10, -30, -20 = net -40
        db.upsert_daily_risk(today, pnl_delta=10.0, open_positions=0)
        db.upsert_daily_risk(today, pnl_delta=-30.0, open_positions=0)
        db.upsert_daily_risk(today, pnl_delta=-20.0, open_positions=0)

        # Create RiskManager with db — should seed _daily_pnl from DB
        rm = _rm_with_db(db)
        assert rm._daily_pnl == pytest.approx(-40.0)

    def test_pnl_seeded_zero_when_no_db_entry(self):
        """If no DB entry exists for today, _daily_pnl should be 0.0."""
        db = Database(":memory:")
        rm = _rm_with_db(db)
        assert rm._daily_pnl == pytest.approx(0.0)

    def test_daily_limit_enforced_after_restart(self):
        """Seed -€40 into DB, create RiskManager, verify limit triggers."""
        db = Database(":memory:")
        today = datetime.now(timezone.utc).date().isoformat()

        # Seed -€40 into DB
        db.upsert_daily_risk(today, pnl_delta=-40.0, open_positions=0)

        # Create RiskManager with limit of €50 — should load -€40
        rm = _rm_with_db(db, daily_loss_limit_eur=50.0)

        # With -€40 loaded and limit -€50, we have €10 headroom
        # allow_trade() should still allow one more trade if capital is intact
        ok, reason = rm.allow_trade(capital=500.0, liquidity_contracts=100)
        assert ok is True  # -40 > -50, so still allowed

        # Simulate another loss that puts us at -€45
        rm.record_pnl(-5.0)

        # Now -€45 > -€50, still allowed
        ok, reason = rm.allow_trade(capital=455.0, liquidity_contracts=100)
        assert ok is True

        # Simulate another loss that puts us at -€50
        rm.record_pnl(-5.0)

        # Now -€50 <= -€50, should be blocked
        ok, reason = rm.allow_trade(capital=450.0, liquidity_contracts=100)
        assert ok is False
        assert "daily loss" in reason


class TestNoDBMode:
    """RiskManager(db=None) works identically to current behavior — no DB access."""

    def test_no_db_mode_accumulates_pnl(self):
        """record_pnl() accumulates in memory when db=None."""
        rm = _rm_no_db()
        rm.record_pnl(10.0)
        rm.record_pnl(-5.0)
        assert rm._daily_pnl == pytest.approx(5.0)

    def test_no_db_mode_no_exception(self):
        """record_pnl() does not raise when db=None."""
        rm = _rm_no_db()
        rm.record_pnl(10.0)
        rm.record_pnl(-20.0)
        # If we reach here without exception, test passes
        assert rm._daily_pnl == pytest.approx(-10.0)

    def test_no_db_mode_allow_trade_works(self):
        """allow_trade() works correctly when db=None."""
        rm = _rm_no_db()
        rm._daily_pnl = -49.0
        ok, _ = rm.allow_trade(capital=451.0, liquidity_contracts=100)
        assert ok is True


class TestRecordPnLWritesToDB:
    """record_pnl() persists changes to DB when db is set."""

    def test_record_pnl_writes_to_db(self):
        """Call record_pnl(), verify the DB row is updated."""
        db = Database(":memory:")
        rm = _rm_with_db(db)

        rm.record_pnl(-20.0)

        # Verify the DB has the PnL
        today = datetime.now(timezone.utc).date().isoformat()
        db_pnl = db.get_daily_pnl(today)
        assert db_pnl == pytest.approx(-20.0)

    def test_record_pnl_accumulates_in_db(self):
        """Multiple record_pnl() calls accumulate in the DB."""
        db = Database(":memory:")
        rm = _rm_with_db(db)

        rm.record_pnl(10.0)
        rm.record_pnl(-5.0)
        rm.record_pnl(3.0)

        today = datetime.now(timezone.utc).date().isoformat()
        db_pnl = db.get_daily_pnl(today)
        assert db_pnl == pytest.approx(8.0)
        assert rm._daily_pnl == pytest.approx(8.0)


class TestNewDayReset:
    """_reset_if_new_day() clears PnL and resets the DB when the day changes."""

    def test_new_day_reset_with_db(self):
        """Seed PnL for yesterday, create RM on 'today', assert _daily_pnl == 0."""
        db = Database(":memory:")

        # Insert PnL for yesterday
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        db.upsert_daily_risk(yesterday, pnl_delta=-50.0, open_positions=0)

        # Create RiskManager today — __post_init__ reads today's PnL (which is 0)
        rm = _rm_with_db(db)
        assert rm._daily_pnl == pytest.approx(0.0)

    def test_new_day_reset_writes_zero_to_db(self):
        """When _reset_if_new_day() fires, it writes a 0.0 PnL row for the new day."""
        from datetime import date

        db = Database(":memory:")
        rm = _rm_with_db(db)

        # Set trade_day to yesterday
        rm._trade_day = date(2000, 1, 1)
        rm._daily_pnl = -40.0

        # Call _reset_if_new_day() to simulate day rollover
        rm._reset_if_new_day()

        # Verify _daily_pnl was reset to 0
        assert rm._daily_pnl == pytest.approx(0.0)

        # Verify today's DB row was created with 0.0 PnL
        today = datetime.now(timezone.utc).date().isoformat()
        db_pnl = db.get_daily_pnl(today)
        assert db_pnl == pytest.approx(0.0)


class TestOpenPositionsTracking:
    """open_position() and close_position() update the DB when db is set."""

    def test_open_positions_written_to_db(self):
        """record_pnl() includes open_positions in the DB upsert."""
        db = Database(":memory:")
        rm = _rm_with_db(db)

        rm.open_position()
        rm.open_position()
        rm.record_pnl(10.0)

        # The DB should have the open_positions count
        today = datetime.now(timezone.utc).date().isoformat()
        cur = db._conn.execute(
            "SELECT open_positions FROM risk_state WHERE trade_date=?",
            (today,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 2

    def test_new_day_reset_writes_open_positions(self):
        """When _reset_if_new_day() fires, it writes the current open_positions to DB."""
        from datetime import date

        db = Database(":memory:")
        rm = _rm_with_db(db)

        rm.open_position()
        rm.open_position()
        rm.open_position()

        # Set trade_day to yesterday
        rm._trade_day = date(2000, 1, 1)
        rm._daily_pnl = -40.0

        # Call _reset_if_new_day() to simulate day rollover
        rm._reset_if_new_day()

        # Verify today's DB row includes the open_positions
        today = datetime.now(timezone.utc).date().isoformat()
        cur = db._conn.execute(
            "SELECT open_positions FROM risk_state WHERE trade_date=?",
            (today,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 3
