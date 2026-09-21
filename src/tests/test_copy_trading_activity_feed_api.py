"""Tests for the Copy-Trading dashboard Activity Feed view backend
(epic F #1143, story F4 #1149).

Covers GET /api/copy-trading/activity-feed:
- Merges copy_signals rows (order_placed / order_skipped) and
  copy_wallets_followed 'paused' rows into one normalized,
  reverse-chronological event list.
- wallet (address) and event_type filters.
- A paused_at IS NULL row (pre-#1145 legacy pause) does not crash the
  endpoint and produces no pause event.
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

    Mirrors test_copy_trading_positions_api.py's fixture of the same name.
    """
    from src.dashboard import api as api_module

    original_db = api_module._db
    db = Database(":memory:")
    seed_config(db)
    api_module.set_db(db)

    client = TestClient(api_module.app, raise_server_exceptions=True)
    yield client, db

    api_module.set_db(original_db)


def _order_placed_signal(db, address, *, market="TEST-MARKET",
                          detected_at="2026-09-01T00:00:00Z",
                          source_price=0.49, fill_price=0.5, size_usd=5.0):
    return db.insert_copy_signal(
        address=address, market=market, source_price=source_price,
        detected_at=detected_at, outcome_index=0, order_placed=1,
        fill_price=fill_price, size_usd=size_usd,
    )


def _order_skipped_signal(db, address, *, market="TEST-MARKET",
                           detected_at="2026-09-01T00:00:00Z",
                           source_price=0.49, skip_reason="market_resolved"):
    return db.insert_copy_signal(
        address=address, market=market, source_price=source_price,
        detected_at=detected_at, outcome_index=0, order_placed=0,
        skip_reason=skip_reason,
    )


def _pause_wallet(db, address, *, paused_at="2026-09-01T12:00:00Z",
                   paused_reason="manual"):
    """Follow then pause *address*, writing a real paused_at timestamp
    directly (update_followed_wallet_status() always stamps "now", so a
    fixed, deterministic paused_at needs a direct UPDATE)."""
    db.insert_followed_wallet(address=address, stake_per_trade=5.0, added_at="2026-08-01T00:00:00Z")
    db._conn.execute(
        "UPDATE copy_wallets_followed SET status='paused', paused_reason=?, paused_at=? WHERE address=?",
        (paused_reason, paused_at, address),
    )
    db._conn.commit()


def _pause_wallet_legacy_null_paused_at(db, address, *, paused_reason="manual"):
    """A pre-#1145 legacy pause: status='paused' but paused_at IS NULL."""
    db.insert_followed_wallet(address=address, stake_per_trade=5.0, added_at="2026-08-01T00:00:00Z")
    db._conn.execute(
        "UPDATE copy_wallets_followed SET status='paused', paused_reason=?, paused_at=NULL WHERE address=?",
        (paused_reason, address),
    )
    db._conn.commit()


# ---------------------------------------------------------------------------
# GET /api/copy-trading/activity-feed
# ---------------------------------------------------------------------------

class TestActivityFeedEndpoint:
    def test_503_when_db_not_initialised(self):
        from src.dashboard import api as api_module
        original_db = api_module._db
        try:
            api_module.set_db(None)
            client = TestClient(api_module.app, raise_server_exceptions=False)
            resp = client.get("/api/copy-trading/activity-feed")
            assert resp.status_code == 503
        finally:
            api_module.set_db(original_db)

    def test_empty_when_nothing_happened(self, api_client):
        client, _ = api_client
        resp = client.get("/api/copy-trading/activity-feed")
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    def test_order_placed_event_shape(self, api_client):
        client, db = api_client
        signal_id = _order_placed_signal(
            db, "0xW1", market="M1", detected_at="2026-09-01T00:00:00Z",
            source_price=0.4, fill_price=0.42, size_usd=5.0,
        )
        resp = client.get("/api/copy-trading/activity-feed")
        events = resp.json()["events"]
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "order_placed"
        assert e["ts"] == "2026-09-01T00:00:00Z"
        assert e["address"] == "0xW1"
        assert e["market"] == "M1"
        assert e["source_price"] == pytest.approx(0.4)
        assert e["fill_price"] == pytest.approx(0.42)
        assert e["size_usd"] == pytest.approx(5.0)
        assert e["signal_id"] == signal_id
        assert e["skip_reason"] is None

    def test_order_skipped_event_shape(self, api_client):
        client, db = api_client
        signal_id = _order_skipped_signal(
            db, "0xW1", market="M1", detected_at="2026-09-01T00:00:00Z",
            skip_reason="wallet_exposure_limit",
        )
        resp = client.get("/api/copy-trading/activity-feed")
        events = resp.json()["events"]
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "order_skipped"
        assert e["skip_reason"] == "wallet_exposure_limit"
        assert e["signal_id"] == signal_id
        assert e["fill_price"] is None
        assert e["size_usd"] is None

    def test_wallet_paused_event_shape(self, api_client):
        client, db = api_client
        _pause_wallet(db, "0xW1", paused_at="2026-09-03T00:00:00Z", paused_reason="unstable")
        resp = client.get("/api/copy-trading/activity-feed")
        events = resp.json()["events"]
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "wallet_paused"
        assert e["ts"] == "2026-09-03T00:00:00Z"
        assert e["address"] == "0xW1"
        assert e["paused_reason"] == "unstable"
        assert e["market"] is None
        assert e["signal_id"] is None

    def test_active_followed_wallet_produces_no_pause_event(self, api_client):
        client, db = api_client
        db.insert_followed_wallet(address="0xActive", stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")
        resp = client.get("/api/copy-trading/activity-feed")
        assert resp.json()["events"] == []

    def test_legacy_paused_at_null_row_does_not_crash_and_produces_no_event(self, api_client):
        """Acceptance criteria: a paused_at IS NULL row (pre-#1145 legacy)
        must not crash the endpoint, just produce no pause event."""
        client, db = api_client
        _pause_wallet_legacy_null_paused_at(db, "0xLegacy")
        resp = client.get("/api/copy-trading/activity-feed")
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    def test_interleaves_signal_and_pause_events_in_timestamp_order(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")
        _pause_wallet(db, "0xW1", paused_at="2026-09-03T00:00:00Z")
        _order_skipped_signal(db, "0xW2", detected_at="2026-09-02T00:00:00Z")

        resp = client.get("/api/copy-trading/activity-feed")
        events = resp.json()["events"]
        assert [e["ts"] for e in events] == [
            "2026-09-03T00:00:00Z", "2026-09-02T00:00:00Z", "2026-09-01T00:00:00Z",
        ]
        assert [e["event_type"] for e in events] == [
            "wallet_paused", "order_skipped", "order_placed",
        ]

    def test_filters_by_wallet_address(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")
        _order_placed_signal(db, "0xW2", detected_at="2026-09-02T00:00:00Z")
        _pause_wallet(db, "0xW2", paused_at="2026-09-03T00:00:00Z")

        resp = client.get("/api/copy-trading/activity-feed", params={"address": "0xW2"})
        events = resp.json()["events"]
        assert len(events) == 2
        assert all(e["address"] == "0xW2" for e in events)

    def test_filters_by_event_type(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")
        _order_skipped_signal(db, "0xW1", detected_at="2026-09-02T00:00:00Z")
        _pause_wallet(db, "0xW1", paused_at="2026-09-03T00:00:00Z")

        resp = client.get("/api/copy-trading/activity-feed", params={"event_type": "wallet_paused"})
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "wallet_paused"

    def test_combined_address_and_event_type_filters(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")
        _order_placed_signal(db, "0xW2", detected_at="2026-09-01T00:00:00Z")
        _order_skipped_signal(db, "0xW1", detected_at="2026-09-02T00:00:00Z")

        resp = client.get(
            "/api/copy-trading/activity-feed",
            params={"address": "0xW1", "event_type": "order_placed"},
        )
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["address"] == "0xW1"
        assert events[0]["event_type"] == "order_placed"

    def test_unknown_event_type_yields_no_matches_not_an_error(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")

        resp = client.get("/api/copy-trading/activity-feed", params={"event_type": "bogus"})
        assert resp.status_code == 200
        assert resp.json()["events"] == []
