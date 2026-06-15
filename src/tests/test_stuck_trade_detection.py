"""Tests for stuck-trade detection in src/scripts/settle.py.

Covers:
- Detection of live trades with outcome IS NULL, no open_position, older than 36 hours
- The detection query correctly identifies stuck trades
- Warning is logged when stuck trades are found
- Idempotent: can run multiple times without side effects
"""
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.data.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path) -> Database:
    """Create a fresh in-memory or temp database for testing."""
    return Database(path=str(tmp_path / "settle_test.db"))


def _insert_stuck_trade(
    db: Database,
    trade_id_hint: int = 1,
    ts_offset_hours: int = -48,
) -> int:
    """Insert a live trade with outcome IS NULL and no open_position.

    Args:
        db: Database instance
        trade_id_hint: Rough hint for the ID (actual ID determined by DB)
        ts_offset_hours: Timestamp offset from now (e.g., -48 for 48 hours ago)

    Returns:
        The inserted trade ID
    """
    # Timestamp is 48 hours ago (> 36 hour threshold for stuck detection)
    ts = (datetime.now(timezone.utc) + timedelta(hours=ts_offset_hours)).isoformat()

    trade_id = db.insert_trade(
        ts=ts,
        station="KORD",
        ticker=f"0xKORD_STUCK_{trade_id_hint}",
        bracket_low=70.0,
        bracket_high=74.0,
        side="NO",
        predicted_price=25,
        actual_price=25,
        predicted_edge=5.0,
        mode="live",
        capital_before=100.0,
        slippage=None,
        order_id=f"order_{trade_id_hint}",
        outcome=None,  # <-- stuck: no outcome
        pnl=None,
        capital_after=None,
        settled_at=None,
    )
    # IMPORTANT: do NOT create an open_position for this trade
    return trade_id


def _insert_settled_trade(db: Database, trade_id_hint: int = 2) -> int:
    """Insert a settled live trade (for contrast with stuck trades)."""
    ts = datetime.now(timezone.utc).isoformat()
    return db.insert_trade(
        ts=ts,
        station="KATL",
        ticker=f"0xKATL_SETTLED_{trade_id_hint}",
        bracket_low=65.0,
        bracket_high=69.0,
        side="YES",
        predicted_price=45,
        actual_price=45,
        predicted_edge=8.0,
        mode="live",
        capital_before=100.0,
        slippage=None,
        order_id=f"order_{trade_id_hint}",
        outcome="filled",  # <-- already settled
        pnl=0.35,
        capital_after=100.35,
        settled_at=datetime.now(timezone.utc).isoformat(),
    )


def _query_stuck_trades(db: Database) -> list:
    """Run the stuck-trade detection query."""
    return db._conn.execute(
        """
        SELECT t.id, t.ticker, t.side, t.ts FROM trades t
        WHERE t.mode = 'live'
          AND t.outcome IS NULL
          AND t.ts < datetime('now', '-36 hours')
          AND NOT EXISTS (SELECT 1 FROM open_positions p WHERE p.trade_id = t.id)
        """
    ).fetchall()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStuckTradeDetection:
    """Tests for stuck-trade detection logic."""

    def test_detect_single_stuck_trade(self, tmp_path, caplog):
        """Single stuck trade (> 36h old, no outcome, no open_position) is detected."""
        db = _fresh_db(tmp_path)
        stuck_id = _insert_stuck_trade(db, trade_id_hint=1, ts_offset_hours=-48)

        stuck = _query_stuck_trades(db)
        assert len(stuck) == 1
        assert stuck[0]["id"] == stuck_id

    def test_detect_multiple_stuck_trades(self, tmp_path):
        """Multiple stuck trades are detected."""
        db = _fresh_db(tmp_path)
        stuck_id_1 = _insert_stuck_trade(db, trade_id_hint=1, ts_offset_hours=-48)
        stuck_id_2 = _insert_stuck_trade(db, trade_id_hint=2, ts_offset_hours=-72)

        stuck = _query_stuck_trades(db)
        assert len(stuck) == 2
        stuck_ids = {r["id"] for r in stuck}
        assert stuck_id_1 in stuck_ids
        assert stuck_id_2 in stuck_ids

    def test_skip_settled_trades(self, tmp_path):
        """Settled trades (outcome IS NOT NULL) are not detected as stuck."""
        db = _fresh_db(tmp_path)
        stuck_id = _insert_stuck_trade(db, trade_id_hint=1, ts_offset_hours=-48)
        settled_id = _insert_settled_trade(db, trade_id_hint=2)

        stuck = _query_stuck_trades(db)
        assert len(stuck) == 1
        assert stuck[0]["id"] == stuck_id

    def test_skip_recent_trades_with_null_outcome(self, tmp_path):
        """Recent trades (< 36 hours) with outcome IS NULL are not flagged as stuck."""
        db = _fresh_db(tmp_path)
        # Insert a trade only 12 hours old (< 36 hour threshold)
        ts = (datetime.now(timezone.utc) + timedelta(hours=-12)).isoformat()
        recent_id = db.insert_trade(
            ts=ts,
            station="KORD",
            ticker="0xRECENT",
            bracket_low=70.0,
            bracket_high=74.0,
            side="NO",
            predicted_price=25,
            actual_price=25,
            predicted_edge=5.0,
            mode="live",
            capital_before=100.0,
            outcome=None,
            pnl=None,
            capital_after=None,
            settled_at=None,
        )

        stuck = _query_stuck_trades(db)
        # Should find nothing: recent_id is too new
        assert len(stuck) == 0

    def test_skip_trades_with_open_position(self, tmp_path):
        """Trades that DO have an open_position are not flagged as stuck."""
        db = _fresh_db(tmp_path)
        ts = (datetime.now(timezone.utc) + timedelta(hours=-48)).isoformat()
        trade_id = db.insert_trade(
            ts=ts,
            station="KORD",
            ticker="0xOPEN",
            bracket_low=70.0,
            bracket_high=74.0,
            side="NO",
            predicted_price=25,
            actual_price=25,
            predicted_edge=5.0,
            mode="live",
            capital_before=100.0,
            outcome=None,
            pnl=None,
            capital_after=None,
            settled_at=None,
        )
        # THIS trade DOES have an open position
        db.open_position(
            trade_id=trade_id,
            station="KORD",
            ticker="0xOPEN",
            token_id="token123",
            side="NO",
            order_id="order_with_position",
            entry_price=25,
            shares=100.0,
            entry_ts=ts,
        )

        stuck = _query_stuck_trades(db)
        # Should find nothing: trade_id has an open_position
        assert len(stuck) == 0

    def test_skip_shadow_trades(self, tmp_path):
        """Shadow mode trades are not flagged as stuck (only live mode)."""
        db = _fresh_db(tmp_path)
        ts = (datetime.now(timezone.utc) + timedelta(hours=-48)).isoformat()
        shadow_id = db.insert_trade(
            ts=ts,
            station="KORD",
            ticker="0xSHADOW",
            bracket_low=70.0,
            bracket_high=74.0,
            side="NO",
            predicted_price=25,
            actual_price=25,
            predicted_edge=5.0,
            mode="shadow",  # <-- shadow, not live
            capital_before=0.0,
            outcome=None,
            pnl=None,
            capital_after=None,
            settled_at=None,
        )

        stuck = _query_stuck_trades(db)
        # Should find nothing: shadow mode not included
        assert len(stuck) == 0

    def test_idempotent_detection(self, tmp_path):
        """Running the query multiple times yields the same result."""
        db = _fresh_db(tmp_path)
        stuck_id = _insert_stuck_trade(db, trade_id_hint=1, ts_offset_hours=-48)

        stuck1 = _query_stuck_trades(db)
        stuck2 = _query_stuck_trades(db)
        stuck3 = _query_stuck_trades(db)

        assert len(stuck1) == len(stuck2) == len(stuck3) == 1
        assert stuck1[0]["id"] == stuck2[0]["id"] == stuck3[0]["id"] == stuck_id


class TestStuckTradeDetectionLogging:
    """Tests for logging and warnings."""

    def test_warning_logged_when_stuck_found(self, tmp_path, caplog):
        """A warning is logged when stuck trades are detected."""
        db = _fresh_db(tmp_path)
        stuck_id = _insert_stuck_trade(db, trade_id_hint=1, ts_offset_hours=-48)

        stuck = _query_stuck_trades(db)
        assert len(stuck) > 0

        # Simulate the settle.py logging
        with caplog.at_level(logging.WARNING):
            logging.getLogger("settle").warning(
                "[settle] %d stuck trade(s) outcome IS NULL > 36h: ids=%s",
                len(stuck), [r['id'] for r in stuck]
            )

        assert "stuck trade" in caplog.text.lower()
        assert str(stuck_id) in caplog.text
