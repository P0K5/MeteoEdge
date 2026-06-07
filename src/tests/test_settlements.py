"""Tests for SettlementWriter."""
import json
import pytest
from pathlib import Path
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


class TestBackfillScript:

    def test_backfill_skips_unsettled(self):
        """Backfill ignores records with pnl=0."""
        import sys
        import scripts.backfill_settlements as bfm

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create temp JSONL with 2 settled and 1 unsettled
            jsonl = Path(tmpdir) / "live_trades.jsonl"
            records = [
                {"ticker": "KORD-t1", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "NO", "pnl": 3.5, "actual_daily_high": 31.0, "actual_price": 95},
                {"ticker": "KMIA-t1", "station": "KMIA", "bracket_low": 80, "bracket_high": 84,
                 "side": "YES", "pnl": 0.0, "actual_daily_high": None},  # unsettled
                {"ticker": "KATL-t1", "station": "KATL", "bracket_low": 70, "bracket_high": 74,
                 "side": "NO", "pnl": -4.5, "actual_daily_high": 72.0, "actual_price": 5},
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

            # Should have 2 rows (KORD and KATL), not KMIA (pnl=0)
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
        """Backfill correctly computes resolved_yes from side and pnl."""
        import scripts.backfill_settlements as bfm

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl = Path(tmpdir) / "live_trades.jsonl"
            records = [
                # side=YES, pnl>0 -> resolved_yes=True
                {"ticker": "T1", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "YES", "pnl": 5.0, "actual_daily_high": 35.0, "actual_price": 90},
                # side=NO, pnl>0 -> resolved_yes=False
                {"ticker": "T2", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "NO", "pnl": 5.0, "actual_daily_high": 31.0, "actual_price": 90},
                # side=NO, pnl<0 -> resolved_yes=True
                {"ticker": "T3", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "NO", "pnl": -5.0, "actual_daily_high": 35.0, "actual_price": 10},
                # side=YES, pnl<0 -> resolved_yes=False
                {"ticker": "T4", "station": "KORD", "bracket_low": 32, "bracket_high": 36,
                 "side": "YES", "pnl": -5.0, "actual_daily_high": 31.0, "actual_price": 10},
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
            assert by_ticker["T1"]["resolved_yes"] == 1  # YES, pnl>0
            assert by_ticker["T2"]["resolved_yes"] == 0  # NO, pnl>0
            assert by_ticker["T3"]["resolved_yes"] == 1  # NO, pnl<0
            assert by_ticker["T4"]["resolved_yes"] == 0  # YES, pnl<0

    def test_backfill_skips_invalid_json(self):
        """Backfill skips malformed JSON lines."""
        import scripts.backfill_settlements as bfm

        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl = Path(tmpdir) / "live_trades.jsonl"
            lines = [
                '{"ticker": "T1", "station": "KORD", "bracket_low": 32, "bracket_high": 36, "side": "YES", "pnl": 5.0, "actual_daily_high": 35.0, "actual_price": 90}',
                "{ invalid json",
                '{"ticker": "T2", "station": "KORD", "bracket_low": 32, "bracket_high": 36, "side": "NO", "pnl": -5.0, "actual_daily_high": 35.0, "actual_price": 10}',
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
