"""Tests for the per-wallet ``signals_24h`` placed-vs-skipped object on
GET /api/copy-trading/positions (issue #1389)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.config import seed_config
from src.data.db import Database


@pytest.fixture()
def api_client():
    from src.dashboard import api as api_module

    original_db = api_module._db
    db = Database(":memory:")
    seed_config(db)
    api_module.set_db(db)
    yield TestClient(api_module.app, raise_server_exceptions=True), db
    api_module.set_db(original_db)


def _ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _sig(db, address, hours_ago, *, reason=None):
    db.insert_copy_signal(
        address=address, market="M", source_price=0.5, detected_at=_ts(hours_ago),
        outcome_index=0, order_placed=0 if reason else 1, skip_reason=reason,
    )


def _wallet(db, address):
    db.insert_followed_wallet(address=address, stake_per_trade=5.0, added_at="2026-09-01T00:00:00Z")


def _rows(client):
    return {w["address"]: w for w in client.get("/api/copy-trading/positions").json()["per_wallet"]}


def test_placed_and_skipped_counts_with_sorted_display_ready_reasons(api_client):
    client, db = api_client
    _wallet(db, "0xbca08c1b99")
    for _ in range(2):
        _sig(db, "0xbca08c1b99", 1)
    for _ in range(3):
        _sig(db, "0xbca08c1b99", 2, reason="non_binary_outcome_index")
    _sig(db, "0xbca08c1b99", 3, reason="duplicate_market_exposure")
    _sig(db, "0xbca08c1b99", 3, reason="brand_new_reason")
    _sig(db, "0xbca08c1b99", 30)  # outside the 24h window
    _sig(db, "0xbca08c1b99", 30, reason="duplicate_market_exposure")

    s = _rows(client)["0xbca08c1b99"]["signals_24h"]
    assert s["placed"] == 2
    assert s["skipped"] == 5 == sum(r["count"] for r in s["reasons"])
    assert [r["reason"] for r in s["reasons"]] == [
        "non_binary_outcome_index", "brand_new_reason", "duplicate_market_exposure",
    ]
    top = s["reasons"][0]
    assert top["class"] == "structural" and top["class_label"] == "Structural"
    assert top["meaning"].startswith("Trade is on a multi-outcome market")
    assert top["accepted_ref"] == "#1390"
    assert top["trend"] == "STEADY"
    unk = s["reasons"][1]
    assert unk["class"] == "unknown" and unk["class_label"] == "Unrecognized"
    assert unk["meaning"] == "Unrecognized skip reason"


def test_wallet_without_signals_reports_zero_not_null(api_client):
    client, db = api_client
    _wallet(db, "0xQuiet")
    assert _rows(client)["0xQuiet"]["signals_24h"] == {"placed": 0, "skipped": 0, "reasons": []}


def test_query_failure_yields_null_and_endpoint_still_works(api_client, monkeypatch):
    client, db = api_client
    _wallet(db, "0xW")

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "get_copy_signal_skip_counts", boom)
    resp = client.get("/api/copy-trading/positions")
    assert resp.status_code == 200
    assert resp.json()["per_wallet"][0]["signals_24h"] is None


def test_live_per_wallet_rows_have_no_signals(api_client):
    client, db = api_client
    _wallet(db, "0xW")
    data = client.get("/api/copy-trading/positions").json()
    assert all(w.get("signals_24h") is None for w in data["live_per_wallet"])
