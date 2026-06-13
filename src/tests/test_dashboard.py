"""Unit tests for the consolidated dashboard at src/dashboard/api.py.

Tests use tmp_path to write mock log files and patch config paths so the
dashboard reads from the temporary files rather than the real logs/ directory.

The bridge stub at src/monitoring/dashboard delegates to src/dashboard/api,
so importing from either module reaches the same implementation.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import src.dashboard.api as dash_api
from src.dashboard.api import app, _compute_win_rate, _today_pnl
from src.dashboard.data import read_jsonl as _read_jsonl


@pytest.fixture
def client():
    """Create a fresh TestClient for each test to avoid state leakage.

    Also resets module-level global state (_db, last_poll_ts) before and after
    each test to prevent test isolation issues.
    """
    # Save original state
    original_db = dash_api._db
    original_last_poll_ts = dash_api.last_poll_ts

    # Reset to clean state
    dash_api._db = None
    dash_api.last_poll_ts = None

    # Also clear the data cache to avoid stale hits between tests
    import src.dashboard.data as _data
    with _data._cache_lock:
        _data._cache.clear()

    # Yield fresh client
    test_client = TestClient(app)
    yield test_client

    # Restore original state after test
    dash_api._db = original_db
    dash_api.last_poll_ts = original_last_poll_ts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_returns_status_ok(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"

    def test_returns_uptime_seconds(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], int)
        assert data["uptime_seconds"] >= 0

    def test_returns_last_poll_field(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "last_poll" in data

    def test_returns_200_with_empty_log_dir(self, client, tmp_path):
        """Health endpoint must not crash when log directory does not exist."""
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "no_trades.jsonl"):
            with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "no_snaps.jsonl"):
                resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    def test_returns_expected_keys(self, client, tmp_path):
        _write_jsonl(tmp_path / "trades.jsonl", [])
        _write_jsonl(tmp_path / "snaps.jsonl", [])
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("capital", "today_pnl", "today_trade_count", "win_rate", "last_poll"):
            assert key in data, f"Missing key: {key}"

    def test_empty_logs_return_defaults(self, client, tmp_path):
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
            with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "missing2.jsonl"):
                resp = client.get("/status")
        data = resp.json()
        assert data["today_pnl"] == 0.0
        assert data["today_trade_count"] == 0
        assert data["win_rate"] == 0.0

    def test_today_pnl_sums_todays_trades(self, client, tmp_path):
        today = _today()
        records = [
            {"ts": f"{today}T10:00:00+00:00", "outcome": "filled", "pnl": 5.0, "station": "KORD"},
            {"ts": f"{today}T11:00:00+00:00", "outcome": "filled", "pnl": -2.0, "station": "KORD"},
            {"ts": "2020-01-01T10:00:00+00:00", "outcome": "filled", "pnl": 100.0, "station": "KORD"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        assert resp.json()["today_pnl"] == pytest.approx(3.0)

    def test_capital_from_snapshot(self, client, tmp_path):
        snaps = [{"capital": 480.0, "ts": "2024-01-01T00:00:00+00:00"}]
        _write_jsonl(tmp_path / "snaps.jsonl", snaps)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "no_trades.jsonl"):
            with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        assert resp.json()["capital"] == pytest.approx(480.0)

    def test_win_rate_is_float_between_0_and_1(self, client, tmp_path):
        today = _today()
        records = [
            {"ts": f"{today}T10:00:00+00:00", "outcome": "filled", "pnl": 3.0, "station": "KORD"}
            for _ in range(4)
        ] + [
            {"ts": f"{today}T10:00:00+00:00", "outcome": "filled", "pnl": -1.0, "station": "KORD"}
            for _ in range(6)
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        win_rate = resp.json()["win_rate"]
        assert 0.0 <= win_rate <= 1.0


# ---------------------------------------------------------------------------
# /trades
# ---------------------------------------------------------------------------

class TestTradesEndpoint:
    def test_returns_list(self, client, tmp_path):
        _write_jsonl(tmp_path / "trades.jsonl", [])
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/trades")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_returns_at_most_50(self, client, tmp_path):
        records = [{"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "station": "KORD", "i": i}
                   for i in range(100)]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/trades")
        assert len(resp.json()) == 50

    def test_returns_newest_first(self, client, tmp_path):
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "station": "A"},
            {"ts": "2024-06-01T00:00:00+00:00", "station": "B"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/trades")
        data = resp.json()
        assert data[0]["station"] == "B"
        assert data[1]["station"] == "A"

    def test_empty_log_returns_empty_list(self, client, tmp_path):
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
            resp = client.get("/trades")
        assert resp.json() == []


# ---------------------------------------------------------------------------
# /stations
# ---------------------------------------------------------------------------

class TestStationsEndpoint:
    def test_returns_dict(self, client, tmp_path):
        _write_jsonl(tmp_path / "trades.jsonl", [])
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_groups_by_station(self, client, tmp_path):
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 2.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": -1.0, "station": "KMIA"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        data = resp.json()
        assert "KORD" in data
        assert "KMIA" in data
        assert data["KORD"]["trade_count"] == 2
        assert data["KMIA"]["trade_count"] == 1

    def test_station_stats_structure(self, client, tmp_path):
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 5.0, "station": "KATL"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        station = resp.json()["KATL"]
        for key in ("trade_count", "filled_count", "win_rate", "total_pnl"):
            assert key in station

    def test_win_rate_calculated_per_station(self, client, tmp_path):
        # 3 wins, 1 loss → win rate = 0.75
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": -1.0, "station": "KORD"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        assert resp.json()["KORD"]["win_rate"] == pytest.approx(0.75)

    def test_empty_log_returns_empty_dict(self, client, tmp_path):
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
            resp = client.get("/stations")
        assert resp.json() == {}


# ---------------------------------------------------------------------------
# _read_jsonl helper (from shared data layer)
# ---------------------------------------------------------------------------

class TestReadJsonl:
    def test_returns_empty_list_for_missing_file(self, tmp_path):
        result = _read_jsonl(tmp_path / "nonexistent.jsonl")
        assert result == []

    def test_skips_malformed_lines(self, tmp_path):
        p = tmp_path / "mixed.jsonl"
        p.write_text('{"a": 1}\nnot_json\n{"b": 2}\n', encoding="utf-8")
        result = _read_jsonl(p)
        assert len(result) == 2
        assert result[0]["a"] == 1
        assert result[1]["b"] == 2


# ---------------------------------------------------------------------------
# Issue G — DB-backed /status: capital and open_positions_count
# ---------------------------------------------------------------------------

class TestStatusDbCapital:
    """Issue G acceptance criteria for /status capital and open_positions_count."""

    def _setup_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def test_status_capital_from_db(self, client, tmp_path):
        db = self._setup_db()
        db.insert_trade(
            ts="2024-01-15T12:00:00Z", station="KORD",
            ticker="KORD-test", bracket_low=32.0, bracket_high=36.0,
            side="NO", predicted_price=70, actual_price=71,
            predicted_edge=0.08, mode="live",
            capital_before=500.0, capital_after=450.0,
        )
        original = dash_api._db
        try:
            dash_api.set_db(db)
            with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
                with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "missing2.jsonl"):
                    resp = client.get("/status")
            assert resp.json()["capital"] == pytest.approx(450.0)
        finally:
            dash_api.set_db(original)

    def test_status_open_positions_count(self, client, tmp_path):
        db = self._setup_db()
        today = datetime.now(timezone.utc).date().isoformat()
        db.upsert_daily_risk(today, pnl_delta=0.0, open_positions=3)
        original = dash_api._db
        try:
            dash_api.set_db(db)
            with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
                with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "missing2.jsonl"):
                    resp = client.get("/status")
            assert resp.json()["open_positions_count"] == 3
        finally:
            dash_api.set_db(original)

    def test_status_defaults_no_data(self, client, tmp_path):
        db = self._setup_db()
        from src.config import STARTING_CAPITAL_EUR
        original = dash_api._db
        try:
            dash_api.set_db(db)
            with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
                with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "missing2.jsonl"):
                    resp = client.get("/status")
            data = resp.json()
            assert data["capital"] == pytest.approx(STARTING_CAPITAL_EUR)
            assert data["open_positions_count"] == 0
        finally:
            dash_api.set_db(original)

    def test_no_breaking_change(self, client, tmp_path):
        """All original keys must still be present plus open_positions_count."""
        original_keys = {"capital", "today_pnl", "today_trade_count", "win_rate", "last_poll"}
        with patch("src.dashboard.data.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
            with patch("src.dashboard.data.SNAPSHOTS_JSONL", tmp_path / "missing2.jsonl"):
                resp = client.get("/status")
        data = resp.json()
        assert original_keys.issubset(data.keys()), f"Missing keys: {original_keys - data.keys()}"
        assert "open_positions_count" in data


# ---------------------------------------------------------------------------
# Closed positions with exit_reason
# ---------------------------------------------------------------------------

class TestClosedPositionsExitReason:
    """Test exit_reason field on closed positions (early exits and settled)."""

    def test_stopped_position_take_profit_exit_reason(self, tmp_path):
        """_stopped_positions() should set exit_reason='take_profit' for take_profit@ triggers."""
        from src.dashboard.api import _stopped_positions
        records = [
            {
                "outcome": "sold",
                "trigger": "take_profit@0.75",
                "question": "Will it rain?",
                "station": "KORD",
                "entry_price_cents": 50,
                "price_cents": 75,
                "shares": 10.0,
                "pnl": 2.5,
                "ts": "2024-01-01T12:00:00+00:00",
                "no_token_id": "token123",
            }
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            positions = _stopped_positions()
        assert len(positions) == 1
        assert positions[0].exit_reason == "take_profit"

    def test_stopped_position_stop_loss_exit_reason(self, tmp_path):
        """_stopped_positions() should set exit_reason='stop_loss' for stop_loss@ triggers."""
        from src.dashboard.api import _stopped_positions
        records = [
            {
                "outcome": "sold",
                "trigger": "stop_loss@20",
                "question": "Will it rain?",
                "station": "KORD",
                "entry_price_cents": 50,
                "price_cents": 20,
                "shares": 10.0,
                "pnl": -3.0,
                "ts": "2024-01-01T12:00:00+00:00",
                "no_token_id": "token456",
            }
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            positions = _stopped_positions()
        assert len(positions) == 1
        assert positions[0].exit_reason == "stop_loss"

    def test_settled_position_won_exit_reason(self, tmp_path):
        """_settled_jsonl_positions() should set exit_reason='won' when pnl > 0."""
        from src.dashboard.api import _settled_jsonl_positions
        records = [
            {
                "outcome": "filled",
                "question": "Will it rain?",
                "station": "KORD",
                "side": "YES",
                "entry_price_cents": 50,
                "shares": 10.0,
                "pnl": 5.0,
                "size_eur": 500.0,
                "end_date": "2024-01-01T23:59:59+00:00",
                "no_token_id": "token789",
            }
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            positions = _settled_jsonl_positions()
        assert len(positions) == 1
        assert positions[0].exit_reason == "won"

    def test_settled_position_lost_exit_reason(self, tmp_path):
        """_settled_jsonl_positions() should set exit_reason='lost' when pnl <= 0."""
        from src.dashboard.api import _settled_jsonl_positions
        records = [
            {
                "outcome": "filled",
                "question": "Will it rain?",
                "station": "KORD",
                "side": "NO",
                "entry_price_cents": 50,
                "shares": 10.0,
                "pnl": -2.5,
                "size_eur": 500.0,
                "end_date": "2024-01-01T23:59:59+00:00",
                "no_token_id": "token012",
            }
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            positions = _settled_jsonl_positions()
        assert len(positions) == 1
        assert positions[0].exit_reason == "lost"

    def test_settled_position_zero_pnl_is_lost(self, tmp_path):
        """_settled_jsonl_positions() should set exit_reason='lost' when pnl == 0."""
        from src.dashboard.api import _settled_jsonl_positions
        records = [
            {
                "outcome": "filled",
                "question": "Will it rain?",
                "station": "KORD",
                "side": "YES",
                "entry_price_cents": 50,
                "shares": 10.0,
                "pnl": 0.0,
                "size_eur": 500.0,
                "end_date": "2024-01-01T23:59:59+00:00",
                "no_token_id": "token345",
            }
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.dashboard.api.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            positions = _settled_jsonl_positions()
        assert len(positions) == 1
        assert positions[0].exit_reason == "lost"


# ---------------------------------------------------------------------------
# Bridge stub compatibility — src.monitoring.dashboard still works
# ---------------------------------------------------------------------------

class TestBridgeStubCompat:
    """Verify that src.monitoring.dashboard correctly delegates to src.dashboard.api."""

    def test_last_poll_ts_synced(self):
        """Setting last_poll_ts via the bridge stub updates src.dashboard.api."""
        import src.monitoring.dashboard as stub
        original = dash_api.last_poll_ts
        try:
            stub.last_poll_ts = "2024-01-01T00:00:00+00:00"
            assert dash_api.last_poll_ts == "2024-01-01T00:00:00+00:00"
        finally:
            dash_api.last_poll_ts = original

    def test_set_db_delegates(self):
        """set_db() via the bridge stub injects into src.dashboard.api."""
        import src.monitoring.dashboard as stub
        from unittest.mock import MagicMock
        mock_db = MagicMock()
        original = dash_api._db
        try:
            stub.set_db(mock_db)
            assert dash_api._db is mock_db
        finally:
            dash_api._db = original

    def test_load_trades_callable(self, tmp_path):
        """_load_trades() from bridge stub returns a list."""
        import src.monitoring.dashboard as stub
        result = stub._load_trades()
        assert isinstance(result, list)

    def test_compute_win_rate_callable(self):
        """_compute_win_rate() from bridge stub computes correctly."""
        import src.monitoring.dashboard as stub
        trades = [
            {"outcome": "filled", "pnl": 1.0},
            {"outcome": "filled", "pnl": -1.0},
        ]
        rate = stub._compute_win_rate(trades, n=50)
        assert rate == pytest.approx(0.5)
