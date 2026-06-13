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


# ---------------------------------------------------------------------------
# GET /api/stations/overview
# ---------------------------------------------------------------------------

class TestStationsOverviewEndpoint:
    """Tests for the /api/stations/overview endpoint (issue #229)."""

    def _setup_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def _inject_db(self, db):
        """Inject db and clear the stations overview cache."""
        dash_api.set_db(db)
        dash_api._stations_overview_cache["ts"] = 0.0
        dash_api._stations_overview_cache["data"] = None

    def test_returns_200_and_list(self, client):
        """Endpoint must return 200 and a list."""
        resp = client.get("/api/stations/overview")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_returns_all_configured_stations(self, client):
        """One entry per configured STATION must appear."""
        from src.config import STATIONS
        resp = client.get("/api/stations/overview")
        data = resp.json()
        metars = {s[0] for s in STATIONS}
        response_metars = {item["metar"] for item in data}
        assert response_metars == metars

    def test_station_record_has_required_keys(self, client):
        """Each record must include all required fields."""
        resp = client.get("/api/stations/overview")
        item = resp.json()[0]
        required = {
            "metar", "city", "lat", "lon", "unit", "timezone",
            "active_hours_local", "enabled", "trade_count", "filled_count",
            "win_rate", "total_pnl", "last_trade_ts", "open_positions_count",
            "last_obs_ts", "status",
        }
        assert required.issubset(item.keys())

    def test_disabled_station_has_disabled_status(self, client):
        """A station in DISABLED_STATIONS must have status='disabled' and enabled=False."""
        from src.config import STATIONS
        original = dash_api._db
        # Patch DISABLED_STATIONS to disable the first station
        first_metar = STATIONS[0][0]
        try:
            with patch("src.dashboard.api.DISABLED_STATIONS", {first_metar}):
                dash_api._stations_overview_cache["ts"] = 0.0
                dash_api._stations_overview_cache["data"] = None
                resp = client.get("/api/stations/overview")
            data = resp.json()
            item = next(s for s in data if s["metar"] == first_metar)
            assert item["enabled"] is False
            assert item["status"] == "disabled"
        finally:
            dash_api._db = original

    def test_station_outside_hours_status(self, client):
        """A station outside its active hours must have status='outside_hours'."""
        from src.config import STATIONS
        first_metar = STATIONS[0][0]
        # Patch to ensure station is enabled but active hours exclude current hour
        # We use hour 25 as end so no real hour can be "in" [0, 0) but let's use [0,0]
        with patch("src.dashboard.api.DISABLED_STATIONS", set()):
            with patch("src.dashboard.api.STATION_ACTIVE_HOURS", {first_metar: (0, 0)}):
                dash_api._stations_overview_cache["ts"] = 0.0
                dash_api._stations_overview_cache["data"] = None
                resp = client.get("/api/stations/overview")
        data = resp.json()
        item = next(s for s in data if s["metar"] == first_metar)
        assert item["status"] == "outside_hours"

    def test_station_no_data_status_when_no_obs(self, client):
        """A station with no observations and in active hours must have status='no_data'."""
        from src.config import STATIONS
        db = self._setup_db()
        first_metar = STATIONS[0][0]
        original = dash_api._db
        try:
            self._inject_db(db)
            # Force active hours to cover all 24h and no disabled stations
            with patch("src.dashboard.api.DISABLED_STATIONS", set()):
                with patch("src.dashboard.api.STATION_ACTIVE_HOURS", {first_metar: (0, 24)}):
                    resp = client.get("/api/stations/overview")
            data = resp.json()
            item = next(s for s in data if s["metar"] == first_metar)
            assert item["status"] == "no_data"
            assert item["last_obs_ts"] is None
        finally:
            dash_api.set_db(original)

    def test_station_active_status_with_fresh_obs(self, client):
        """A station with a recent observation and in active hours must have status='active'."""
        from src.config import STATIONS
        db = self._setup_db()
        first_metar = STATIONS[0][0]
        # Insert a fresh observation (now)
        now_iso = datetime.now(timezone.utc).isoformat()
        db.insert_observation(
            ts=now_iso, station=first_metar, temp_f=75.0, temp_native=75.0,
            unit="F", source="test",
        )
        original = dash_api._db
        try:
            self._inject_db(db)
            with patch("src.dashboard.api.DISABLED_STATIONS", set()):
                with patch("src.dashboard.api.STATION_ACTIVE_HOURS", {first_metar: (0, 24)}):
                    resp = client.get("/api/stations/overview")
            data = resp.json()
            item = next(s for s in data if s["metar"] == first_metar)
            assert item["status"] == "active"
            assert item["last_obs_ts"] is not None
        finally:
            dash_api.set_db(original)

    def test_trade_stats_reflected(self, client):
        """Trade stats (trade_count, filled_count, win_rate, total_pnl) must come from the DB."""
        from src.config import STATIONS
        db = self._setup_db()
        first_metar = STATIONS[0][0]
        # Insert two trades: one win, one loss
        for i, pnl in enumerate([3.0, -1.0]):
            db.insert_trade(
                ts=f"2026-06-01T1{i}:00:00Z", station=first_metar,
                ticker=f"{first_metar}-t{i}", bracket_low=70.0, bracket_high=74.0,
                side="NO", predicted_price=80, actual_price=81,
                predicted_edge=0.10, mode="live", capital_before=500.0,
                capital_after=500.0 + pnl, outcome="filled", pnl=pnl,
            )
        original = dash_api._db
        try:
            self._inject_db(db)
            resp = client.get("/api/stations/overview")
            data = resp.json()
            item = next(s for s in data if s["metar"] == first_metar)
            assert item["trade_count"] == 2
            assert item["filled_count"] == 2
            assert item["win_rate"] == pytest.approx(0.5)
            assert item["total_pnl"] == pytest.approx(2.0)
        finally:
            dash_api.set_db(original)

    def test_open_positions_count_from_db(self, client):
        """open_positions_count must reflect the DB open_positions table."""
        from src.config import STATIONS
        db = self._setup_db()
        first_metar = STATIONS[0][0]
        # Insert a trade then an open position referencing it
        trade_id = db.insert_trade(
            ts="2026-06-01T10:00:00Z", station=first_metar,
            ticker=f"{first_metar}-op", bracket_low=70.0, bracket_high=74.0,
            side="NO", predicted_price=80, actual_price=80,
            predicted_edge=0.10, mode="live", capital_before=500.0,
        )
        db.open_position(
            trade_id=trade_id, station=first_metar,
            ticker=f"{first_metar}-op", token_id="tok123",
            side="NO", order_id="order-x", entry_price=80,
            shares=10.0, entry_ts="2026-06-01T10:00:00Z",
        )
        original = dash_api._db
        try:
            self._inject_db(db)
            resp = client.get("/api/stations/overview")
            data = resp.json()
            item = next(s for s in data if s["metar"] == first_metar)
            assert item["open_positions_count"] == 1
        finally:
            dash_api.set_db(original)

    def test_cache_returns_same_response(self, client):
        """Two rapid requests must return identical data (cache hit on second)."""
        resp1 = client.get("/api/stations/overview")
        resp2 = client.get("/api/stations/overview")
        assert resp1.status_code == 200
        assert resp1.json() == resp2.json()


# ---------------------------------------------------------------------------
# EMOS management API endpoints (issue #232)
# ---------------------------------------------------------------------------

class TestEmosStatusEndpoint:
    """Tests for GET /api/emos/status."""

    def _setup_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def test_returns_list_for_all_cities(self, client):
        """Status endpoint returns one entry per city in STATIONS."""
        from src.config import STATIONS
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.get("/api/emos/status")
            assert resp.status_code == 200
            data = resp.json()
            assert isinstance(data, list)
            assert len(data) == len(STATIONS)
        finally:
            dash_api.set_db(original)

    def test_status_response_keys(self, client):
        """Each status entry has the required keys."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.get("/api/emos/status")
            assert resp.status_code == 200
            item = resp.json()[0]
            for key in ("city", "metar", "effective_mode", "shadow", "primary",
                        "settled_days_available", "min_settled_days_required"):
                assert key in item, f"Missing key: {key}"
        finally:
            dash_api.set_db(original)

    def test_status_shadow_null_when_no_calibration(self, client):
        """shadow and primary are null when no calibration rows exist."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.get("/api/emos/status")
            assert resp.status_code == 200
            for item in resp.json():
                assert item["shadow"] is None
                assert item["primary"] is None
        finally:
            dash_api.set_db(original)

    def test_status_shadow_populated(self, client):
        """shadow block is populated when a shadow calibration row exists."""
        db = self._setup_db()
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=-0.5, b=1.02, c=0.8, d=0.95,
            crps_score=1.72, trained_at="2026-05-01T00:00:00Z",
            ready_for_promotion=1,
        )
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.get("/api/emos/status")
            assert resp.status_code == 200
            data = resp.json()
            chicago = next(d for d in data if d["city"] == "Chicago")
            assert chicago["shadow"] is not None
            assert chicago["shadow"]["a"] == pytest.approx(-0.5)
            assert chicago["shadow"]["crps_score"] == pytest.approx(1.72)
            assert chicago["shadow"]["ready_for_promotion"] is True
            assert chicago["primary"] is None
        finally:
            dash_api.set_db(original)

    def test_status_effective_mode_from_config_default(self, client):
        """effective_mode defaults to EMOS_DEFAULT_MODE when no override is set."""
        from src.config import EMOS_DEFAULT_MODE
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.get("/api/emos/status")
            data = resp.json()
            for item in data:
                assert item["effective_mode"] == EMOS_DEFAULT_MODE
        finally:
            dash_api.set_db(original)

    def test_status_503_when_db_none(self, client):
        """Returns 503 when _db is None."""
        original = dash_api._db
        try:
            dash_api._db = None
            resp = client.get("/api/emos/status")
            assert resp.status_code == 503
        finally:
            dash_api._db = original


class TestEmosPromoteEndpoint:
    """Tests for POST /api/emos/{city}/promote."""

    def _setup_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def _insert_shadow(self, db, city="Chicago", ready=1):
        db.upsert_emos_coefficients(
            city=city, model_mode="emos_shadow",
            a=-0.4, b=1.03, c=0.81, d=0.97,
            crps_score=1.61, trained_at="2026-05-28T00:00:00Z",
            ready_for_promotion=ready,
        )

    def test_promote_happy_path(self, client):
        """Promote succeeds when shadow exists and ready_for_promotion=1."""
        db = self._setup_db()
        self._insert_shadow(db, "Chicago", ready=1)
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/promote")
            assert resp.status_code == 200
            data = resp.json()
            assert data["city"] == "Chicago"
            assert data["effective_mode"] == "emos_primary"
            assert data["primary"] is not None
            assert data["primary"]["a"] == pytest.approx(-0.4)
        finally:
            dash_api.set_db(original)

    def test_promote_409_no_shadow(self, client):
        """Promote returns 409 when no shadow row exists."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/promote")
            assert resp.status_code == 409
        finally:
            dash_api.set_db(original)

    def test_promote_409_not_ready(self, client):
        """Promote returns 409 when shadow exists but ready_for_promotion=0."""
        db = self._setup_db()
        self._insert_shadow(db, "Chicago", ready=0)
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/promote")
            assert resp.status_code == 409
        finally:
            dash_api.set_db(original)

    def test_promote_404_unknown_city(self, client):
        """Promote returns 404 for an unknown city name."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/NotACity/promote")
            assert resp.status_code == 404
        finally:
            dash_api.set_db(original)

    def test_promote_url_encoded_city(self, client):
        """Promote works with URL-encoded city names (spaces → %20)."""
        db = self._setup_db()
        db.upsert_emos_coefficients(
            city="Kuala Lumpur", model_mode="emos_shadow",
            a=0.1, b=1.0, c=0.5, d=1.0,
            crps_score=2.0, trained_at="2026-05-01T00:00:00Z",
            ready_for_promotion=1,
        )
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Kuala%20Lumpur/promote")
            assert resp.status_code == 200
            assert resp.json()["city"] == "Kuala Lumpur"
        finally:
            dash_api.set_db(original)


class TestEmosDemoteEndpoint:
    """Tests for POST /api/emos/{city}/demote."""

    def _setup_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def test_demote_sets_legacy_mode(self, client):
        """Demote sets effective mode to legacy."""
        db = self._setup_db()
        # First set it to emos_primary
        db.set_emos_effective_mode("Chicago", "emos_primary")
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/demote")
            assert resp.status_code == 200
            data = resp.json()
            assert data["city"] == "Chicago"
            assert data["effective_mode"] == "legacy"
        finally:
            dash_api.set_db(original)

    def test_demote_is_idempotent(self, client):
        """Demote is safe to call when already in legacy mode."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            # Call twice
            resp1 = client.post("/api/emos/Chicago/demote")
            resp2 = client.post("/api/emos/Chicago/demote")
            assert resp1.status_code == 200
            assert resp2.status_code == 200
            assert resp2.json()["effective_mode"] == "legacy"
        finally:
            dash_api.set_db(original)

    def test_demote_404_unknown_city(self, client):
        """Demote returns 404 for an unknown city name."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Nowhere/demote")
            assert resp.status_code == 404
        finally:
            dash_api.set_db(original)

    def test_demote_does_not_delete_calibration(self, client):
        """Demote preserves shadow/primary calibration rows."""
        db = self._setup_db()
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=-0.4, b=1.03, c=0.81, d=0.97,
            crps_score=1.61, trained_at="2026-05-28T00:00:00Z",
            ready_for_promotion=1,
        )
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_primary",
            a=-0.4, b=1.03, c=0.81, d=0.97,
            crps_score=1.61, trained_at="2026-05-28T00:00:00Z",
            ready_for_promotion=0,
        )
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/demote")
            assert resp.status_code == 200
            data = resp.json()
            assert data["shadow"] is not None, "Shadow row should be preserved after demote"
            assert data["primary"] is not None, "Primary row should be preserved after demote"
        finally:
            dash_api.set_db(original)


class TestEmosMarkReadyEndpoint:
    """Tests for POST /api/emos/{city}/mark-ready."""

    def _setup_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def test_mark_ready_toggles_0_to_1(self, client):
        """mark-ready toggles ready_for_promotion from 0 to 1."""
        db = self._setup_db()
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=-0.4, b=1.03, c=0.81, d=0.97,
            crps_score=1.61, trained_at="2026-05-28T00:00:00Z",
            ready_for_promotion=0,
        )
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/mark-ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["shadow"]["ready_for_promotion"] is True
        finally:
            dash_api.set_db(original)

    def test_mark_ready_toggles_1_to_0(self, client):
        """mark-ready toggles ready_for_promotion from 1 back to 0."""
        db = self._setup_db()
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow",
            a=-0.4, b=1.03, c=0.81, d=0.97,
            crps_score=1.61, trained_at="2026-05-28T00:00:00Z",
            ready_for_promotion=1,
        )
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/mark-ready")
            assert resp.status_code == 200
            data = resp.json()
            assert data["shadow"]["ready_for_promotion"] is False
        finally:
            dash_api.set_db(original)

    def test_mark_ready_409_no_shadow(self, client):
        """mark-ready returns 409 when no shadow row exists."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Chicago/mark-ready")
            assert resp.status_code == 409
        finally:
            dash_api.set_db(original)

    def test_mark_ready_404_unknown_city(self, client):
        """mark-ready returns 404 for unknown city."""
        db = self._setup_db()
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp = client.post("/api/emos/Nowhere/mark-ready")
            assert resp.status_code == 404
        finally:
            dash_api.set_db(original)

    def test_mark_ready_double_toggle_round_trips(self, client):
        """Calling mark-ready twice returns to the original state."""
        db = self._setup_db()
        db.upsert_emos_coefficients(
            city="Miami", model_mode="emos_shadow",
            a=0.0, b=1.0, c=0.5, d=1.0,
            crps_score=2.0, trained_at="2026-05-01T00:00:00Z",
            ready_for_promotion=0,
        )
        original = dash_api._db
        try:
            dash_api.set_db(db)
            resp1 = client.post("/api/emos/Miami/mark-ready")
            assert resp1.json()["shadow"]["ready_for_promotion"] is True
            resp2 = client.post("/api/emos/Miami/mark-ready")
            assert resp2.json()["shadow"]["ready_for_promotion"] is False
        finally:
            dash_api.set_db(original)
