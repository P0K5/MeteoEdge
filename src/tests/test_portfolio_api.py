"""Unit tests for src/dashboard/api.py (Issue 2 — Epic 4).

Updated in Issue #369: removed live_state.json / STATE_PATH references;
enrichment is now sourced from the DB open_positions table.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


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
    def test_returns_200_with_no_db(self):
        """Portfolio endpoint returns 200 even when _db is None (no enrichment)."""
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.status_code == 200

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_empty_open_positions_when_no_db(self):
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.json()["open_positions"] == []

    def test_returns_expected_keys(self):
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=42.0):
                with patch.object(api, "_midpoint_cents", return_value=65):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        data = resp.json()
        for key in ("cash_usdc", "open_positions", "updated_at"):
            assert key in data, f"Missing key: {key}"

    def test_cash_usdc_returned(self):
        from src.dashboard import api
        with patch.object(api, "_db", None):
            with patch.object(api, "_cash_usdc", return_value=99.5):
                client = TestClient(api.app)
                resp = client.get("/api/portfolio")
        assert resp.json()["cash_usdc"] == pytest.approx(99.5)

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_position_fields_present(self):
        from src.dashboard import api
        mock_ob = {
            "bids": [{"price": "0.63", "size": "100"}],
            "asks": [{"price": "0.67", "size": "100"}],
        }
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0xabc",
            "station": "KLAX",
            "bracket_low": 72.0,
            "bracket_high": 78.0,
            "predicted_price": 68,
        }]
        with patch.object(api, "_db", mock_db):
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

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_market_prob_uses_clob_midpoint(self):
        from src.dashboard import api
        mock_ob = {
            "bids": [{"price": "0.62", "size": "100"}],
            "asks": [{"price": "0.66", "size": "100"}],
        }
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0xabc",
            "station": "KLAX",
            "bracket_low": 72.0,
            "bracket_high": 78.0,
            "predicted_price": 68,
        }]
        with patch.object(api, "_db", mock_db):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook", return_value=mock_ob):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        pos = resp.json()["open_positions"][0]
        assert pos["market_prob"] == 64

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_orderbook_failure_falls_back_to_entry_price(self):
        from src.dashboard import api
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0xabc",
            "station": "KLAX",
            "bracket_low": 72.0,
            "bracket_high": 78.0,
            "predicted_price": 68,
        }]
        with patch.object(api, "_db", mock_db):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook",
                           side_effect=RuntimeError("fetch failed")):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        pos = resp.json()["open_positions"][0]
        assert pos["market_prob"] == pos["entry_price"]

    @pytest.mark.skip(reason="pre-existing: issue #174 — Dashboard state isolation")
    def test_single_orderbook_failure_does_not_break_endpoint(self):
        """Even with a failing orderbook, /api/portfolio returns 200."""
        from src.dashboard import api
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [{
            "token_id": "0x1",
            "station": "KATL",
            "bracket_low": 80.0,
            "bracket_high": 84.0,
            "predicted_price": 75,
        }]
        with patch.object(api, "_db", mock_db):
            with patch.object(api, "_cash_usdc", return_value=0.0):
                with patch("src.dashboard.api.get_orderbook",
                           side_effect=RuntimeError("network error")):
                    client = TestClient(api.app)
                    resp = client.get("/api/portfolio")
        assert resp.status_code == 200
        assert len(resp.json()["open_positions"]) == 1
