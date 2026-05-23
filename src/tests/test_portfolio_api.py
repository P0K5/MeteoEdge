"""Unit tests for src/dashboard/api.py (Issue 2 — Epic 4)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _make_client(state_path: Path):
    """Return a TestClient with STATE_PATH patched to state_path."""
    with patch("src.dashboard.api.STATE_PATH", state_path):
        with patch("src.dashboard.api._cash_usdc", return_value=42.0):
            from src.dashboard import api
            # Re-import after patch to pick up new STATE_PATH in _read_state
            return TestClient(api.app)


@pytest.fixture()
def empty_state(tmp_path) -> Path:
    p = tmp_path / "live_state.json"
    p.write_text(json.dumps({"updated_at": "2026-05-23T15:00:00Z", "open_trades": []}))
    return p


@pytest.fixture()
def one_trade_state(tmp_path) -> Path:
    p = tmp_path / "live_state.json"
    p.write_text(json.dumps({
        "updated_at": "2026-05-23T15:00:00Z",
        "open_trades": [
            {
                "order_id": "abc123",
                "token_id": "0xabc",
                "station": "KLAX",
                "side": "NO",
                "bracket_low": 72.0,
                "bracket_high": 78.0,
                "entry_price": 64,
                "predicted_price": 68,
                "predicted_edge": 4.0,
                "size_usdc": 5.0,
                "placed_at": "2026-05-23T14:55:00Z",
            }
        ],
    }))
    return p


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_returns_200(self):
        from src.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_returns_status_ok(self):
        from src.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/health")
        assert resp.json()["status"] == "ok"

    def test_returns_ts_field(self):
        from src.dashboard.api import app
        client = TestClient(app)
        resp = client.get("/api/health")
        assert "ts" in resp.json()


# ---------------------------------------------------------------------------
# /api/portfolio
# ---------------------------------------------------------------------------

class TestPortfolioEndpoint:
    def test_returns_200_with_missing_state_file(self, tmp_path):
        missing = tmp_path / "no_state.json"
        from src.dashboard import api
        with patch.object(api, "STATE_PATH", missing):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.status_code == 200

    def test_empty_open_positions_when_no_state_file(self, tmp_path):
        missing = tmp_path / "no_state.json"
        from src.dashboard import api
        with patch.object(api, "STATE_PATH", missing):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.json()["open_positions"] == []

    def test_returns_expected_keys(self, empty_state):
        from src.dashboard import api
        with patch.object(api, "STATE_PATH", empty_state):
            with patch.object(api, "_cash_usdc", return_value=42.0):
                with patch.object(api, "_midpoint_cents", return_value=65):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        data = resp.json()
        for key in ("cash_usdc", "open_positions", "updated_at"):
            assert key in data, f"Missing key: {key}"

    def test_cash_usdc_returned(self, empty_state):
        from src.dashboard import api
        with patch.object(api, "STATE_PATH", empty_state):
            with patch.object(api, "_cash_usdc", return_value=99.5):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.json()["cash_usdc"] == pytest.approx(99.5)

    def test_position_fields_present(self, one_trade_state):
        from src.dashboard import api
        mock_ob = {
            "bids": [{"price": "0.63", "size": "100"}],
            "asks": [{"price": "0.67", "size": "100"}],
        }
        with patch.object(api, "STATE_PATH", one_trade_state):
            with patch.object(api, "_cash_usdc", return_value=10.0):
                with patch("src.dashboard.api.get_orderbook", return_value=mock_ob):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        positions = resp.json()["open_positions"]
        assert len(positions) == 1
        pos = positions[0]
        for field in ("station", "side", "bracket_low", "bracket_high",
                      "entry_price", "market_prob", "my_prob", "edge",
                      "shares", "invested", "current_value", "target_value"):
            assert field in pos, f"Missing field: {field}"

    def test_market_prob_uses_clob_midpoint(self, one_trade_state):
        from src.dashboard import api
        mock_ob = {
            "bids": [{"price": "0.62", "size": "100"}],
            "asks": [{"price": "0.66", "size": "100"}],
        }
        with patch.object(api, "STATE_PATH", one_trade_state):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook", return_value=mock_ob):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        pos = resp.json()["open_positions"][0]
        # midpoint = (0.62 + 0.66) / 2 * 100 = 64
        assert pos["market_prob"] == 64

    def test_orderbook_failure_falls_back_to_entry_price(self, one_trade_state):
        from src.dashboard import api
        with patch.object(api, "STATE_PATH", one_trade_state):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook",
                           side_effect=RuntimeError("fetch failed")):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        pos = resp.json()["open_positions"][0]
        assert pos["market_prob"] == pos["entry_price"]

    def test_single_orderbook_failure_does_not_break_endpoint(self, tmp_path):
        """Even with a failing orderbook, /api/portfolio returns 200."""
        state = tmp_path / "state.json"
        state.write_text(json.dumps({
            "updated_at": "2026-05-23T15:00:00Z",
            "open_trades": [
                {"order_id": "x1", "token_id": "0x1", "station": "KATL", "side": "YES",
                 "bracket_low": 80.0, "bracket_high": 84.0, "entry_price": 70,
                 "predicted_price": 75, "predicted_edge": 5.0, "size_usdc": 5.0,
                 "placed_at": "2026-05-23T12:00:00Z"},
            ],
        }))
        from src.dashboard import api
        with patch.object(api, "STATE_PATH", state):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook",
                           side_effect=RuntimeError("network error")):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        assert len(resp.json()["open_positions"]) == 1
