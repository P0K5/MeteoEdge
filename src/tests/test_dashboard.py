"""Unit tests for src/monitoring/dashboard.py.

Tests use tmp_path to write mock log files and patch config paths so the
dashboard reads from the temporary files rather than the real logs/ directory.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.monitoring.dashboard import app, _read_jsonl, _compute_win_rate, _today_pnl

client = TestClient(app)

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
    def test_returns_200(self):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_returns_status_ok(self):
        resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"

    def test_returns_uptime_seconds(self):
        resp = client.get("/health")
        data = resp.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], int)
        assert data["uptime_seconds"] >= 0

    def test_returns_last_poll_field(self):
        resp = client.get("/health")
        data = resp.json()
        assert "last_poll" in data

    def test_returns_200_with_empty_log_dir(self, tmp_path):
        """Health endpoint must not crash when log directory does not exist."""
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "no_trades.jsonl"):
            with patch("src.monitoring.dashboard.SNAPSHOTS_JSONL", tmp_path / "no_snaps.jsonl"):
                resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

class TestStatusEndpoint:
    def test_returns_expected_keys(self, tmp_path):
        _write_jsonl(tmp_path / "trades.jsonl", [])
        _write_jsonl(tmp_path / "snaps.jsonl", [])
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            with patch("src.monitoring.dashboard.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        for key in ("capital", "today_pnl", "today_trade_count", "win_rate", "last_poll"):
            assert key in data, f"Missing key: {key}"

    def test_empty_logs_return_defaults(self, tmp_path):
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
            with patch("src.monitoring.dashboard.SNAPSHOTS_JSONL", tmp_path / "missing2.jsonl"):
                resp = client.get("/status")
        data = resp.json()
        assert data["today_pnl"] == 0.0
        assert data["today_trade_count"] == 0
        assert data["win_rate"] == 0.0

    def test_today_pnl_sums_todays_trades(self, tmp_path):
        today = _today()
        records = [
            {"ts": f"{today}T10:00:00+00:00", "outcome": "filled", "pnl": 5.0, "station": "KORD"},
            {"ts": f"{today}T11:00:00+00:00", "outcome": "filled", "pnl": -2.0, "station": "KORD"},
            {"ts": "2020-01-01T10:00:00+00:00", "outcome": "filled", "pnl": 100.0, "station": "KORD"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            with patch("src.monitoring.dashboard.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        assert resp.json()["today_pnl"] == pytest.approx(3.0)

    def test_capital_from_snapshot(self, tmp_path):
        snaps = [{"capital": 480.0, "ts": "2024-01-01T00:00:00+00:00"}]
        _write_jsonl(tmp_path / "snaps.jsonl", snaps)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "no_trades.jsonl"):
            with patch("src.monitoring.dashboard.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        assert resp.json()["capital"] == pytest.approx(480.0)

    def test_win_rate_is_float_between_0_and_1(self, tmp_path):
        today = _today()
        records = [
            {"ts": f"{today}T10:00:00+00:00", "outcome": "filled", "pnl": 3.0, "station": "KORD"}
            for _ in range(4)
        ] + [
            {"ts": f"{today}T10:00:00+00:00", "outcome": "filled", "pnl": -1.0, "station": "KORD"}
            for _ in range(6)
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            with patch("src.monitoring.dashboard.SNAPSHOTS_JSONL", tmp_path / "snaps.jsonl"):
                resp = client.get("/status")
        win_rate = resp.json()["win_rate"]
        assert 0.0 <= win_rate <= 1.0


# ---------------------------------------------------------------------------
# /trades
# ---------------------------------------------------------------------------

class TestTradesEndpoint:
    def test_returns_list(self, tmp_path):
        _write_jsonl(tmp_path / "trades.jsonl", [])
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/trades")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_returns_at_most_50(self, tmp_path):
        records = [{"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "station": "KORD", "i": i}
                   for i in range(100)]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/trades")
        assert len(resp.json()) == 50

    def test_returns_newest_first(self, tmp_path):
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "station": "A"},
            {"ts": "2024-06-01T00:00:00+00:00", "station": "B"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/trades")
        data = resp.json()
        assert data[0]["station"] == "B"
        assert data[1]["station"] == "A"

    def test_empty_log_returns_empty_list(self, tmp_path):
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
            resp = client.get("/trades")
        assert resp.json() == []


# ---------------------------------------------------------------------------
# /stations
# ---------------------------------------------------------------------------

class TestStationsEndpoint:
    def test_returns_dict(self, tmp_path):
        _write_jsonl(tmp_path / "trades.jsonl", [])
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_groups_by_station(self, tmp_path):
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 2.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": -1.0, "station": "KMIA"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        data = resp.json()
        assert "KORD" in data
        assert "KMIA" in data
        assert data["KORD"]["trade_count"] == 2
        assert data["KMIA"]["trade_count"] == 1

    def test_station_stats_structure(self, tmp_path):
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 5.0, "station": "KATL"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        station = resp.json()["KATL"]
        for key in ("trade_count", "filled_count", "win_rate", "total_pnl"):
            assert key in station

    def test_win_rate_calculated_per_station(self, tmp_path):
        # 3 wins, 1 loss → win rate = 0.75
        records = [
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": 1.0, "station": "KORD"},
            {"ts": "2024-01-01T00:00:00+00:00", "outcome": "filled", "pnl": -1.0, "station": "KORD"},
        ]
        _write_jsonl(tmp_path / "trades.jsonl", records)
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "trades.jsonl"):
            resp = client.get("/stations")
        assert resp.json()["KORD"]["win_rate"] == pytest.approx(0.75)

    def test_empty_log_returns_empty_dict(self, tmp_path):
        with patch("src.monitoring.dashboard.LIVE_TRADES_JSONL", tmp_path / "missing.jsonl"):
            resp = client.get("/stations")
        assert resp.json() == {}


# ---------------------------------------------------------------------------
# _read_jsonl helper
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
