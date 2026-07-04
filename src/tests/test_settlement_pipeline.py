"""Tests for the DB settlement pipeline (issue #198).

Covers Database.update_trade_by_order / add_settled_pnl, settle.py's DB
write-back, and the scripts/repair_db.py one-time repair helpers.
"""
import importlib.util
import json
from datetime import date
from pathlib import Path

from src.data.db import Database
from src.scripts import settle

_REPAIR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "repair_db.py"
_spec = importlib.util.spec_from_file_location("repair_db", _REPAIR_PATH)
repair_db = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair_db)


def _db() -> Database:
    return Database(":memory:")


def _insert_trade(db: Database, order_id: str = "0xbuy1", **kw) -> int:
    defaults = dict(
        ts="2026-06-10T12:00:00+00:00",
        station="KSEA",
        ticker=f"KSEA-order-{order_id[:8]}",
        bracket_low=70.0,
        bracket_high=72.0,
        side="NO",
        predicted_price=80,
        actual_price=70,
        predicted_edge=10.0,
        mode="live",
        capital_before=5.0,
        order_id=order_id,
    )
    defaults.update(kw)
    return db.insert_trade(**defaults)


# ---------------------------------------------------------------------------
# Database.update_trade_by_order
# ---------------------------------------------------------------------------

class TestUpdateTradeByOrder:
    def test_updates_matching_row(self):
        db = _db()
        _insert_trade(db, order_id="0xbuy1")
        n = db.update_trade_by_order(
            "0xbuy1", outcome="sold", pnl=1.23, settled_at="2026-06-11"
        )
        assert n == 1
        row = db.get_trades(limit=1)[0]
        assert row["outcome"] == "sold"
        assert row["pnl"] == 1.23
        assert row["settled_at"] == "2026-06-11"

    def test_returns_zero_when_no_match(self):
        db = _db()
        _insert_trade(db, order_id="0xbuy1")
        assert db.update_trade_by_order("0xother", pnl=1.0) == 0

    def test_returns_zero_when_no_fields(self):
        db = _db()
        _insert_trade(db, order_id="0xbuy1")
        assert db.update_trade_by_order("0xbuy1") == 0

    def test_leaves_unspecified_fields_untouched(self):
        db = _db()
        _insert_trade(db, order_id="0xbuy1", outcome="filled")
        db.update_trade_by_order("0xbuy1", pnl=-5.0)
        row = db.get_trades(limit=1)[0]
        assert row["outcome"] == "filled"
        assert row["pnl"] == -5.0


# ---------------------------------------------------------------------------
# Database.add_settled_pnl
# ---------------------------------------------------------------------------

class TestAddSettledPnl:
    def test_creates_row_and_accumulates(self):
        db = _db()
        db.add_settled_pnl("2026-06-10", 2.5)
        db.add_settled_pnl("2026-06-10", -1.0)
        assert db.get_daily_pnl("2026-06-10") == 1.5

    def test_preserves_open_positions_counter(self):
        db = _db()
        db.upsert_daily_risk("2026-06-10", pnl_delta=0.0, open_positions=3)
        db.add_settled_pnl("2026-06-10", 2.0)
        cur = db._conn.execute(
            "SELECT open_positions FROM risk_state WHERE trade_date='2026-06-10'"
        )
        assert cur.fetchone()[0] == 3


# ---------------------------------------------------------------------------
# settle.settle_live_trades DB write-back
# ---------------------------------------------------------------------------

def _filled_record(**kw) -> dict:
    rec = {
        "ts": "2026-06-10T12:00:00+00:00",
        "order_id": "0xbuy1",
        "station": "KSEA",
        "end_date": "2026-06-10",
        "ticker": "0xmarkethash1",
        "no_token_id": "tok1",
        "bracket_low": 70.0,
        "bracket_high": 72.0,
        "side": "NO",
        "price_cents": 70,
        "size_eur": 5.0,
        "outcome": "filled",
    }
    rec.update(kw)
    return rec


class TestSettleLiveTradesDb:
    def _setup(self, tmp_path, monkeypatch, records):
        jsonl = tmp_path / "live_trades.jsonl"
        jsonl.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        monkeypatch.setattr(settle, "LIVE_TRADES_JSONL", jsonl)
        return jsonl

    def test_writes_pnl_settlement_and_risk(self, tmp_path, monkeypatch):
        db = _db()
        _insert_trade(db, order_id="0xbuy1", outcome="filled", end_date="2026-06-10")
        self._setup(tmp_path, monkeypatch, [_filled_record()])
        target = date(2026, 6, 10)
        truth = {"KSEA": 68.0}  # outside bracket -> NO wins

        settle.settle_live_trades(target, truth, db=db)

        row = db.get_trades(limit=1)[0]
        # shares = 5 / 0.70; pnl = 30c per share
        assert row["pnl"] == round(30 / 100 * (5.0 / 0.70), 4)
        assert row["settled_at"] is not None

        cur = db._conn.execute("SELECT * FROM settlements")
        s = cur.fetchall()
        assert len(s) == 1
        s = dict(s[0])
        assert s["ticker"] == "0xmarkethash1"
        assert s["actual_high_f"] == 68.0
        assert s["resolved_yes"] == 0

        assert db.get_daily_pnl("2026-06-10") == row["pnl"]

    def test_rerun_is_idempotent(self, tmp_path, monkeypatch):
        db = _db()
        _insert_trade(db, order_id="0xbuy1", outcome="filled", end_date="2026-06-10")
        self._setup(tmp_path, monkeypatch, [_filled_record()])
        target = date(2026, 6, 10)
        truth = {"KSEA": 68.0}

        settle.settle_live_trades(target, truth, db=db)
        first_pnl = db.get_daily_pnl("2026-06-10")
        settle.settle_live_trades(target, truth, db=db)

        assert db.get_daily_pnl("2026-06-10") == first_pnl
        cur = db._conn.execute("SELECT COUNT(*) FROM settlements")
        assert cur.fetchone()[0] == 1

    def test_synthetic_ticker_maps_to_market_hash(self, tmp_path, monkeypatch):
        db = _db()
        records = [
            _filled_record(order_id="0xbuy1", ticker="0xmarkethash1", no_token_id="tok1"),
            _filled_record(order_id="0xbuy2", ticker="KSEA-order-0xbuy2", no_token_id="tok1"),
        ]
        self._setup(tmp_path, monkeypatch, records)
        settle.settle_live_trades(date(2026, 6, 10), {"KSEA": 68.0}, db=db)

        cur = db._conn.execute("SELECT ticker FROM settlements")
        tickers = [r[0] for r in cur.fetchall()]
        assert tickers == ["0xmarkethash1"]

    def test_no_db_does_not_crash(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, [_filled_record()])
        settle.settle_live_trades(date(2026, 6, 10), {"KSEA": 68.0}, db=None)


# ---------------------------------------------------------------------------
# scripts/repair_db.py helpers
# ---------------------------------------------------------------------------

class TestRepairDb:
    def test_dedupe_trades_keeps_lowest_id_and_merges(self):
        db = _db()
        _insert_trade(db, order_id="0xbuy1", ticker="KSEA-order-0xbuy1")
        _insert_trade(db, order_id="0xbuy1", ticker="0xmarkethash1", outcome="filled")

        removed = repair_db.dedupe_trades(db._conn)

        assert removed == 1
        rows = db.get_trades(limit=None)
        assert len(rows) == 1
        assert rows[0]["ticker"] == "0xmarkethash1"
        assert rows[0]["outcome"] == "filled"

    def test_backfill_sold_via_ticker_prefix(self):
        db = _db()
        _insert_trade(db, order_id="0xbuy1abcdef")
        sold = {
            "order_id": "0xsell1",
            "ticker": "KSEA-order-0xbuy1ab",
            "no_token_id": "tok1",
            "station": "KSEA",
            "outcome": "sold",
            "pnl": 0.976,
            "ts": "2026-06-10T15:00:00+00:00",
        }
        n_filled, n_sold, n_timeout = repair_db.backfill_trade_pnl(db._conn, [sold])

        assert (n_filled, n_sold, n_timeout) == (0, 1, 0)
        row = db.get_trades(limit=1)[0]
        assert row["outcome"] == "sold"
        assert row["pnl"] == 0.976

    def test_rebuild_settlements_drops_garbage_rows(self):
        db = _db()
        db.insert_settlement(
            ts="2026-06-09", station="KSEA", ticker="garbage",
            bracket_low=70.0, bracket_high=72.0,
            actual_high_f=0.0, resolved_yes=0,
        )
        rec = _filled_record(actual_high=68.0, pnl=2.0, yes_won=False)
        deleted, written = repair_db.rebuild_settlements(db._conn, [rec])

        assert deleted == 1
        assert written == 1
        cur = db._conn.execute("SELECT ticker, actual_high_f FROM settlements")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "0xmarkethash1"
        assert rows[0][1] == 68.0

    def test_clean_open_positions_removes_settled(self):
        db = _db()
        t1 = _insert_trade(db, order_id="0xbuy1", pnl=2.0)
        t2 = _insert_trade(db, order_id="0xbuy2")
        for tid, oid, tok in [(t1, "0xbuy1", "tok1"), (t2, "0xbuy2", "tok2")]:
            db.open_position(
                trade_id=tid, station="KSEA", ticker="0xm", token_id=tok,
                side="NO", order_id=oid, entry_price=70, shares=7.14,
                entry_ts="2026-06-10T12:00:00+00:00",
            )
        deleted = repair_db.clean_open_positions(db._conn)

        assert deleted == 1
        remaining = db.get_open_positions()
        assert len(remaining) == 1
        assert remaining[0]["order_id"] == "0xbuy2"
