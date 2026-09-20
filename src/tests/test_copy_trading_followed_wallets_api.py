"""Tests for the Copy-Trading dashboard Followed Wallets view backend
(epic F #1143, story F2 #1147).

Covers:
- GET /api/copy-trading/followed-wallets: response shape, per-wallet
  running P&L, active/paused counts, aggregate P&L.
- POST /api/copy-trading/wallets/{address}/pause: success + refusal paths
  (unknown address, empty reason), reusing copy_wallet_promotion.py::pause().
- POST /api/copy-trading/wallets/{address}/resume: success + refusal paths
  (unknown address, not paused, roster full), reusing
  copy_wallet_promotion.py::resume().
- POST /api/copy-trading/wallets/{address}/unfollow: success + refusal
  path (unknown address); DELETEs the row without touching copy_positions.
- PATCH /api/copy-trading/wallets/{address}/stake: success + refusal paths
  (unknown address, non-positive stake, exceeds max exposure).
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


def _settle(db, address, *, stake_usd, pnl, entry_ts="2026-09-01T00:00:00Z",
            settled_at="2026-09-02T00:00:00Z", market="TEST-MARKET"):
    """Insert one settled copy_positions row for *address* via a throwaway
    signal, mirroring test_copy_pnl.py's fixture-building pattern."""
    signal_id = db.insert_copy_signal(
        address=address, market=market, source_price=0.5, detected_at=entry_ts,
    )
    position_id = db.insert_copy_position(
        signal_id=signal_id, address=address, market=market, outcome_index=0,
        entry_price=0.5, stake_usd=stake_usd, entry_ts=entry_ts,
    )
    db.settle_copy_position(position_id, pnl, settled_at)


# ---------------------------------------------------------------------------
# GET /api/copy-trading/followed-wallets
# ---------------------------------------------------------------------------

class TestFollowedWalletsEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.get("/api/copy-trading/followed-wallets")
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_empty_when_no_wallets_followed(self, api_client):
        client, _ = api_client
        resp = client.get("/api/copy-trading/followed-wallets")
        assert resp.status_code == 200
        data = resp.json()
        assert data["wallets"] == []
        assert data["active_count"] == 0
        assert data["paused_count"] == 0
        assert data["aggregate_pnl_usd"] == 0.0
        assert data["n_settled_total"] == 0

    def test_active_and_paused_counts(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xActive", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.insert_followed_wallet(address="0xPaused", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.update_followed_wallet_status("0xPaused", "paused", "unstable")

        resp = client.get("/api/copy-trading/followed-wallets")
        data = resp.json()
        assert data["active_count"] == 1
        assert data["paused_count"] == 1

        rows = {w["address"]: w for w in data["wallets"]}
        assert rows["0xPaused"]["status"] == "paused"
        assert rows["0xPaused"]["paused_reason"] == "unstable"
        assert rows["0xActive"]["status"] == "active"
        assert rows["0xActive"]["paused_reason"] is None

    def test_per_wallet_and_aggregate_pnl(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW1", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.insert_followed_wallet(address="0xW2", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        _settle(db, "0xW1", stake_usd=5.0, pnl=3.0)
        _settle(db, "0xW1", stake_usd=5.0, pnl=-1.0)
        _settle(db, "0xW2", stake_usd=5.0, pnl=2.0)

        resp = client.get("/api/copy-trading/followed-wallets")
        data = resp.json()
        rows = {w["address"]: w for w in data["wallets"]}
        assert rows["0xW1"]["realized_pnl_usd"] == pytest.approx(2.0)
        assert rows["0xW1"]["n_settled"] == 2
        assert rows["0xW2"]["realized_pnl_usd"] == pytest.approx(2.0)
        assert data["aggregate_pnl_usd"] == pytest.approx(4.0)
        assert data["n_settled_total"] == 3

    def test_wallet_with_no_settled_positions_has_zero_pnl(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xFresh", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.get("/api/copy-trading/followed-wallets")
        row = resp.json()["wallets"][0]
        assert row["realized_pnl_usd"] == 0.0
        assert row["n_settled"] == 0

    def test_sorted_most_recently_added_first(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xOld", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.insert_followed_wallet(address="0xNew", stake_per_trade=5.0, added_at="2026-09-02T00:00:00Z")

        resp = client.get("/api/copy-trading/followed-wallets")
        addresses = [w["address"] for w in resp.json()["wallets"]]
        assert addresses == ["0xNew", "0xOld"]


# ---------------------------------------------------------------------------
# POST /api/copy-trading/wallets/{address}/pause
# ---------------------------------------------------------------------------

class TestPauseEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.post("/api/copy-trading/wallets/0xabc/pause", json={"reason": "x"})
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_success_path(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.post("/api/copy-trading/wallets/0xW/pause", json={"reason": "manual review"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True

        wallets = {w["address"]: w for w in db.get_followed_wallets()}
        assert wallets["0xW"]["status"] == "paused"
        assert wallets["0xW"]["paused_reason"] == "manual review"

    def test_refuses_when_not_followed(self, api_client):
        client, _ = api_client
        resp = client.post("/api/copy-trading/wallets/0xGhost/pause", json={"reason": "x"})
        body = resp.json()
        assert body["success"] is False
        assert "not a followed wallet" in body["message"]

    def test_refuses_empty_reason(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.post("/api/copy-trading/wallets/0xW/pause", json={"reason": "   "})
        body = resp.json()
        assert body["success"] is False
        assert "reason" in body["message"].lower()
        assert db.get_followed_wallets()[0]["status"] == "active"


# ---------------------------------------------------------------------------
# POST /api/copy-trading/wallets/{address}/resume
# ---------------------------------------------------------------------------

class TestResumeEndpoint:
    def test_success_path(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.update_followed_wallet_status("0xW", "paused", "unstable")

        resp = client.post("/api/copy-trading/wallets/0xW/resume")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        wallets = {w["address"]: w for w in db.get_followed_wallets()}
        assert wallets["0xW"]["status"] == "active"
        assert wallets["0xW"]["paused_reason"] is None

    def test_refuses_when_not_followed(self, api_client):
        client, _ = api_client
        resp = client.post("/api/copy-trading/wallets/0xGhost/resume")
        body = resp.json()
        assert body["success"] is False
        assert "not a followed wallet" in body["message"]

    def test_refuses_when_not_paused(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.post("/api/copy-trading/wallets/0xW/resume")
        body = resp.json()
        assert body["success"] is False
        assert "not 'paused'" in body["message"]

    def test_refuses_when_roster_full(self, api_client):
        client, db = api_client
        db.set_config("COPY_MAX_WALLETS_FOLLOWED", "1")
        db.insert_followed_wallet(address="0xActive", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.insert_followed_wallet(address="0xPaused", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.update_followed_wallet_status("0xPaused", "paused", "unstable")

        resp = client.post("/api/copy-trading/wallets/0xPaused/resume")
        body = resp.json()
        assert body["success"] is False
        assert "active wallets already followed" in body["message"]


# ---------------------------------------------------------------------------
# POST /api/copy-trading/wallets/{address}/unfollow
# ---------------------------------------------------------------------------

class TestUnfollowEndpoint:
    def test_success_path_deletes_row(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.post("/api/copy-trading/wallets/0xW/unfollow")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "not affected" in body["message"]
        assert db.get_followed_wallets() == []

    def test_refuses_when_not_followed(self, api_client):
        client, _ = api_client
        resp = client.post("/api/copy-trading/wallets/0xGhost/unfollow")
        body = resp.json()
        assert body["success"] is False
        assert "not a followed wallet" in body["message"]

    def test_open_positions_survive_unfollow(self, api_client):
        """Acceptance criteria: unfollow must NOT close/touch existing
        open positions."""
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        signal_id = db.insert_copy_signal(
            address="0xW", market="M", source_price=0.5, detected_at="2026-09-01T00:00:00Z",
        )
        db.insert_copy_position(
            signal_id=signal_id, address="0xW", market="M", outcome_index=0,
            entry_price=0.5, stake_usd=5.0, entry_ts="2026-09-01T00:00:00Z",
        )

        resp = client.post("/api/copy-trading/wallets/0xW/unfollow")
        assert resp.json()["success"] is True

        open_positions = db.get_open_copy_positions("0xW")
        assert len(open_positions) == 1
        assert open_positions[0]["status"] == "open"

    def test_can_refollow_after_unfollow(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        client.post("/api/copy-trading/wallets/0xW/unfollow")

        # Re-inserting after unfollow must not raise a duplicate-PK error.
        db.insert_followed_wallet(address="0xW", stake_per_trade=7.0, added_at="2026-09-05T00:00:00Z")
        wallets = {w["address"]: w for w in db.get_followed_wallets()}
        assert wallets["0xW"]["stake_per_trade"] == 7.0


# ---------------------------------------------------------------------------
# PATCH /api/copy-trading/wallets/{address}/stake
# ---------------------------------------------------------------------------

class TestUpdateStakeEndpoint:
    def test_success_path(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.patch("/api/copy-trading/wallets/0xW/stake", json={"stake": 12.5})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        wallets = {w["address"]: w for w in db.get_followed_wallets()}
        assert wallets["0xW"]["stake_per_trade"] == 12.5

    def test_refuses_when_not_followed(self, api_client):
        client, _ = api_client
        resp = client.patch("/api/copy-trading/wallets/0xGhost/stake", json={"stake": 5.0})
        body = resp.json()
        assert body["success"] is False
        assert "not a followed wallet" in body["message"]

    def test_refuses_non_positive_stake(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.patch("/api/copy-trading/wallets/0xW/stake", json={"stake": 0})
        body = resp.json()
        assert body["success"] is False
        wallets = {w["address"]: w for w in db.get_followed_wallets()}
        assert wallets["0xW"]["stake_per_trade"] == 5.0  # unchanged

    def test_refuses_stake_exceeding_max_exposure(self, api_client):
        client, db = api_client
        db.set_config("COPY_MAX_EXPOSURE_PER_WALLET_USD", "50.0")
        db.insert_followed_wallet(address="0xW", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.patch("/api/copy-trading/wallets/0xW/stake", json={"stake": 500.0})
        body = resp.json()
        assert body["success"] is False
        assert "COPY_MAX_EXPOSURE_PER_WALLET_USD" in body["message"]
        wallets = {w["address"]: w for w in db.get_followed_wallets()}
        assert wallets["0xW"]["stake_per_trade"] == 5.0  # unchanged
