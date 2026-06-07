"""Tests for paper_trader and dashboard DB integration (Issue E: trades migration)."""
import pytest

from src.data.db import Database
from src.paper_trader import PaperTrader


def _db() -> Database:
    return Database(":memory:")


# ---------------------------------------------------------------------------
# PaperTrader DB integration
# ---------------------------------------------------------------------------

class TestPaperTraderDbWrite:
    """execute_trade() must write records to DB."""

    def test_execute_trade_inserts_to_db(self):
        db = _db()
        pt = PaperTrader(db=db, starting_capital_eur=100.0, position_size_eur=5.0)
        trade = pt.execute_trade(
            station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            bracket_low=32.0,
            bracket_high=36.0,
            side="NO",
            predicted_price=70,
            predicted_edge=0.08,
            actual_daily_high=40.0,  # outside bracket -> NO wins
            minutes_to_settlement=60.0,
        )
        assert trade is not None
        rows = db.get_trades(limit=None, mode="paper")
        assert len(rows) == 1
        assert rows[0]["station"] == "KORD"
        assert rows[0]["mode"] == "paper"
        assert rows[0]["side"] == "NO"

    def test_execute_trade_no_db_still_works(self):
        pt = PaperTrader(db=None, starting_capital_eur=100.0, position_size_eur=5.0)
        trade = pt.execute_trade(
            station="KJFK",
            ticker="KJFK-2024-01-15-HIGH-40-44",
            bracket_low=40.0,
            bracket_high=44.0,
            side="YES",
            predicted_price=60,
            predicted_edge=0.10,
            actual_daily_high=42.0,
            minutes_to_settlement=60.0,
        )
        assert trade is not None

    def test_execute_trade_capital_tracked_in_db(self):
        db = _db()
        pt = PaperTrader(db=db, starting_capital_eur=100.0, position_size_eur=5.0)
        pt.execute_trade(
            station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            bracket_low=32.0,
            bracket_high=36.0,
            side="NO",
            predicted_price=70,
            predicted_edge=0.08,
            actual_daily_high=40.0,
            minutes_to_settlement=60.0,
        )
        rows = db.get_trades(limit=1, mode="paper")
        assert rows[0]["capital_before"] == pytest.approx(100.0)
        assert rows[0]["capital_after"] is not None

    def test_insufficient_capital_returns_none(self):
        db = _db()
        pt = PaperTrader(db=db, starting_capital_eur=1.0, position_size_eur=5.0)
        result = pt.execute_trade(
            station="KORD",
            ticker="KORD-test",
            bracket_low=32.0,
            bracket_high=36.0,
            side="NO",
            predicted_price=70,
            predicted_edge=0.08,
            actual_daily_high=40.0,
            minutes_to_settlement=60.0,
        )
        assert result is None
        assert db.get_trades(limit=None, mode="paper") == []


class TestPaperTraderCapitalRecovery:
    """PaperTrader.__init__ must restore capital from DB on restart."""

    def test_capital_restored_from_db(self):
        db = _db()
        pt1 = PaperTrader(db=db, starting_capital_eur=100.0, position_size_eur=5.0)
        pt1.execute_trade(
            station="KORD",
            ticker="KORD-2024-01-15-HIGH-32-36",
            bracket_low=32.0,
            bracket_high=36.0,
            side="NO",
            predicted_price=70,
            predicted_edge=0.08,
            actual_daily_high=40.0,
            minutes_to_settlement=60.0,
        )
        capital_after_first_trade = pt1.capital

        # Simulate restart — new PaperTrader, same DB
        pt2 = PaperTrader(db=db, starting_capital_eur=100.0, position_size_eur=5.0)
        assert pt2.capital == pytest.approx(capital_after_first_trade)

    def test_no_db_starts_with_starting_capital(self):
        pt = PaperTrader(db=None, starting_capital_eur=500.0)
        assert pt.capital == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# Dashboard DB integration
# ---------------------------------------------------------------------------

class TestDashboardDbIntegration:
    """_load_trades() must prefer DB when _db is set."""

    def test_load_trades_from_db(self):
        import src.monitoring.dashboard as dash
        db = _db()
        db.insert_trade(
            ts="2024-01-15T12:00:00Z", station="KORD",
            ticker="KORD-test", bracket_low=32.0, bracket_high=36.0,
            side="NO", predicted_price=70, actual_price=71,
            predicted_edge=0.08, mode="live", capital_before=1000.0,
        )
        original_db = dash._db
        try:
            dash.set_db(db)
            trades = dash._load_trades()
            assert len(trades) == 1
            assert trades[0]["station"] == "KORD"
        finally:
            dash.set_db(original_db)

    def test_load_trades_falls_back_without_db(self):
        import src.monitoring.dashboard as dash
        original_db = dash._db
        try:
            dash.set_db(None)
            # Should not raise — falls back to JSONL (which may be empty)
            trades = dash._load_trades()
            assert isinstance(trades, list)
        finally:
            dash.set_db(original_db)

    def test_latest_capital_from_db(self):
        import src.monitoring.dashboard as dash
        db = _db()
        db.insert_trade(
            ts="2024-01-15T12:00:00Z", station="KORD",
            ticker="KORD-test", bracket_low=32.0, bracket_high=36.0,
            side="NO", predicted_price=70, actual_price=71,
            predicted_edge=0.08, mode="live",
            capital_before=1000.0, capital_after=1007.50,
        )
        original_db = dash._db
        try:
            dash.set_db(db)
            capital = dash._latest_capital([])
            assert capital == pytest.approx(1007.50)
        finally:
            dash.set_db(original_db)
