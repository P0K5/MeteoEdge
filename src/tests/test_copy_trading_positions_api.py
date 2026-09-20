"""Tests for the Copy-Trading dashboard Positions & P&L view backend
(epic F #1143, story F3 #1148).

Covers:
- GET /api/copy-trading/positions: response shape, open positions,
  realized-P&L history (raw settled rows, oldest first), per-wallet
  breakdown (including previously-followed wallets with settled history),
  backtest-comparison figures, and totals.
- GET /api/copy-trading/signals/{signal_id}: source-signal click-through,
  including the 404 path for an unknown id.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data.db import Database
from src.config import seed_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client():
    """FastAPI TestClient with a fresh seeded in-memory Database injected.

    Mirrors test_copy_trading_candidates_api.py's fixture of the same name.
    """
    from src.dashboard import api as api_module

    original_db = api_module._db
    db = Database(":memory:")
    seed_config(db)
    api_module.set_db(db)

    client = TestClient(api_module.app, raise_server_exceptions=True)
    yield client, db

    api_module.set_db(original_db)


def _open_position(db, address, *, market="TEST-MARKET", entry_ts="2026-09-01T00:00:00Z",
                    stake_usd=5.0, entry_price=0.5, source_price=0.49):
    """Insert one open copy_positions row (with its originating signal),
    mirroring test_copy_trading_followed_wallets_api.py's `_settle` helper.
    Returns (signal_id, position_id)."""
    signal_id = db.insert_copy_signal(
        address=address, market=market, source_price=source_price, detected_at=entry_ts,
        outcome_index=0, order_placed=1, fill_price=entry_price, size_usd=stake_usd,
    )
    position_id = db.insert_copy_position(
        signal_id=signal_id, address=address, market=market, outcome_index=0,
        entry_price=entry_price, stake_usd=stake_usd, entry_ts=entry_ts,
    )
    return signal_id, position_id


def _settle(db, address, *, stake_usd, pnl, entry_ts="2026-09-01T00:00:00Z",
            settled_at="2026-09-02T00:00:00Z", market="TEST-MARKET"):
    """Insert one settled copy_positions row for *address*."""
    _signal_id, position_id = _open_position(
        db, address, market=market, entry_ts=entry_ts, stake_usd=stake_usd,
    )
    db.settle_copy_position(position_id, pnl, settled_at)


# ---------------------------------------------------------------------------
# GET /api/copy-trading/positions
# ---------------------------------------------------------------------------

class TestPositionsEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.get("/api/copy-trading/positions")
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_empty_when_nothing_followed_or_settled(self, api_client):
        client, _ = api_client
        resp = client.get("/api/copy-trading/positions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["open_positions"] == []
        assert data["realized_pnl_history"] == []
        assert data["per_wallet"] == []
        assert data["total"] == {"n_settled": 0, "realized_pnl_usd": 0.0}
        assert data["backtest_total"]["n_wallets"] == 0

    def test_open_positions_shape(self, api_client):
        client, db = api_client
        signal_id, position_id = _open_position(db, "0xW1", stake_usd=5.0, entry_price=0.4)

        resp = client.get("/api/copy-trading/positions")
        data = resp.json()
        assert len(data["open_positions"]) == 1
        pos = data["open_positions"][0]
        assert pos["id"] == position_id
        assert pos["address"] == "0xW1"
        assert pos["market"] == "TEST-MARKET"
        assert pos["entry_price"] == pytest.approx(0.4)
        assert pos["stake_usd"] == pytest.approx(5.0)
        assert pos["signal_id"] == signal_id

    def test_settled_positions_excluded_from_open(self, api_client):
        client, db = api_client
        _settle(db, "0xW1", stake_usd=5.0, pnl=1.0)
        resp = client.get("/api/copy-trading/positions")
        assert resp.json()["open_positions"] == []

    def test_realized_pnl_history_raw_rows_oldest_first(self, api_client):
        client, db = api_client
        _settle(db, "0xW1", stake_usd=5.0, pnl=3.0, settled_at="2026-09-05T00:00:00Z")
        _settle(db, "0xW1", stake_usd=5.0, pnl=-1.0, settled_at="2026-09-01T00:00:00Z")

        resp = client.get("/api/copy-trading/positions")
        history = resp.json()["realized_pnl_history"]
        assert [p["settled_at"] for p in history] == [
            "2026-09-01T00:00:00Z", "2026-09-05T00:00:00Z",
        ]
        assert [p["settled_pnl_usd"] for p in history] == [-1.0, 3.0]

    def test_per_wallet_breakdown_and_totals(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW1", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.insert_followed_wallet(address="0xW2", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        _settle(db, "0xW1", stake_usd=5.0, pnl=3.0)
        _settle(db, "0xW1", stake_usd=5.0, pnl=-1.0)
        _settle(db, "0xW2", stake_usd=5.0, pnl=2.0)

        resp = client.get("/api/copy-trading/positions")
        data = resp.json()
        rows = {w["address"]: w for w in data["per_wallet"]}
        assert rows["0xW1"]["realized_pnl_usd"] == pytest.approx(2.0)
        assert rows["0xW1"]["n_settled"] == 2
        assert rows["0xW2"]["realized_pnl_usd"] == pytest.approx(2.0)
        assert data["total"] == {"n_settled": 3, "realized_pnl_usd": pytest.approx(4.0)}

    def test_per_wallet_sorted_by_realized_pnl_desc(self, api_client):
        client, db = api_client
        _settle(db, "0xLow", stake_usd=5.0, pnl=-2.0)
        _settle(db, "0xHigh", stake_usd=5.0, pnl=9.0)

        resp = client.get("/api/copy-trading/positions")
        addresses = [w["address"] for w in resp.json()["per_wallet"]]
        assert addresses == ["0xHigh", "0xLow"]

    def test_previously_followed_wallet_with_settled_history_still_shown(self, api_client):
        """A wallet unfollowed after settling positions must still appear
        in the per-wallet breakdown (issue #1148 acceptance criteria --
        unfollow never touches copy_positions, see
        copy_trading_unfollow_wallet's docstring)."""
        client, db = api_client
        db.insert_followed_wallet(address="0xGone", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        _settle(db, "0xGone", stake_usd=5.0, pnl=4.0)
        db.delete_followed_wallet("0xGone")

        resp = client.get("/api/copy-trading/positions")
        addresses = [w["address"] for w in resp.json()["per_wallet"]]
        assert "0xGone" in addresses

    def test_followed_wallet_with_no_settled_positions_has_zero_pnl_row(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xFresh", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.get("/api/copy-trading/positions")
        rows = {w["address"]: w for w in resp.json()["per_wallet"]}
        assert rows["0xFresh"]["n_settled"] == 0
        assert rows["0xFresh"]["realized_pnl_usd"] == 0.0

    def test_backtest_comparison_figures_present_when_screened(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW1", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.insert_wallet_screening(
            address="0xW1", window="30d", screened_at="2026-09-01T00:00:00Z",
            n_buy_trades=10, n_resolved=10, slippage_bps=50,
            flat_dollar_pnl=10.0, eligible_to_follow=1,
        )
        _settle(db, "0xW1", stake_usd=5.0, pnl=12.0)

        resp = client.get("/api/copy-trading/positions")
        row = {w["address"]: w for w in resp.json()["per_wallet"]}["0xW1"]
        assert row["projected_flat_dollar_pnl"] == pytest.approx(10.0)
        assert row["divergence_usd"] == pytest.approx(2.0)

        backtest_total = resp.json()["backtest_total"]
        assert backtest_total["n_wallets"] == 1
        assert backtest_total["realized_pnl_usd"] == pytest.approx(12.0)
        assert backtest_total["projected_flat_dollar_pnl"] == pytest.approx(10.0)

    def test_backtest_comparison_figures_none_when_unscreened(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xNoScreen", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.get("/api/copy-trading/positions")
        row = {w["address"]: w for w in resp.json()["per_wallet"]}["0xNoScreen"]
        assert row["projected_flat_dollar_pnl"] is None
        assert row["divergence_usd"] is None
        assert row["divergence_pct"] is None


# ---------------------------------------------------------------------------
# GET /api/copy-trading/signals/{signal_id}
# ---------------------------------------------------------------------------

class TestSignalEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.get("/api/copy-trading/signals/1")
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_404_for_unknown_signal(self, api_client):
        client, _ = api_client
        resp = client.get("/api/copy-trading/signals/999")
        assert resp.status_code == 404

    def test_returns_signal_for_a_positions_signal_id(self, api_client):
        client, db = api_client
        signal_id, _position_id = _open_position(
            db, "0xW1", market="M1", entry_price=0.42, source_price=0.4, stake_usd=5.0,
        )

        # Fetch the signal_id via the positions endpoint, exactly as the
        # frontend's click-through does.
        positions_resp = client.get("/api/copy-trading/positions")
        pos = positions_resp.json()["open_positions"][0]
        assert pos["signal_id"] == signal_id

        resp = client.get(f"/api/copy-trading/signals/{pos['signal_id']}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == signal_id
        assert body["address"] == "0xW1"
        assert body["market"] == "M1"
        assert body["source_price"] == pytest.approx(0.4)
        assert body["order_placed"] is True
        assert body["fill_price"] == pytest.approx(0.42)
