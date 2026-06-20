"""Unit + integration tests for _db_open_positions_enrichment() (Issue #369).

Covers:
- Happy path: DB returns rows → enrichment dict keyed by token_id.
- _db is None → returns empty dict without error.
- Token not in open_positions but in live_trades.jsonl → JSONL fallback used.
- Integration: real DB + wallet fixture; KATL/OEJN/EFHK positions show station + bracket.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.dashboard.api as dash_api
from src.dashboard.api import _db_open_positions_enrichment


# ---------------------------------------------------------------------------
# Unit: _db_open_positions_enrichment
# ---------------------------------------------------------------------------

class TestDbOpenPositionsEnrichment:
    def test_returns_dict_keyed_by_token_id(self):
        """Mock DB returns one row → enrichment dict contains the token."""
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [
            {
                "token_id": "0xABC",
                "station": "KATL",
                "bracket_low": 80.0,
                "bracket_high": 84.0,
                "predicted_price": 75,
            }
        ]
        with patch.object(dash_api, "_db", mock_db):
            result = _db_open_positions_enrichment()

        assert "0xABC" in result
        assert result["0xABC"]["station"] == "KATL"
        assert result["0xABC"]["bracket_low"] == 80.0
        assert result["0xABC"]["bracket_high"] == 84.0
        assert result["0xABC"]["predicted_price"] == 75

    def test_multiple_rows_all_returned(self):
        """All rows from DB are present in the enrichment dict."""
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [
            {"token_id": "0x1", "station": "OEJN", "bracket_low": 40.0, "bracket_high": 44.0, "predicted_price": 60},
            {"token_id": "0x2", "station": "EFHK", "bracket_low": 20.0, "bracket_high": 24.0, "predicted_price": 55},
        ]
        with patch.object(dash_api, "_db", mock_db):
            result = _db_open_positions_enrichment()

        assert len(result) == 2
        assert result["0x1"]["station"] == "OEJN"
        assert result["0x2"]["station"] == "EFHK"

    def test_db_none_returns_empty_dict(self):
        """When _db is None, function returns {} without error (falls back to JSONL)."""
        with patch.object(dash_api, "_db", None):
            result = _db_open_positions_enrichment()

        assert result == {}

    def test_row_without_token_id_excluded(self):
        """Rows with no token_id are skipped."""
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [
            {"token_id": None, "station": "KATL", "bracket_low": 80.0, "bracket_high": 84.0, "predicted_price": 75},
            {"token_id": "", "station": "OEJN", "bracket_low": 40.0, "bracket_high": 44.0, "predicted_price": 60},
            {"token_id": "0xGOOD", "station": "EFHK", "bracket_low": 20.0, "bracket_high": 24.0, "predicted_price": 55},
        ]
        with patch.object(dash_api, "_db", mock_db):
            result = _db_open_positions_enrichment()

        assert list(result.keys()) == ["0xGOOD"]

    def test_db_query_failure_returns_empty_dict(self):
        """If get_open_positions() raises, function returns {} without propagating."""
        mock_db = MagicMock()
        mock_db.get_open_positions.side_effect = Exception("DB error")
        with patch.object(dash_api, "_db", mock_db):
            result = _db_open_positions_enrichment()

        assert result == {}


# ---------------------------------------------------------------------------
# Unit: JSONL fallback when token not in open_positions
# ---------------------------------------------------------------------------

class TestJsonlFallback:
    def test_token_not_in_db_is_absent_from_enrichment(self):
        """Token not in open_positions is absent from DB enrichment (falls back to JSONL).

        This verifies that _db_open_positions_enrichment() only covers currently
        open positions. The _cached_live_trades() path in _positions_from_wallet()
        then picks up the token from live_trades.jsonl as the second-tier fallback.
        """
        mock_db = MagicMock()
        # DB has no record for 0xMISSING (token was settled and removed)
        mock_db.get_open_positions.return_value = [
            {
                "token_id": "0xOTHER",
                "station": "KATL",
                "bracket_low": 80.0,
                "bracket_high": 84.0,
                "predicted_price": 75,
            }
        ]
        with patch.object(dash_api, "_db", mock_db):
            result = _db_open_positions_enrichment()

        assert "0xMISSING" not in result
        assert "0xOTHER" in result

    def test_cached_live_trades_returns_dict_with_station(self, tmp_path):
        """_cached_live_trades() returns enrichment dict keyed by token_id with station."""
        import src.dashboard.data as _data
        from src.dashboard.api import _cached_live_trades

        jsonl = tmp_path / "live_trades.jsonl"
        # _parse_live_trades keyed by asset_id/no_token_id and requires outcome="filled"
        jsonl.write_text(json.dumps({
            "no_token_id": "0xMISSING",
            "asset_id": "0xMISSING",
            "outcome": "filled",
            "station": "KLAX",
            "bracket_low": 72.0,
            "bracket_high": 78.0,
            "predicted_price": 68,
            "side": "NO",
            "price_cents": 64,
            "shares": 10.0,
            "size_eur": 6.4,
        }) + "\n")

        with patch("src.dashboard.api.LIVE_TRADES_JSONL", jsonl):
            with patch("src.dashboard.api.SNAPSHOTS_JSONL", jsonl.parent / "snap.jsonl"):
                # Clear cache so new path is used
                with _data._cache_lock:
                    _data._cache.clear()
                jsonl_enrichment, _, _ = _cached_live_trades()

        # Enrichment dict is keyed by asset_id/no_token_id (per _parse_live_trades)
        assert "0xMISSING" in jsonl_enrichment
        assert jsonl_enrichment["0xMISSING"].get("station") == "KLAX"
        assert jsonl_enrichment["0xMISSING"].get("bracket_low") == 72.0


# ---------------------------------------------------------------------------
# Integration: real DB + wallet fixture
# ---------------------------------------------------------------------------

class TestIntegrationRealDb:
    """Feed a real (in-memory) DB + wallet fixture and verify enrichment works."""

    def _make_db(self) -> "Database":  # noqa: F821
        """Create an in-memory Database with three open positions."""
        from src.data.db import Database

        db = Database(":memory:")
        # Insert minimal rows matching the actual schema:
        # trades: no token_id column — bracket_low/high/predicted_price live here
        # open_positions: token_id + trade_id FK
        db._conn.executescript("""
            INSERT INTO trades (id, ts, station, ticker, bracket_low, bracket_high, side,
                                predicted_price, actual_price, predicted_edge, mode,
                                order_id, capital_before)
            VALUES
                (1, '2026-06-10T10:00:00', 'KATL', 'KATL-80-84', 80.0, 84.0, 'YES', 75, 75, 5.0, 'live', 'ord1', 1000.0),
                (2, '2026-06-10T11:00:00', 'OEJN', 'OEJN-40-44', 40.0, 44.0, 'NO',  60, 60, 4.0, 'live', 'ord2', 1000.0),
                (3, '2026-06-10T12:00:00', 'EFHK', 'EFHK-20-24', 20.0, 24.0, 'YES', 55, 55, 3.0, 'live', 'ord3', 1000.0);

            INSERT INTO open_positions (id, trade_id, station, ticker, token_id, side,
                                        order_id, entry_price, shares, entry_ts)
            VALUES
                (1, 1, 'KATL', 'KATL-80-84', '0xKATL', 'YES', 'ord1', 75, 10, '2026-06-10T10:00:00'),
                (2, 2, 'OEJN', 'OEJN-40-44', '0xOEJN', 'NO',  'ord2', 60, 5,  '2026-06-10T11:00:00'),
                (3, 3, 'EFHK', 'EFHK-20-24', '0xEFHK', 'YES', 'ord3', 55, 8,  '2026-06-10T12:00:00');
        """)
        db._conn.commit()
        return db

    def test_katl_oejn_efhk_have_station_and_bracket(self):
        """KATL, OEJN, EFHK positions show station + bracket via DB enrichment."""
        real_db = self._make_db()
        with patch.object(dash_api, "_db", real_db):
            result = _db_open_positions_enrichment()

        assert result["0xKATL"]["station"] == "KATL"
        assert result["0xKATL"]["bracket_low"] == 80.0
        assert result["0xKATL"]["bracket_high"] == 84.0

        assert result["0xOEJN"]["station"] == "OEJN"
        assert result["0xOEJN"]["bracket_low"] == 40.0

        assert result["0xEFHK"]["station"] == "EFHK"
        assert result["0xEFHK"]["bracket_high"] == 24.0

    def test_no_jsonl_required_for_enrichment(self):
        """Enrichment works without any live_trades.jsonl present."""
        real_db = self._make_db()
        missing_jsonl = Path("/tmp/nonexistent_live_trades.jsonl")
        with patch.object(dash_api, "_db", real_db):
            with patch("src.config.LIVE_TRADES_JSONL", missing_jsonl):
                result = _db_open_positions_enrichment()

        # All three positions enriched from DB despite no JSONL
        assert len(result) == 3
        assert all(result[tid]["station"] for tid in ("0xKATL", "0xOEJN", "0xEFHK"))
