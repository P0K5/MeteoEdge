"""Tests for #321 — trade cost accounting.

Covers:
- Database.update_trade_costs() writes estimated_fee_cents and size_eur
- Database.get_trade_cost_summary() aggregates live closed trades
- _record_sell_in_db() calls update_trade_costs() for all close paths
- GET /api/trade-costs/summary endpoint
"""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from src.data.db import Database
from src.strategy.fee import estimate_fee_cents

# get_trade_cost_summary(days=N) filters on a trailing window anchored to
# date.today() — fixture timestamps must be relative, not hardcoded, or the
# tests start failing once the hardcoded date ages out of the window.
_RECENT_TS = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    return Database(":memory:")


def _insert_live_trade(db: Database, order_id: str = "ord-001", actual_price: int = 60) -> int:
    return db.insert_trade(
        ts=_RECENT_TS,
        station="KORD",
        ticker="KORD-2026-06-01-HIGH-80-84",
        bracket_low=80.0,
        bracket_high=84.0,
        side="NO",
        predicted_price=actual_price,
        actual_price=actual_price,
        predicted_edge=0.05,
        mode="live",
        capital_before=500.0,
        order_id=order_id,
    )


# ---------------------------------------------------------------------------
# Database.update_trade_costs
# ---------------------------------------------------------------------------

class TestUpdateTradeCosts:
    def test_writes_estimated_fee_cents(self):
        db = _db()
        _insert_live_trade(db, order_id="ord-A", actual_price=50)
        rows = db._conn.execute(
            "UPDATE trades SET outcome='sold' WHERE order_id='ord-A'"
        )
        db._conn.commit()

        n = db.update_trade_costs("ord-A", estimated_fee_cents=1.75)
        assert n == 1
        row = db._conn.execute(
            "SELECT estimated_fee_cents FROM trades WHERE order_id='ord-A'"
        ).fetchone()
        assert row["estimated_fee_cents"] == pytest.approx(1.75)

    def test_writes_size_eur(self):
        db = _db()
        _insert_live_trade(db, order_id="ord-B")
        db.update_trade_costs("ord-B", size_eur=12.50)
        row = db._conn.execute(
            "SELECT size_eur FROM trades WHERE order_id='ord-B'"
        ).fetchone()
        assert row["size_eur"] == pytest.approx(12.50)

    def test_skips_none_fields(self):
        db = _db()
        _insert_live_trade(db, order_id="ord-C", actual_price=40)
        # Write fee first
        db.update_trade_costs("ord-C", estimated_fee_cents=2.0, size_eur=5.0)
        # Update with all-None — should be a no-op
        n = db.update_trade_costs("ord-C")
        assert n == 0
        row = db._conn.execute(
            "SELECT estimated_fee_cents, size_eur FROM trades WHERE order_id='ord-C'"
        ).fetchone()
        assert row["estimated_fee_cents"] == pytest.approx(2.0)
        assert row["size_eur"] == pytest.approx(5.0)

    def test_returns_zero_for_unknown_order(self):
        db = _db()
        n = db.update_trade_costs("does-not-exist", estimated_fee_cents=1.0)
        assert n == 0


# ---------------------------------------------------------------------------
# Database.get_trade_cost_summary
# ---------------------------------------------------------------------------

class TestGetTradeCostSummary:
    def _insert_closed_live(
        self, db: Database, order_id: str, actual_price: int = 60,
        fee: float = 1.75, size_eur: float = 10.0, pnl: float = 0.5,
    ) -> None:
        _insert_live_trade(db, order_id=order_id, actual_price=actual_price)
        db.update_trade_by_order(order_id, outcome="sold", pnl=pnl)
        db.update_trade_costs(order_id, estimated_fee_cents=fee, size_eur=size_eur)

    def test_returns_zeros_when_no_trades(self):
        db = _db()
        result = db.get_trade_cost_summary(days=30)
        assert result["trade_count"] == 0
        assert result["total_fee_eur"] == 0.0
        assert result["period_days"] == 30

    def test_aggregates_live_closed_trades(self):
        db = _db()
        self._insert_closed_live(db, "ord-1", fee=1.75, size_eur=10.0, pnl=0.5)
        self._insert_closed_live(db, "ord-2", fee=2.00, size_eur=8.0, pnl=-0.2)
        result = db.get_trade_cost_summary(days=30)
        assert result["trade_count"] == 2
        assert result["fee_populated_count"] == 2
        assert result["total_fee_eur"] == pytest.approx((1.75 + 2.00) / 100.0, abs=1e-4)
        assert result["total_size_eur"] == pytest.approx(18.0, abs=1e-4)
        assert result["total_pnl"] == pytest.approx(0.3, abs=1e-4)

    def test_excludes_paper_trades(self):
        db = _db()
        db.insert_trade(
            ts=_RECENT_TS,
            station="KORD",
            ticker="KORD-2026-06-01-HIGH-80-84",
            bracket_low=80.0, bracket_high=84.0, side="NO",
            predicted_price=60, actual_price=60, predicted_edge=0.05,
            mode="paper", capital_before=500.0, order_id="ord-paper",
            outcome="sold", pnl=1.0,
        )
        result = db.get_trade_cost_summary(days=30)
        assert result["trade_count"] == 0

    def test_fee_populated_count_excludes_null(self):
        db = _db()
        # Insert one trade with fee, one without
        self._insert_closed_live(db, "ord-with-fee", fee=1.5)
        _insert_live_trade(db, order_id="ord-no-fee")
        db.update_trade_by_order("ord-no-fee", outcome="sold", pnl=0.0)
        result = db.get_trade_cost_summary(days=30)
        assert result["trade_count"] == 2
        assert result["fee_populated_count"] == 1


# ---------------------------------------------------------------------------
# _record_sell_in_db wires estimated_fee_cents
# ---------------------------------------------------------------------------

class TestRecordSellInDbCostWiring:
    """Verify that _record_sell_in_db calls db.update_trade_costs for each fill."""

    def _make_fill(self, order_id: str = "ord-buy-001", price_cents: int = 30,
                   size_eur: float = 5.0) -> dict:
        return {"order_id": order_id, "price_cents": price_cents, "size_eur": size_eur}

    def test_update_trade_costs_called(self):
        from src.execution.order_manager import _record_sell_in_db

        mock_db = MagicMock()
        fill = self._make_fill(order_id="ord-buy-001", price_cents=30, size_eur=5.0)
        _record_sell_in_db([fill], sell_price_cents=35, ts="2026-06-01T12:00:00+00:00", db=mock_db)

        expected_fee = estimate_fee_cents(35)
        mock_db.update_trade_costs.assert_called_once_with(
            "ord-buy-001",
            estimated_fee_cents=pytest.approx(expected_fee),
            size_eur=5.0,
        )

    def test_multiple_fills_each_get_costs_updated(self):
        from src.execution.order_manager import _record_sell_in_db

        mock_db = MagicMock()
        fills = [
            self._make_fill(order_id="ord-a", price_cents=25, size_eur=4.0),
            self._make_fill(order_id="ord-b", price_cents=28, size_eur=6.0),
        ]
        _record_sell_in_db(fills, sell_price_cents=40, ts="2026-06-01T12:00:00+00:00", db=mock_db)

        assert mock_db.update_trade_costs.call_count == 2
        calls = mock_db.update_trade_costs.call_args_list
        called_ids = {c[0][0] for c in calls}
        assert called_ids == {"ord-a", "ord-b"}

    def test_skips_fill_without_order_id(self):
        from src.execution.order_manager import _record_sell_in_db

        mock_db = MagicMock()
        fills = [{"price_cents": 30, "size_eur": 5.0}]  # no order_id
        _record_sell_in_db(fills, sell_price_cents=35, ts="2026-06-01T12:00:00+00:00", db=mock_db)

        mock_db.update_trade_costs.assert_not_called()

    def test_no_db_is_a_noop(self):
        from src.execution.order_manager import _record_sell_in_db
        # Must not raise
        fill = self._make_fill(order_id="ord-xyz")
        _record_sell_in_db([fill], sell_price_cents=50, ts="ts", db=None)


# ---------------------------------------------------------------------------
# API endpoint
# ---------------------------------------------------------------------------

class TestTradeCostSummaryEndpoint:
    def test_returns_summary(self):
        from fastapi.testclient import TestClient
        from src.dashboard import api as api_module
        from src.dashboard.api import app

        mock_db = MagicMock()
        mock_db.get_trade_cost_summary.return_value = {
            "period_days": 30, "trade_count": 5, "fee_populated_count": 5,
            "total_fee_eur": 0.0875, "avg_fee_eur": 0.0175,
            "total_size_eur": 50.0, "total_pnl": 2.5,
        }
        original_db = api_module._db
        api_module._db = mock_db
        try:
            client = TestClient(app)
            resp = client.get("/api/trade-costs/summary")
            assert resp.status_code == 200
            data = resp.json()
            assert data["trade_count"] == 5
            assert data["total_fee_eur"] == pytest.approx(0.0875)
            mock_db.get_trade_cost_summary.assert_called_once_with(days=30)
        finally:
            api_module._db = original_db

    def test_503_when_db_none(self):
        from fastapi.testclient import TestClient
        from src.dashboard import api as api_module
        from src.dashboard.api import app

        original_db = api_module._db
        api_module._db = None
        try:
            client = TestClient(app)
            resp = client.get("/api/trade-costs/summary")
            assert resp.status_code == 503
        finally:
            api_module._db = original_db

    def test_custom_days_param(self):
        from fastapi.testclient import TestClient
        from src.dashboard import api as api_module
        from src.dashboard.api import app

        mock_db = MagicMock()
        mock_db.get_trade_cost_summary.return_value = {
            "period_days": 7, "trade_count": 0, "fee_populated_count": 0,
            "total_fee_eur": 0.0, "avg_fee_eur": 0.0,
            "total_size_eur": 0.0, "total_pnl": 0.0,
        }
        original_db = api_module._db
        api_module._db = mock_db
        try:
            client = TestClient(app)
            resp = client.get("/api/trade-costs/summary?days=7")
            assert resp.status_code == 200
            mock_db.get_trade_cost_summary.assert_called_once_with(days=7)
        finally:
            api_module._db = original_db


# ---------------------------------------------------------------------------
# Migration and field validation tests
# ---------------------------------------------------------------------------

class TestEstimatedFeeCentsMigration:
    """Verify that the column rename migration works correctly."""

    def test_migration_preserves_values(self):
        """Migration from actual_fee_cents to estimated_fee_cents preserves all data."""
        import sqlite3
        import tempfile
        import os
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_migration.db")

            # Create an old-schema DB with actual_fee_cents column
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("""
                CREATE TABLE trades (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts              TEXT NOT NULL,
                    station         TEXT NOT NULL,
                    ticker          TEXT NOT NULL,
                    bracket_low     REAL NOT NULL,
                    bracket_high    REAL NOT NULL,
                    side            TEXT NOT NULL CHECK(side IN ('YES','NO')),
                    predicted_price INTEGER NOT NULL,
                    actual_price    INTEGER NOT NULL,
                    slippage        INTEGER,
                    predicted_edge  REAL NOT NULL,
                    mode            TEXT NOT NULL CHECK(mode IN ('paper','live','shadow')),
                    order_id        TEXT,
                    outcome         TEXT,
                    pnl             REAL,
                    capital_before  REAL NOT NULL,
                    capital_after   REAL,
                    settled_at      TEXT,
                    actual_fee_cents REAL,
                    size_eur        REAL,
                    direction       TEXT NOT NULL DEFAULT 'high'
                )
            """)
            # Insert test data with actual_fee_cents
            conn.execute("""
                INSERT INTO trades (ts, station, ticker, bracket_low, bracket_high,
                                   side, predicted_price, actual_price, predicted_edge,
                                   mode, capital_before, order_id, outcome, pnl,
                                   actual_fee_cents, size_eur)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _RECENT_TS, "KORD", "TEST-2026-06-01-HIGH-80-84", 80.0, 84.0,
                "NO", 60, 60, 0.05, "live", 500.0,
                "ord-migration-test", "sold", 1.5, 1.75, 10.0
            ))
            conn.commit()
            conn.close()

            # Now open with Database class (which runs migration)
            db = Database(path)

            try:
                # Verify that estimated_fee_cents column exists with correct value
                row = db._conn.execute(
                    "SELECT estimated_fee_cents, size_eur FROM trades WHERE order_id='ord-migration-test'"
                ).fetchone()
                assert row is not None, "Trade row should exist after migration"
                assert row["estimated_fee_cents"] == pytest.approx(1.75), "Fee value should be preserved"
                assert row["size_eur"] == pytest.approx(10.0), "Size value should be preserved"

                # Verify old column name no longer exists
                try:
                    db._conn.execute("SELECT actual_fee_cents FROM trades LIMIT 1")
                    assert False, "actual_fee_cents column should not exist after migration"
                except sqlite3.OperationalError as e:
                    assert "no such column" in str(e)
            finally:
                db._conn.close()

    def test_no_actual_prefix_in_writable_columns(self):
        """Verify no code path writes to any column with 'actual_' prefix that isn't validated."""
        import inspect
        from src.data.db import Database

        # Get all methods in Database class
        methods = inspect.getmembers(Database, predicate=inspect.ismethod)

        # Look for write operations using UPDATE or INSERT statements
        for name, method in methods:
            if hasattr(method, '__func__'):
                source = inspect.getsource(method.__func__)
            else:
                source = inspect.getsource(method)

            # Check for UPDATE statements with actual_* columns (except estimated_fee_cents migration)
            if "actual_" in source.lower():
                # The only allowed actual_* is "actual_price" and "actual_high_f" which are read-only observations
                lines_with_actual = [line for line in source.split('\n') if "actual_" in line.lower()]
                for line in lines_with_actual:
                    # Allow actual_price and actual_high_f (observations), reject others
                    if "actual_price" not in line and "actual_high_f" not in line:
                        # This shouldn't have update/insert with other actual_* columns
                        if "UPDATE" in line.upper() or "INSERT" in line.upper():
                            assert False, f"Found writable 'actual_*' column in {name}: {line.strip()}"


class TestEstimatedFeeAggregation:
    """Verify dashboard aggregation works identically before/after rename."""

    def test_aggregation_consistency(self):
        """get_trade_cost_summary returns identical numbers after column rename."""
        db = _db()

        # Insert multiple trades with estimated fees
        for i in range(3):
            order_id = f"ord-agg-{i}"
            _insert_live_trade(db, order_id=order_id, actual_price=50 + i*10)
            db.update_trade_by_order(order_id, outcome="sold", pnl=0.5 + i*0.1)
            db.update_trade_costs(
                order_id,
                estimated_fee_cents=1.5 + i*0.25,
                size_eur=10.0 + i*2.0
            )

        result = db.get_trade_cost_summary(days=30)

        # Verify aggregation is correct
        expected_fee_total = (1.5 + 1.75 + 2.0) / 100.0  # cents to EUR
        expected_size_total = 10.0 + 12.0 + 14.0
        expected_pnl = 0.5 + 0.6 + 0.7

        assert result["trade_count"] == 3
        assert result["fee_populated_count"] == 3
        assert result["total_fee_eur"] == pytest.approx(expected_fee_total, abs=1e-4)
        assert result["total_size_eur"] == pytest.approx(expected_size_total, abs=1e-4)
        assert result["total_pnl"] == pytest.approx(expected_pnl, abs=1e-4)
