"""Unit tests for live_state.json persistence (Issue 1 — Epic 4)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.execution.live_trader import persist_state


class TestPersistState:
    def test_creates_file(self, tmp_path):
        state_file = tmp_path / "live_state.json"
        with patch("src.execution.live_trader.STATE_PATH", state_file):
            persist_state([])
        assert state_file.exists()

    def test_valid_json(self, tmp_path):
        state_file = tmp_path / "live_state.json"
        with patch("src.execution.live_trader.STATE_PATH", state_file):
            persist_state([])
        data = json.loads(state_file.read_text())
        assert "updated_at" in data
        assert "open_trades" in data

    def test_empty_trades_writes_empty_list(self, tmp_path):
        state_file = tmp_path / "live_state.json"
        with patch("src.execution.live_trader.STATE_PATH", state_file):
            persist_state([])
        data = json.loads(state_file.read_text())
        assert data["open_trades"] == []

    def test_writes_trade_records(self, tmp_path):
        state_file = tmp_path / "live_state.json"
        trades = [
            {
                "order_id": "abc123",
                "token_id": "0xabc",
                "station": "KLAX",
                "side": "YES",
                "bracket_low": 72.0,
                "bracket_high": 78.0,
                "entry_price": 64,
                "predicted_price": 68,
                "predicted_edge": 4.0,
                "size_usdc": 5.0,
                "placed_at": "2026-05-23T14:55:00Z",
            }
        ]
        with patch("src.execution.live_trader.STATE_PATH", state_file):
            persist_state(trades)
        data = json.loads(state_file.read_text())
        assert len(data["open_trades"]) == 1
        trade = data["open_trades"][0]
        assert trade["order_id"] == "abc123"
        assert trade["station"] == "KLAX"
        assert trade["entry_price"] == 64
        assert trade["predicted_edge"] == 4.0

    def test_atomic_write_no_partial_reads(self, tmp_path):
        """Verifies tmp-then-rename: the file is always complete JSON."""
        state_file = tmp_path / "live_state.json"
        with patch("src.execution.live_trader.STATE_PATH", state_file):
            persist_state([{"order_id": "first"}])
            first = json.loads(state_file.read_text())
            persist_state([{"order_id": "second"}])
            second = json.loads(state_file.read_text())
        assert first["open_trades"][0]["order_id"] == "first"
        assert second["open_trades"][0]["order_id"] == "second"

    def test_overwrites_previous_state(self, tmp_path):
        state_file = tmp_path / "live_state.json"
        with patch("src.execution.live_trader.STATE_PATH", state_file):
            persist_state([{"order_id": "old"}])
            persist_state([{"order_id": "new"}])
        data = json.loads(state_file.read_text())
        assert len(data["open_trades"]) == 1
        assert data["open_trades"][0]["order_id"] == "new"

    def test_updated_at_is_iso_string(self, tmp_path):
        state_file = tmp_path / "live_state.json"
        with patch("src.execution.live_trader.STATE_PATH", state_file):
            persist_state([])
        data = json.loads(state_file.read_text())
        assert data["updated_at"].endswith("Z")
