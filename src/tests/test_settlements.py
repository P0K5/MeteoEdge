"""Tests for SettlementWriter and settlement writer integration."""
import json
import pytest
from datetime import date
from pathlib import Path
from unittest.mock import patch
import tempfile
from src.data.db import Database
from src.data.settlements import SettlementWriter


def _db():
    return Database(":memory:")


class TestSettlementWriter:

    def test_record_settlement_upsert(self):
        """Same ticker twice — only one row, last value wins."""
        db = _db()
        writer = SettlementWriter(db)
        common = dict(
            ticker="KORD-2024-01-15-HIGH-32-36",
            station="KORD",
            bracket_low=32.0,
            bracket_high=36.0,
            resolved_yes=True,
        )
        writer.record_settlement(**common, actual_high_f=34.5)
        writer.record_settlement(**common, actual_high_f=99.0)

        rows = db.get_settlements("KORD", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["actual_high_f"] == pytest.approx(99.0)

    def test_get_settlements_ascending_order(self):
        """get_settlements returns rows in ascending ts order."""
        db = _db()
        writer = SettlementWriter(db)
        # Insert out of order
        db.insert_settlement(
            ts="2024-06-01T20:00:00Z",
            station="KORD",
            ticker="KORD-t2",
            bracket_low=70.0, bracket_high=74.0,
            actual_high_f=72.0, resolved_yes=1,
        )
        db.insert_settlement(
            ts="2024-01-01T20:00:00Z",
            station="KORD",
            ticker="KORD-t1",
            bracket_low=32.0, bracket_high=36.0,
            actual_high_f=34.0, resolved_yes=1,
        )
        rows = db.get_settlements("KORD", since="2000-01-01")
        assert len(rows) == 2
        assert rows[0]["ts"] < rows[1]["ts"]

    def test_record_settlement_resolved_yes_false(self):
        """resolved_yes=False stores as 0."""
        db = _db()
        writer = SettlementWriter(db)
        writer.record_settlement(
            ticker="KMIA-t1",
            station="KMIA",
            bracket_low=80.0, bracket_high=84.0,
            actual_high_f=79.0,
            resolved_yes=False,
        )
        rows = db.get_settlements("KMIA", since="2000-01-01")
        assert rows[0]["resolved_yes"] == 0

    def test_record_settlement_with_market_final_price(self):
        """record_settlement accepts optional market_final_price."""
        db = _db()
        writer = SettlementWriter(db)
        writer.record_settlement(
            ticker="KATL-t1",
            station="KATL",
            bracket_low=70.0, bracket_high=74.0,
            actual_high_f=72.0,
            resolved_yes=True,
            market_final_price=85,
        )
        rows = db.get_settlements("KATL", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["market_final_price"] == 85

    def test_record_settlement_without_market_final_price(self):
        """record_settlement works without market_final_price."""
        db = _db()
        writer = SettlementWriter(db)
        writer.record_settlement(
            ticker="KSEA-t1",
            station="KSEA",
            bracket_low=60.0, bracket_high=64.0,
            actual_high_f=62.0,
            resolved_yes=True,
        )
        rows = db.get_settlements("KSEA", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["market_final_price"] is None


class TestWriteDbSettlements:
    """Tests for _write_db_settlements() — the settlement writer in settle.py."""

    def test_market_final_price_is_populated(self):
        """_write_db_settlements() calls fetch_market_final_price and stores the result."""
        from src.scripts.settle import _write_db_settlements

        db = _db()
        target = date(2024, 6, 15)
        truth = {"KORD": 78.2}
        records = [
            {
                "ticker": "0xdeadbeef1234",
                "station": "KORD",
                "bracket_low": 76.0,
                "bracket_high": 80.0,
                "end_date": "2024-06-15",
                "no_token_id": None,
            }
        ]

        with patch(
            "src.scripts.settle.fetch_market_final_price", return_value=97
        ) as mock_fetch:
            _write_db_settlements(records, target, truth, db)
            mock_fetch.assert_called_once_with("0xdeadbeef1234")

        rows = db.get_settlements("KORD", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["market_final_price"] == 97

    def test_market_final_price_none_when_api_fails(self):
        """_write_db_settlements() stores NULL when the Gamma API returns None."""
        from src.scripts.settle import _write_db_settlements

        db = _db()
        target = date(2024, 6, 15)
        truth = {"KORD": 78.2}
        records = [
            {
                "ticker": "0xdeadbeef5678",
                "station": "KORD",
                "bracket_low": 76.0,
                "bracket_high": 80.0,
                "end_date": "2024-06-15",
                "no_token_id": None,
            }
        ]

        with patch("src.scripts.settle.fetch_market_final_price", return_value=None):
            _write_db_settlements(records, target, truth, db)

        rows = db.get_settlements("KORD", since="2000-01-01")
        assert len(rows) == 1
        assert rows[0]["market_final_price"] is None

    def test_non_0x_ticker_skips_api_call(self):
        """_write_db_settlements() does not call Gamma for non-0x tickers."""
        from src.scripts.settle import _write_db_settlements

        db = _db()
        target = date(2024, 6, 15)
        truth = {"KORD": 78.2}
        records = [
            {
                "ticker": "KORD-order-abc123",
                "station": "KORD",
                "bracket_low": 76.0,
                "bracket_high": 80.0,
                "end_date": "2024-06-15",
                "no_token_id": "some-token-id",
            }
        ]

        with patch("src.scripts.settle.fetch_market_final_price") as mock_fetch:
            _write_db_settlements(records, target, truth, db)
            mock_fetch.assert_not_called()

    def test_skips_record_outside_target_date(self):
        """_write_db_settlements() ignores records with a different end_date."""
        from src.scripts.settle import _write_db_settlements

        db = _db()
        target = date(2024, 6, 15)
        truth = {"KORD": 78.2}
        records = [
            {
                "ticker": "0xdeadbeef9999",
                "station": "KORD",
                "bracket_low": 76.0,
                "bracket_high": 80.0,
                "end_date": "2024-06-14",  # wrong date
                "no_token_id": None,
            }
        ]

        with patch("src.scripts.settle.fetch_market_final_price", return_value=50):
            _write_db_settlements(records, target, truth, db)

        rows = db.get_settlements("KORD", since="2000-01-01")
        assert len(rows) == 0


class TestBackfillScript:

    def test_backfill_skips_unsettled(self):
        """Backfill ignores records without an actual_high (not yet settled)."""
        import scripts.backfill_settlements as bfm

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create temp JSONL with 2 settled and 1 unsettled
            jsonl = Path(tmpdir) / "live_trades.jsonl"
            records = [
                {"ticker": "0xkord1", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "NO", "pnl": 3.5, "actual_high": 31.0},
                {"ticker": "0xkmia1", "station": "KMIA", "bracket_low": 80, "bracket_high": 84,
                 "side": "YES", "actual_high": None},  # unsettled
                {"ticker": "0xkatl1", "station": "KATL", "bracket_low": 70, "bracket_high": 74,
                 "side": "NO", "pnl": -4.5, "actual_high": 72.0},
            ]
            with open(jsonl, "w") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")

            db = _db()
            # Patch LIVE_TRADES path and db in the module
            original_lt = bfm.LIVE_TRADES
            bfm.LIVE_TRADES = jsonl
            original_db_class = bfm.Database
            bfm.Database = lambda: db

            try:
                bfm.main()
            finally:
                bfm.LIVE_TRADES = original_lt
                bfm.Database = original_db_class

            # Should have 2 rows (KORD and KATL), not KMIA (no actual_high)
            all_rows = db.get_settlements("KORD", since="2000-01-01") + \
                       db.get_settlements("KATL", since="2000-01-01") + \
                       db.get_settlements("KMIA", since="2000-01-01")
            assert len(all_rows) == 2

    def test_backfill_missing_file(self):
        """Backfill gracefully handles missing live_trades.jsonl."""
        import scripts.backfill_settlements as bfm

        db = _db()
        original_lt = bfm.LIVE_TRADES
        bfm.LIVE_TRADES = Path("/nonexistent/path/live_trades.jsonl")
        original_db_class = bfm.Database
        bfm.Database = lambda: db

        try:
            bfm.main()  # Should not raise
        finally:
            bfm.LIVE_TRADES = original_lt
            bfm.Database = original_db_class

        # Verify no rows were inserted
        all_rows = db.get_settlements("KORD", since="2000-01-01")
        assert len(all_rows) == 0

    def test_backfill_resolved_yes_logic(self):
        """Backfill computes resolved_yes from the bracket and actual_high."""
        import scripts.backfill_settlements as bfm

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl = Path(tmpdir) / "live_trades.jsonl"
            records = [
                # actual inside bracket -> resolved_yes=True
                {"ticker": "0xt1", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "YES", "actual_high": 35.0},
                # actual below bracket -> resolved_yes=False
                {"ticker": "0xt2", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "NO", "actual_high": 31.0},
                # actual on bracket edge -> resolved_yes=True
                {"ticker": "0xt3", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "NO", "actual_high": 36.0},
                # actual above bracket -> resolved_yes=False
                {"ticker": "0xt4", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "YES", "actual_high": 37.0},
            ]
            with open(jsonl, "w") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")

            db = _db()
            original_lt = bfm.LIVE_TRADES
            bfm.LIVE_TRADES = jsonl
            original_db_class = bfm.Database
            bfm.Database = lambda: db

            try:
                bfm.main()
            finally:
                bfm.LIVE_TRADES = original_lt
                bfm.Database = original_db_class

            rows = db.get_settlements("KORD", since="2000-01-01")
            assert len(rows) == 4

            # Find by ticker and check resolved_yes
            by_ticker = {r["ticker"]: r for r in rows}
            assert by_ticker["0xt1"]["resolved_yes"] == 1  # 35.0 in [32, 36]
            assert by_ticker["0xt2"]["resolved_yes"] == 0  # 31.0 below
            assert by_ticker["0xt3"]["resolved_yes"] == 1  # 36.0 on edge
            assert by_ticker["0xt4"]["resolved_yes"] == 0  # 37.0 above

    def test_backfill_skips_invalid_json(self):
        """Backfill skips malformed JSON lines."""
        import scripts.backfill_settlements as bfm

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl = Path(tmpdir) / "live_trades.jsonl"
            lines = [
                '{"ticker": "0xt1", "station": "KORD", "bracket_low": 32, "bracket_high": 36, "side": "YES", "actual_high": 35.0}',
                "{ invalid json",
                '{"ticker": "0xt2", "station": "KORD", "bracket_low": 32, "bracket_high": 36, "side": "NO", "actual_high": 35.0}',
            ]
            with open(jsonl, "w") as f:
                for line in lines:
                    f.write(line + "\n")

            db = _db()
            original_lt = bfm.LIVE_TRADES
            bfm.LIVE_TRADES = jsonl
            original_db_class = bfm.Database
            bfm.Database = lambda: db

            try:
                bfm.main()
            finally:
                bfm.LIVE_TRADES = original_lt
                bfm.Database = original_db_class

            # Should have 2 rows (skipped the invalid JSON)
            all_rows = db.get_settlements("KORD", since="2000-01-01")
            assert len(all_rows) == 2
