"""Tests for the Copy-Trading dashboard Activity Feed view backend
(epic F #1143, story F4 #1149; live events + mode filter, epic J #1161,
issue #1188).

Covers GET /api/copy-trading/activity-feed:
- Merges copy_signals rows (order_placed / order_skipped),
  copy_wallets_followed 'paused' rows, and (issue #1188)
  copy_live_positions rows into one normalized, reverse-chronological
  event list.
- wallet (address), event_type, and mode filters.
- A paused_at IS NULL row (pre-#1145 legacy pause) does not crash the
  endpoint and produces no pause event.
- Every event carries an explicit, always-populated ``mode`` ("live" or
  "paper").
- copy_live_positions rows synthesize one event per row, keyed off
  status, including the rejected-vs-skipped-vs-circuit-breaker-tripped
  classification.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.data.db import Database
from src.config import seed_config
from src.risk.copy_risk_manager import REASON_LIVE_DAILY_LOSS, REASON_LIVE_DRAWDOWN


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


def _live_position(db, address, *, market="LIVE-MARKET", entry_ts="2026-09-05T00:00:00Z",
                    stake_usd=10.0, status="pending", order_id=None, fill_price=None,
                    rejected_reason=None, filled_stake_usd=None,
                    settled_pnl_usd=None, settled_at=None):
    """Create one ``copy_live_positions`` row in the given terminal (or
    pending) *status*, FK-linked to a fresh ``copy_signals`` row (the
    schema requires a real ``signal_id``).
    """
    signal_id = db.insert_copy_signal(
        address=address, market=market, source_price=0.5, outcome_index=0,
        detected_at=entry_ts, order_placed=1, fill_price=0.5, size_usd=stake_usd,
    )
    position_id = db.insert_copy_live_position(
        signal_id=signal_id, address=address, market=market, outcome_index=0,
        stake_usd=stake_usd, entry_ts=entry_ts, status="pending",
    )
    if status != "pending":
        db.update_copy_live_position_status(
            position_id, status=status, order_id=order_id, fill_price=fill_price,
            rejected_reason=rejected_reason, filled_stake_usd=filled_stake_usd,
        )
    if status == "settled":
        # settle_copy_live_position only transitions from filled/partial --
        # bring the row there first via a direct UPDATE (test-only shortcut,
        # mirrors settle_copy_live_position's own WHERE-scoped semantics).
        db._conn.execute("UPDATE copy_live_positions SET status='filled' WHERE id=?", (position_id,))
        db._conn.commit()
        db.settle_copy_live_position(position_id, settled_pnl_usd, settled_at)
    return position_id


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

    def test_paper_events_are_always_mode_paper(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")
        _order_skipped_signal(db, "0xW1", detected_at="2026-09-02T00:00:00Z")
        _pause_wallet(db, "0xW1", paused_at="2026-09-03T00:00:00Z")

        resp = client.get("/api/copy-trading/activity-feed")
        events = resp.json()["events"]
        assert len(events) == 3
        assert all(e["mode"] == "paper" for e in events)


# ---------------------------------------------------------------------------
# GET /api/copy-trading/activity-feed — live events (issue #1188)
# ---------------------------------------------------------------------------

def _only(events, event_type):
    """Filter a raw events list down to one event_type -- ``_live_position``
    always creates a companion paper ``order_placed`` event too (the
    schema's ``copy_live_positions.signal_id`` FK requires a real
    ``copy_signals`` row), so tests that only care about the live-derived
    event isolate it this way rather than asserting a bare ``len() == 1``.
    """
    matches = [e for e in events if e["event_type"] == event_type]
    assert len(matches) == 1, f"expected exactly one {event_type!r} event, got {matches!r}"
    return matches[0]


class TestActivityFeedLiveEvents:
    def test_pending_live_position_produces_pending_event(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", market="M1", entry_ts="2026-09-05T00:00:00Z",
                        stake_usd=12.0, status="pending")

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_pending")
        assert e["mode"] == "live"
        assert e["ts"] == "2026-09-05T00:00:00Z"
        assert e["address"] == "0xW1"
        assert e["market"] == "M1"
        assert e["size_usd"] == pytest.approx(12.0)

    def test_filled_live_position_produces_filled_event(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="filled", order_id="ord-1", fill_price=0.55)

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_filled")
        assert e["mode"] == "live"
        assert e["fill_price"] == pytest.approx(0.55)
        assert e["size_usd"] == pytest.approx(10.0)

    def test_partial_live_position_produces_partial_event(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="partial", order_id="ord-1", fill_price=0.55,
                        filled_stake_usd=4.2)

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_partial")
        assert e["fill_price"] == pytest.approx(0.55)
        assert e["filled_stake_usd"] == pytest.approx(4.2)

    def test_settled_live_position_uses_settled_at_as_ts(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="settled", settled_pnl_usd=3.5, settled_at="2026-09-08T00:00:00Z")

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_position_settled")
        assert e["ts"] == "2026-09-08T00:00:00Z", "settled events must be timestamped at settlement, not entry"
        assert e["settled_pnl_usd"] == pytest.approx(3.5)

    def test_post_submission_rejection_produces_rejected_event_with_plain_text(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="rejected", order_id="ord-1", rejected_reason="timeout")

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_rejected")
        assert e["rejected_reason"] == "no fill before the timeout window closed"

    def test_ghost_order_is_a_rejected_event_not_skipped(self, api_client):
        """cancel_failed_ghost carries an order_id (a real attempt reached
        the exchange) -- it must classify as rejected, never skipped."""
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="rejected", order_id="ord-1",
                        rejected_reason="cancel_failed_ghost", fill_price=0.5)

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_rejected")
        assert "reconciliation" in e["rejected_reason"]

    def test_unexpected_error_rejection_includes_detail_in_plain_text(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="rejected", rejected_reason="unexpected_error:boom")

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_rejected")
        assert e["rejected_reason"] == "an unexpected error occurred (boom)"

    def test_place_failed_with_no_order_id_is_still_rejected_not_skipped(self, api_client):
        """place_failed never gets an order_id (copy_live_executor's own
        contract) -- the order_id-absence must NOT be used as a proxy for
        'gate-skipped before submission'."""
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="rejected", order_id=None, rejected_reason="place_failed")

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        _only(events, "live_order_rejected")

    def test_gate_skip_reason_produces_skipped_event_with_plain_text(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="rejected", rejected_reason="live_wallet_exposure_limit")

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_skipped")
        assert e["skip_reason"] == "this wallet's live exposure cap was reached"

    def test_sanity_check_message_falls_through_as_skipped_with_raw_text(self, api_client):
        """The dynamic live_startup_sanity_check() message isn't a fixed
        constant -- it must fall through to 'skipped' (not 'rejected'),
        surfaced verbatim since it's already plain English."""
        client, db = api_client
        msg = ("COPY_LIVE_MAX_TOTAL_EXPOSURE_USD ($500.00) exceeds "
               "COPY_LIVE_CAPITAL_USD ($400.00) -- refusing to start.")
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="rejected", rejected_reason=msg)

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_order_skipped")
        assert e["skip_reason"] == msg

    @pytest.mark.parametrize("reason", [REASON_LIVE_DAILY_LOSS, REASON_LIVE_DRAWDOWN])
    def test_circuit_breaker_reasons_produce_tripped_event(self, api_client, reason):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-05T00:00:00Z", stake_usd=10.0,
                        status="rejected", rejected_reason=reason)

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        e = _only(events, "live_circuit_breaker_tripped")
        assert e["skip_reason"] and e["skip_reason"] != reason, "must be plain language, not the raw constant"

    def test_live_and_paper_events_interleave_by_timestamp(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", market="EXTRA", detected_at="2026-09-01T00:00:00Z")
        _live_position(db, "0xW1", entry_ts="2026-09-02T00:00:00Z", status="pending")
        _pause_wallet(db, "0xW1", paused_at="2026-09-03T00:00:00Z")

        events = client.get("/api/copy-trading/activity-feed").json()["events"]
        # _live_position's own FK-required companion signal is also
        # "order_placed", at the same 2026-09-02 timestamp as the live
        # event itself -- only the well-ordered boundary event (the pause,
        # strictly latest) is asserted positionally; the live event's
        # presence/mode/ts ordering relative to the earliest signal is
        # asserted directly instead.
        assert events[0]["event_type"] == "wallet_paused"
        assert events[-1]["event_type"] == "order_placed"
        assert events[-1]["market"] == "EXTRA"
        live_events = [e for e in events if e["mode"] == "live"]
        assert len(live_events) == 1
        assert live_events[0]["event_type"] == "live_order_pending"
        assert live_events[0]["ts"] == "2026-09-02T00:00:00Z"

    def test_mode_filter_live(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")
        _live_position(db, "0xW1", entry_ts="2026-09-02T00:00:00Z", status="pending")

        resp = client.get("/api/copy-trading/activity-feed", params={"mode": "live"})
        events = resp.json()["events"]
        assert len(events) == 1
        assert events[0]["event_type"] == "live_order_pending"

    def test_mode_filter_paper(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")
        _live_position(db, "0xW1", entry_ts="2026-09-02T00:00:00Z", status="pending")

        resp = client.get("/api/copy-trading/activity-feed", params={"mode": "paper"})
        events = resp.json()["events"]
        # _order_placed_signal's own row PLUS _live_position's own FK-required
        # companion signal are both "paper" -- two rows, both order_placed.
        assert len(events) == 2
        assert all(e["event_type"] == "order_placed" for e in events)
        assert all(e["mode"] == "paper" for e in events)

    def test_unknown_mode_yields_no_matches_not_an_error(self, api_client):
        client, db = api_client
        _order_placed_signal(db, "0xW1", detected_at="2026-09-01T00:00:00Z")

        resp = client.get("/api/copy-trading/activity-feed", params={"mode": "bogus"})
        assert resp.status_code == 200
        assert resp.json()["events"] == []

    def test_address_filter_applies_to_live_positions_too(self, api_client):
        client, db = api_client
        _live_position(db, "0xW1", entry_ts="2026-09-01T00:00:00Z", status="pending")
        _live_position(db, "0xW2", entry_ts="2026-09-02T00:00:00Z", status="pending")

        resp = client.get("/api/copy-trading/activity-feed", params={"address": "0xW2"})
        events = resp.json()["events"]
        assert all(e["address"] == "0xW2" for e in events)
        live_events = [e for e in events if e["mode"] == "live"]
        assert len(live_events) == 1
