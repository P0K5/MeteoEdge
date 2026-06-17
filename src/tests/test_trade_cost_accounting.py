"""Tests for #321 — trade cost accounting.

Covers:
- Database.update_trade_costs() writes actual_fee_cents and size_eur
- Database.get_trade_cost_summary() aggregates live closed trades
- _record_sell_in_db() calls update_trade_costs() for all close paths
- GET /api/trade-costs/summary endpoint
"""
import sys
from unittest.mock import MagicMock, patch, call

import pytest

from src.data.db import Database
from src.strategy.fee import estimate_fee_cents


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    return Database(":memory:")


def _insert_live_trade(db: Database, order_id: str = "ord-001", actual_price: int = 60) -> int:
    return db.insert_trade(
        ts="2026-06-01T10:00:00+00:00",
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
    def test_writes_actual_fee_cents(self):
        db = _db()
        _insert_live_trade(db, order_id="ord-A", actual_price=50)
        rows = db._conn.execute(
            "UPDATE trades SET outcome='sold' WHERE order_id='ord-A'"
        )
        db._conn.commit()

        n = db.update_trade_costs("ord-A", actual_fee_cents=1.75)
        assert n == 1
        row = db._conn.execute(
            "SELECT actual_fee_cents FROM trades WHERE order_id='ord-A'"
        ).fetchone()
        assert row["actual_fee_cents"] == pytest.approx(1.75)

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
        db.update_trade_costs("ord-C", actual_fee_cents=2.0, size_eur=5.0)
        # Update with all-None — should be a no-op
        n = db.update_trade_costs("ord-C")
        assert n == 0
        row = db._conn.execute(
            "SELECT actual_fee_cents, size_eur FROM trades WHERE order_id='ord-C'"
        ).fetchone()
        assert row["actual_fee_cents"] == pytest.approx(2.0)
        assert row["size_eur"] == pytest.approx(5.0)

    def test_returns_zero_for_unknown_order(self):
        db = _db()
        n = db.update_trade_costs("does-not-exist", actual_fee_cents=1.0)
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
        db.update_trade_costs(order_id, actual_fee_cents=fee, size_eur=size_eur)

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
            ts="2026-06-01T10:00:00+00:00",
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
# _record_sell_in_db wires actual_fee_cents
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
            actual_fee_cents=pytest.approx(expected_fee),
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
