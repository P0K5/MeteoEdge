"""Tests for the Copy-Trading dashboard Candidates view backend (epic F
#1143, story F1 #1146).

Covers:
- GET /api/copy-trading/candidates: response shape, default sort
  (median_roi desc — never $ PnL), instability-flag computation, followed
  state, slots-remaining math.
- GET /api/copy-trading/wallets/{address}/history: sparkline data source.
- POST /api/copy-trading/wallets/{address}/follow: success path and the
  three refusal paths (roster full, ineligible, already followed), reusing
  copy_wallet_promotion.py::follow() rather than re-deriving its checks.
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

    Restores the original module-level `_db` on teardown so state doesn't
    leak into other test files (mirrors test_config_api.py's api_client
    fixture).
    """
    from src.dashboard import api as api_module

    original_db = api_module._db
    db = Database(":memory:")
    seed_config(db)
    api_module.set_db(db)

    client = TestClient(api_module.app, raise_server_exceptions=True)
    yield client, db

    api_module.set_db(original_db)


def _screen(
    db, address, *, screened_at, median_roi, n_resolved=100, window="month",
    win_rate=0.6, mean_roi=None, mirrored_dollar_pnl=10.0, flat_dollar_pnl=8.0,
    flat_stake=5.0, eligible_to_follow=0, n_buy_trades=None,
):
    """Convenience wrapper around Database.insert_wallet_screening()."""
    db.insert_wallet_screening(
        address=address,
        window=window,
        screened_at=screened_at,
        n_buy_trades=n_buy_trades if n_buy_trades is not None else n_resolved,
        n_resolved=n_resolved,
        win_rate=win_rate,
        mean_roi=mean_roi if mean_roi is not None else median_roi,
        median_roi=median_roi,
        mirrored_dollar_pnl=mirrored_dollar_pnl,
        flat_dollar_pnl=flat_dollar_pnl,
        flat_stake=flat_stake,
        slippage_bps=50.0,
        eligible_to_follow=eligible_to_follow,
    )


# ---------------------------------------------------------------------------
# GET /api/copy-trading/candidates
# ---------------------------------------------------------------------------

class TestCandidatesEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.get("/api/copy-trading/candidates")
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_empty_when_no_wallets_screened(self, api_client):
        client, _ = api_client
        resp = client.get("/api/copy-trading/candidates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["candidates"] == []
        assert data["max_followed"] == 10  # COPY_MAX_WALLETS_FOLLOWED default
        assert data["slots_remaining"] == 10
        assert data["active_follow_count"] == 0

    def test_default_sort_is_median_roi_descending(self, api_client):
        """Never sort by mirrored_dollar_pnl/flat_dollar_pnl as the default —
        the exact mistake the spike already made once (design spec)."""
        client, db = api_client
        # Wallet A has the highest $ PnL but the lowest median_roi -- if the
        # endpoint sorted by $ PnL it would rank first; it must rank last.
        _screen(db, "0xA", screened_at="2026-09-01T00:00:00Z", median_roi=0.01,
                mirrored_dollar_pnl=999.0, eligible_to_follow=0)
        _screen(db, "0xB", screened_at="2026-09-01T00:00:00Z", median_roi=0.50,
                mirrored_dollar_pnl=1.0, eligible_to_follow=0)
        _screen(db, "0xC", screened_at="2026-09-01T00:00:00Z", median_roi=0.20,
                mirrored_dollar_pnl=5.0, eligible_to_follow=0)

        resp = client.get("/api/copy-trading/candidates")
        addresses = [c["address"] for c in resp.json()["candidates"]]
        assert addresses == ["0xB", "0xC", "0xA"]

    def test_instability_flag_true_on_sign_flip(self, api_client):
        """Reproduces the 0xd3b034d7-style reversal the design spec calls
        out: median_roi flips sign across the wallet's last two runs."""
        client, db = api_client
        _screen(db, "0xUnstable", screened_at="2026-09-01T00:00:00Z",
                n_resolved=7498, median_roi=0.334, eligible_to_follow=0)
        _screen(db, "0xUnstable", screened_at="2026-09-01T15:00:00Z",
                n_resolved=7500, median_roi=-1.0, eligible_to_follow=0)

        resp = client.get("/api/copy-trading/candidates")
        row = resp.json()["candidates"][0]
        assert row["address"] == "0xUnstable"
        assert row["unstable"] is True
        assert row["eligible_to_follow"] is False

    def test_instability_flag_false_when_two_runs_agree(self, api_client):
        client, db = api_client
        _screen(db, "0xStable", screened_at="2026-09-01T00:00:00Z",
                n_resolved=100, median_roi=0.10, eligible_to_follow=0)
        _screen(db, "0xStable", screened_at="2026-09-02T00:00:00Z",
                n_resolved=105, median_roi=0.12, eligible_to_follow=1)

        resp = client.get("/api/copy-trading/candidates")
        row = resp.json()["candidates"][0]
        assert row["unstable"] is False
        assert row["eligible_to_follow"] is True

    def test_followed_flag_and_status(self, api_client):
        client, db = api_client
        _screen(db, "0xFollowed", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=1)
        db.insert_followed_wallet(
            address="0xFollowed", stake_per_trade=5.0, added_at="2026-09-02T00:00:00Z",
        )

        resp = client.get("/api/copy-trading/candidates")
        row = resp.json()["candidates"][0]
        assert row["followed"] is True
        assert row["follow_status"] == "active"

    def test_not_followed_wallet_has_null_status(self, api_client):
        client, db = api_client
        _screen(db, "0xNew", screened_at="2026-09-01T00:00:00Z", median_roi=0.10)

        resp = client.get("/api/copy-trading/candidates")
        row = resp.json()["candidates"][0]
        assert row["followed"] is False
        assert row["follow_status"] is None

    def test_slots_remaining_accounts_for_active_follows(self, api_client):
        client, db = api_client
        db.set_config("COPY_MAX_WALLETS_FOLLOWED", "2")
        db.insert_followed_wallet(address="0x1", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        db.insert_followed_wallet(address="0x2", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z", status="paused")

        resp = client.get("/api/copy-trading/candidates")
        data = resp.json()
        assert data["max_followed"] == 2
        assert data["active_follow_count"] == 1  # paused wallets don't count
        assert data["slots_remaining"] == 1


# ---------------------------------------------------------------------------
# GET /api/copy-trading/wallets/{address}/history
# ---------------------------------------------------------------------------

class TestWalletHistoryEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.get("/api/copy-trading/wallets/0xabc/history")
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_unknown_address_returns_empty_runs(self, api_client):
        client, _ = api_client
        resp = client.get("/api/copy-trading/wallets/0xghost/history")
        assert resp.status_code == 200
        assert resp.json() == {"address": "0xghost", "runs": []}

    def test_returns_runs_newest_first(self, api_client):
        client, db = api_client
        _screen(db, "0xW", screened_at="2026-09-01T00:00:00Z", median_roi=0.05)
        _screen(db, "0xW", screened_at="2026-09-02T00:00:00Z", median_roi=0.10)

        resp = client.get("/api/copy-trading/wallets/0xW/history")
        runs = resp.json()["runs"]
        assert [r["screened_at"] for r in runs] == [
            "2026-09-02T00:00:00Z", "2026-09-01T00:00:00Z",
        ]

    def test_respects_limit_param(self, api_client):
        client, db = api_client
        for i in range(5):
            _screen(db, "0xW", screened_at=f"2026-09-0{i+1}T00:00:00Z", median_roi=0.05)

        resp = client.get("/api/copy-trading/wallets/0xW/history?limit=2")
        assert len(resp.json()["runs"]) == 2


# ---------------------------------------------------------------------------
# POST /api/copy-trading/wallets/{address}/follow
# ---------------------------------------------------------------------------

class TestFollowEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.post("/api/copy-trading/wallets/0xabc/follow", json={})
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_success_path_defaults_stake(self, api_client):
        client, db = api_client
        _screen(db, "0xGood", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=1)

        resp = client.post("/api/copy-trading/wallets/0xGood/follow", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True

        followed = {w["address"]: w for w in db.get_followed_wallets()}
        assert followed["0xGood"]["stake_per_trade"] == 5.0  # COPY_DEFAULT_FLAT_STAKE_USD

    def test_success_path_explicit_stake(self, api_client):
        client, db = api_client
        _screen(db, "0xGood", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=1)

        resp = client.post(
            "/api/copy-trading/wallets/0xGood/follow", json={"stake": 25.0},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        followed = {w["address"]: w for w in db.get_followed_wallets()}
        assert followed["0xGood"]["stake_per_trade"] == 25.0

    def test_refuses_when_already_followed(self, api_client):
        client, db = api_client
        _screen(db, "0xDup", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=1)
        db.insert_followed_wallet(address="0xDup", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")

        resp = client.post("/api/copy-trading/wallets/0xDup/follow", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "already followed" in body["message"]

    def test_refuses_when_roster_full(self, api_client):
        client, db = api_client
        db.set_config("COPY_MAX_WALLETS_FOLLOWED", "1")
        db.insert_followed_wallet(address="0xExisting", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        _screen(db, "0xNew", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=1)

        resp = client.post("/api/copy-trading/wallets/0xNew/follow", json={})
        body = resp.json()
        assert body["success"] is False
        assert "active wallets already followed" in body["message"]

    def test_refuses_when_ineligible(self, api_client):
        client, db = api_client
        _screen(db, "0xUnstable", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=0)

        resp = client.post("/api/copy-trading/wallets/0xUnstable/follow", json={})
        body = resp.json()
        assert body["success"] is False
        assert "eligible_to_follow=0" in body["message"]

    def test_refuses_when_never_screened(self, api_client):
        client, _ = api_client
        resp = client.post("/api/copy-trading/wallets/0xGhost/follow", json={})
        body = resp.json()
        assert body["success"] is False
        assert "no screening run found" in body["message"]

    def test_refuses_when_stake_exceeds_max_exposure(self, api_client):
        """Trading-safety guardrail (AI review, PR #1152): an
        operator-submitted stake is otherwise unbounded above."""
        client, db = api_client
        db.set_config("COPY_MAX_EXPOSURE_PER_WALLET_USD", "50.0")
        _screen(db, "0xGood", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=1)

        resp = client.post(
            "/api/copy-trading/wallets/0xGood/follow", json={"stake": 500.0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "COPY_MAX_EXPOSURE_PER_WALLET_USD" in body["message"]
        assert db.get_followed_wallets() == []

    def test_follow_exception_returns_structured_refusal_not_500(self, api_client, monkeypatch):
        """If follow() ever raises, the endpoint must not leak a bare 500 —
        surface it as a structured, non-success result (AI review, PR #1152)."""
        client, db = api_client
        _screen(db, "0xGood", screened_at="2026-09-01T00:00:00Z",
                median_roi=0.10, eligible_to_follow=1)

        import src.scripts.copy_wallet_promotion as promotion_module

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(promotion_module, "follow", _boom)
        # The endpoint does `from ... import follow` inside the function body,
        # so patching the module attribute is picked up on the next call.

        resp = client.post("/api/copy-trading/wallets/0xGood/follow", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "boom" in body["message"]
