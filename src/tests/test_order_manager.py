"""Unit tests for src/execution/order_manager.py.

Covers:
- OrderManager.reconcile_timeout_fills(): patches timeout->filled for held tokens,
  leaves non-held records alone, skips missing file / empty wallet.
  Rotation-aware: iterates all rotated JSONL sources, skips .gz archives.
  DB sync: updates trades.outcome, inserts row if absent.
- OrderManager.check_take_profit_exits(): sells when best_bid >= target, skips
  when below target, handles 404 resolved markets, handles balance=0 error.
- OrderManager.sync_open_orders(): open exchange orders are added to dedup guard;
  filled positions from DB or JSONL are added; expired / missing records are removed.
"""
import gzip as _gzip
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out py_clob_client_v2 before any project imports.
# ---------------------------------------------------------------------------
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in (
    "AssetType",
    "BalanceAllowanceParams",
    "CreateOrderOptions",
    "OrderArgs",
    "OpenOrderParams",
    "OrderPayload",
):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

from src.execution.order_manager import OrderManager  # noqa: E402
import src.data.polymarket  # noqa: E402  -- pre-load so patch("src.data.polymarket.get_orderbook") is reliable
import src.scripts.run  # noqa: E402  -- pre-load so patch("src.scripts.run.*") is reliable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_om() -> OrderManager:
    return OrderManager()


def _write_jsonl(path: Path, records: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


# ===========================================================================
# reconcile_timeout_fills
# ===========================================================================

class TestReconcileTimeoutFills:
    """reconcile_timeout_fills rewrites 'timeout' -> 'filled' when the token is held."""

    def test_noop_when_file_missing(self, tmp_path):
        """No file — returns silently without raising."""
        om = _make_om()
        missing = tmp_path / "live_trades.jsonl"
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", missing), \
             patch("src.execution.order_manager._wallet_held_token_ids") as mock_wallet:
            om.reconcile_timeout_fills("ts-1")
        mock_wallet.assert_not_called()

    def test_noop_when_wallet_empty(self, tmp_path):
        """Wallet returns no tokens — file is left unchanged."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        record = {"asset_id": "tok-A", "outcome": "timeout"}
        _write_jsonl(f, [record])
        original = f.read_text()

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value=set()):
            om.reconcile_timeout_fills("ts-1")

        assert f.read_text() == original

    def test_patches_timeout_record_when_token_held(self, tmp_path):
        """timeout record for a held token is rewritten to filled."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        record = {"asset_id": "tok-B", "outcome": "timeout", "station": "KORD"}
        _write_jsonl(f, [record])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-B"}):
            om.reconcile_timeout_fills("ts-2026")

        patched = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        assert len(patched) == 1
        assert patched[0]["outcome"] == "filled"
        assert patched[0]["reconciled_at"] == "ts-2026"
        assert patched[0]["station"] == "KORD"

    def test_non_held_timeout_record_not_patched(self, tmp_path):
        """timeout record for token NOT in wallet is left alone."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        record = {"asset_id": "tok-C", "outcome": "timeout"}
        _write_jsonl(f, [record])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-other"}):
            om.reconcile_timeout_fills("ts-3")

        lines = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        assert lines[0]["outcome"] == "timeout"
        assert "reconciled_at" not in lines[0]

    def test_filled_outcome_for_held_token_not_overwritten(self, tmp_path):
        """'filled' outcome for a held token is NOT overwritten."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        records = [
            {"asset_id": "tok-D", "outcome": "filled"},
            {"asset_id": "tok-D", "outcome": "timeout"},
        ]
        _write_jsonl(f, records)

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-D"}):
            om.reconcile_timeout_fills("ts-4")

        lines = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        assert lines[0]["outcome"] == "filled"
        assert "reconciled_at" not in lines[0]
        assert lines[1]["outcome"] == "filled"
        assert lines[1]["reconciled_at"] == "ts-4"

    def test_uses_no_token_id_field_as_fallback(self, tmp_path):
        """Falls back to 'no_token_id' field when 'asset_id' is absent."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        record = {"no_token_id": "tok-E", "outcome": "timeout"}
        _write_jsonl(f, [record])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-E"}):
            om.reconcile_timeout_fills("ts-5")

        lines = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        assert lines[0]["outcome"] == "filled"

    def test_multiple_held_tokens_all_patched(self, tmp_path):
        """All timeout records for held tokens are patched in one pass."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        records = [
            {"asset_id": "tok-F", "outcome": "timeout"},
            {"asset_id": "tok-G", "outcome": "timeout"},
            {"asset_id": "tok-H", "outcome": "timeout"},
        ]
        _write_jsonl(f, records)

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-F", "tok-H"}):
            om.reconcile_timeout_fills("ts-6")

        lines = [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]
        outcomes = {ln["asset_id"]: ln["outcome"] for ln in lines}
        assert outcomes["tok-F"] == "filled"
        assert outcomes["tok-G"] == "timeout"  # not held
        assert outcomes["tok-H"] == "filled"

    def test_malformed_json_line_preserved(self, tmp_path):
        """Malformed JSON lines are passed through unchanged."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        f.write_text('NOT_JSON\n{"asset_id": "tok-J", "outcome": "timeout"}\n')

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-J"}):
            om.reconcile_timeout_fills("ts-8")

        lines = f.read_text().splitlines()
        assert lines[0] == "NOT_JSON"
        assert json.loads(lines[1])["outcome"] == "filled"

    def test_blank_lines_preserved(self, tmp_path):
        """Blank lines in the JSONL are preserved as-is (not counted as patches)."""
        om = _make_om()
        f = tmp_path / "live_trades.jsonl"
        f.write_text('{"asset_id": "tok-I", "outcome": "timeout"}\n\n')

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-I"}):
            om.reconcile_timeout_fills("ts-7")

        content = f.read_text()
        non_blank = [ln for ln in content.splitlines() if ln.strip()]
        assert len(non_blank) == 1
        assert json.loads(non_blank[0])["outcome"] == "filled"


# ===========================================================================
# reconcile_timeout_fills — rotation-aware and DB sync
# ===========================================================================

class TestReconcileRotationAware:
    """reconcile_timeout_fills must iterate all rotated JSONL sources correctly."""

    def test_only_dated_token_patched_when_plain_file_has_different_token(self, tmp_path):
        """Plain file has token-A (not in wallet); dated file has token-B (in wallet).
        Only the dated file record must be patched."""
        om = _make_om()
        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-06-20.jsonl"

        # Plain file: token-A (not in wallet)
        _write_jsonl(base, [{"asset_id": "tok-A", "outcome": "timeout"}])
        # Dated file: token-B (in wallet)
        _write_jsonl(dated, [{"asset_id": "tok-B", "outcome": "timeout"}])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={"tok-B"}):
            om.reconcile_timeout_fills("ts-rot-1")

        plain_lines = [json.loads(ln) for ln in base.read_text().splitlines() if ln.strip()]
        dated_lines = [json.loads(ln) for ln in dated.read_text().splitlines() if ln.strip()]

        assert plain_lines[0]["outcome"] == "timeout"   # not patched
        assert dated_lines[0]["outcome"] == "filled"    # patched
        assert dated_lines[0]["reconciled_at"] == "ts-rot-1"

    def test_gz_file_not_rewritten(self, tmp_path):
        """Records in .gz archives are visible for reads but the archive is never rewritten."""
        om = _make_om()
        base = tmp_path / "live_trades.jsonl"
        gz_path = tmp_path / "live_trades.2026-06-19.jsonl.gz"

        record = {"asset_id": "tok-gz", "outcome": "timeout"}
        with _gzip.open(gz_path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

        gz_mtime_before = gz_path.stat().st_mtime

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={"tok-gz"}):
            om.reconcile_timeout_fills("ts-rot-2")

        # .gz file must not have been modified
        assert gz_path.stat().st_mtime == gz_mtime_before
        # Content unchanged
        with _gzip.open(gz_path, "rt", encoding="utf-8") as fh:
            content2 = [json.loads(ln) for ln in fh if ln.strip()]
        assert content2[0]["outcome"] == "timeout"

    def test_multiple_dated_files_all_patched(self, tmp_path):
        """All dated JSONL files with matching tokens are patched in one reconcile pass."""
        om = _make_om()
        base = tmp_path / "live_trades.jsonl"
        dated1 = tmp_path / "live_trades.2026-06-18.jsonl"
        dated2 = tmp_path / "live_trades.2026-06-19.jsonl"
        dated3 = tmp_path / "live_trades.2026-06-20.jsonl"

        _write_jsonl(dated1, [{"asset_id": "tok-d1", "outcome": "timeout"}])
        _write_jsonl(dated2, [{"asset_id": "tok-d2", "outcome": "timeout"}])
        _write_jsonl(dated3, [{"asset_id": "tok-d3", "outcome": "timeout"}])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={"tok-d1", "tok-d3"}):
            om.reconcile_timeout_fills("ts-rot-3")

        d1 = [json.loads(ln) for ln in dated1.read_text().splitlines() if ln.strip()]
        d2 = [json.loads(ln) for ln in dated2.read_text().splitlines() if ln.strip()]
        d3 = [json.loads(ln) for ln in dated3.read_text().splitlines() if ln.strip()]

        assert d1[0]["outcome"] == "filled"
        assert d2[0]["outcome"] == "timeout"   # not in wallet
        assert d3[0]["outcome"] == "filled"

    def test_noop_when_no_sources_at_all(self, tmp_path):
        """No plain file, no dated files -> _wallet_held_token_ids never called."""
        om = _make_om()
        base = tmp_path / "live_trades.jsonl"  # does not exist

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids") as mock_w:
            om.reconcile_timeout_fills("ts-rot-4")

        mock_w.assert_not_called()

    def test_idempotent_second_run_produces_no_patches(self, tmp_path):
        """Running reconcile twice produces zero new patches on the second pass."""
        om = _make_om()
        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-06-20.jsonl"
        _write_jsonl(dated, [{"asset_id": "tok-idem", "outcome": "timeout"}])

        wallet = {"tok-idem"}
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value=wallet):
            om.reconcile_timeout_fills("ts-idem-1")

        # First run patched it. Now run again.
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value=wallet):
            om.reconcile_timeout_fills("ts-idem-2")

        lines = [json.loads(ln) for ln in dated.read_text().splitlines() if ln.strip()]
        assert lines[0]["outcome"] == "filled"
        assert lines[0]["reconciled_at"] == "ts-idem-1"  # unchanged on second pass


class TestReconcileDbSync:
    """reconcile_timeout_fills must keep the DB trades table in sync."""

    def _make_db(self):
        from src.data.db import Database
        return Database(":memory:")

    def _insert_trade(self, db, order_id="ord-001", outcome="timeout"):
        return db.insert_trade(
            ts="2026-06-20T10:00:00Z", station="KORD",
            ticker="KORD-80-82", bracket_low=80.0, bracket_high=82.0,
            side="NO", predicted_price=72, actual_price=72,
            predicted_edge=0.05, mode="live", capital_before=1000.0,
            order_id=order_id, outcome=outcome,
        )

    def test_db_row_outcome_updated_to_filled(self, tmp_path):
        """After reconcile, existing DB trades row has outcome='filled'."""
        om = _make_om()
        db = self._make_db()
        self._insert_trade(db, order_id="ord-db-1", outcome="timeout")

        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-06-20.jsonl"
        _write_jsonl(dated, [{"asset_id": "tok-db-1", "outcome": "timeout",
                               "order_id": "ord-db-1", "price_cents": 72}])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={"tok-db-1"}):
            om.reconcile_timeout_fills("ts-db-1", db=db)

        cur = db._conn.execute("SELECT outcome FROM trades WHERE order_id=?", ("ord-db-1",))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "filled"

    def test_db_row_inserted_when_absent(self, tmp_path):
        """When no DB row exists for the order_id, one is inserted with outcome='filled'."""
        om = _make_om()
        db = self._make_db()

        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-06-20.jsonl"
        record = {
            "asset_id": "tok-new", "outcome": "timeout",
            "order_id": "ord-new-1", "price_cents": 68,
            "ts": "2026-06-20T10:00:00Z", "station": "KORD",
            "ticker": "KORD-80-82", "bracket_low": 80.0, "bracket_high": 82.0,
            "side": "NO", "predicted_price": 68, "edge_cents": 0.05,
            "size_eur": 5.0,
        }
        _write_jsonl(dated, [record])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={"tok-new"}):
            om.reconcile_timeout_fills("ts-db-2", db=db)

        cur = db._conn.execute(
            "SELECT outcome, order_id FROM trades WHERE order_id=?", ("ord-new-1",)
        )
        row = cur.fetchone()
        assert row is not None, "Trade row should have been inserted"
        assert row[0] == "filled"

    def test_no_db_writes_on_second_run(self, tmp_path):
        """Idempotent: second reconcile triggers 0 new DB writes (outcome already filled)."""
        om = _make_om()
        db = self._make_db()
        self._insert_trade(db, order_id="ord-idem-db", outcome="timeout")

        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-06-20.jsonl"
        _write_jsonl(dated, [{"asset_id": "tok-idem-db", "outcome": "timeout",
                               "order_id": "ord-idem-db", "price_cents": 72}])

        wallet = {"tok-idem-db"}
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value=wallet):
            om.reconcile_timeout_fills("ts-idem-db-1", db=db)

        # Second run: JSONL now has outcome='filled', so no patch loop runs -> no DB write.
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value=wallet):
            om.reconcile_timeout_fills("ts-idem-db-2", db=db)

        cur = db._conn.execute("SELECT outcome FROM trades WHERE order_id=?", ("ord-idem-db",))
        assert cur.fetchone()[0] == "filled"

    def test_no_db_call_when_db_is_none(self, tmp_path):
        """Passing db=None must not raise -- JSONL is still patched."""
        om = _make_om()
        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-06-20.jsonl"
        _write_jsonl(dated, [{"asset_id": "tok-nodb", "outcome": "timeout",
                               "order_id": "ord-nodb"}])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={"tok-nodb"}):
            om.reconcile_timeout_fills("ts-nodb", db=None)

        lines = [json.loads(ln) for ln in dated.read_text().splitlines() if ln.strip()]
        assert lines[0]["outcome"] == "filled"


# ===========================================================================
# sync_open_orders
# ===========================================================================

class TestSyncOpenOrders:
    """sync_open_orders builds the dedup guard from exchange orders + filled positions."""

    def test_open_exchange_orders_added_to_guard(self):
        """Exchange open orders are added to _open_orders."""
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = [
            {"asset_id": "tok-open-1"},
            {"asset_id": "tok-open-2"},
        ]
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", Path("/nonexistent/path.jsonl")):
            om.sync_open_orders(trader)

        assert "tok-open-1" in om._open_orders
        assert "tok-open-2" in om._open_orders

    def test_db_positions_added_when_db_provided(self):
        """Filled positions from DB are added to the dedup guard."""
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = []
        db = MagicMock()
        db.get_open_positions.return_value = [
            {"no_token_id": "tok-db-1"},
            {"no_token_id": "tok-db-2"},
        ]
        om.sync_open_orders(trader, db=db)

        assert "tok-db-1" in om._open_orders
        assert "tok-db-2" in om._open_orders
        db.get_open_positions.assert_called_once()

    def test_jsonl_positions_used_when_no_db(self, tmp_path):
        """Today's filled positions from JSONL are added when no DB is provided."""
        today = datetime.now(timezone.utc).date().isoformat()
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = []

        f = tmp_path / "live_trades.jsonl"
        records = [
            {"no_token_id": "tok-jsonl-1", "end_date": today, "outcome": "filled"},
            {"no_token_id": "tok-jsonl-2", "end_date": today, "outcome": "filled"},
        ]
        _write_jsonl(f, records)

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            om.sync_open_orders(trader)

        assert "tok-jsonl-1" in om._open_orders
        assert "tok-jsonl-2" in om._open_orders

    def test_only_today_positions_from_jsonl(self, tmp_path):
        """JSONL positions from other dates are not added to the guard."""
        today = datetime.now(timezone.utc).date().isoformat()
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = []

        f = tmp_path / "live_trades.jsonl"
        records = [
            {"no_token_id": "tok-today", "end_date": today, "outcome": "filled"},
            {"no_token_id": "tok-yesterday", "end_date": yesterday, "outcome": "filled"},
        ]
        _write_jsonl(f, records)

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            om.sync_open_orders(trader)

        assert "tok-today" in om._open_orders
        assert "tok-yesterday" not in om._open_orders

    def test_non_filled_jsonl_records_excluded(self, tmp_path):
        """Only outcome='filled' records are added from JSONL — timeout/sold excluded."""
        today = datetime.now(timezone.utc).date().isoformat()
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = []

        f = tmp_path / "live_trades.jsonl"
        records = [
            {"no_token_id": "tok-filled", "end_date": today, "outcome": "filled"},
            {"no_token_id": "tok-timeout", "end_date": today, "outcome": "timeout"},
            {"no_token_id": "tok-sold", "end_date": today, "outcome": "sold"},
        ]
        _write_jsonl(f, records)

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            om.sync_open_orders(trader)

        assert "tok-filled" in om._open_orders
        assert "tok-timeout" not in om._open_orders
        assert "tok-sold" not in om._open_orders

    def test_open_orders_cleared_on_each_call(self):
        """_open_orders is reset on each sync call — stale entries do not persist."""
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = [{"asset_id": "tok-stale"}]

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", Path("/nonexistent.jsonl")):
            om.sync_open_orders(trader)

        assert "tok-stale" in om._open_orders

        # Second call with empty exchange response — stale token should be gone
        trader.client.get_open_orders.return_value = []
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", Path("/nonexistent.jsonl")):
            om.sync_open_orders(trader)

        assert "tok-stale" not in om._open_orders

    def test_exchange_error_falls_through_to_jsonl(self, tmp_path):
        """Exchange API failure is logged and sync continues with JSONL fallback."""
        today = datetime.now(timezone.utc).date().isoformat()
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.side_effect = Exception("network error")

        f = tmp_path / "live_trades.jsonl"
        _write_jsonl(f, [{"no_token_id": "tok-fallback", "end_date": today, "outcome": "filled"}])

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            om.sync_open_orders(trader)

        # JSONL fallback still works
        assert "tok-fallback" in om._open_orders

    def test_db_error_is_handled_gracefully(self):
        """DB read failure is caught; dedup guard still has exchange orders."""
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = [{"asset_id": "tok-exchange"}]
        db = MagicMock()
        db.get_open_positions.side_effect = Exception("db error")

        om.sync_open_orders(trader, db=db)

        assert "tok-exchange" in om._open_orders

    def test_jsonl_position_without_no_token_id_skipped(self, tmp_path):
        """Records missing the no_token_id field are not added to the guard."""
        today = datetime.now(timezone.utc).date().isoformat()
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = []

        f = tmp_path / "live_trades.jsonl"
        _write_jsonl(f, [{"end_date": today, "outcome": "filled"}])  # no no_token_id

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            om.sync_open_orders(trader)

        assert len(om._open_orders) == 0

    def test_db_positions_without_no_token_id_skipped(self):
        """DB positions missing no_token_id key are not added to the guard."""
        om = _make_om()
        trader = MagicMock()
        trader.client.get_open_orders.return_value = []
        db = MagicMock()
        db.get_open_positions.return_value = [{"station": "KORD"}]  # no no_token_id

        om.sync_open_orders(trader, db=db)

        assert len(om._open_orders) == 0


# ===========================================================================
# check_take_profit_exits
# ===========================================================================

def _make_fill(
    token_id: str,
    predicted_price: int = 95,
    price_cents: int = 80,
    size_eur: float = 5.0,
) -> dict:
    return {
        "no_token_id": token_id,
        "predicted_price": predicted_price,
        "price_cents": price_cents,
        "size_eur": size_eur,
        "station": "KORD",
        "bracket_low": 80.0,
        "bracket_high": 82.0,
        "question": "Will Chicago reach 80-82F?",
        "ticker": "KORD-test",
    }


class TestCheckTakeProfitExits:
    """check_take_profit_exits sells when best_bid >= target, skips otherwise."""

    @pytest.fixture(autouse=True)
    def _reset_om(self):
        """Fresh OrderManager per test (avoids _sold_positions leaking)."""
        self.om = _make_om()

    def _make_trader(self, sell_result=("sell-id-1", 93)):
        trader = MagicMock()
        trader.sell_position.return_value = sell_result
        return trader

    def test_sells_when_bid_reaches_target(self):
        """best_bid >= predicted_price - buffer → sell is placed and position recorded."""
        token = "tok-tp-1"
        fill = _make_fill(token, predicted_price=95)
        fill["side"] = "NO"  # required for DB-based position lookup
        # target = 95 - 2 = 93; bid 95 > 93 → should sell (bid clearly above target)
        trader = self._make_trader(sell_result=("sell-tp-1", 95))
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [fill]
        mock_db.close_positions_by_token.return_value = 1

        # Use patch with the module path to avoid lazy-import name-binding issues.
        # _record_sell_in_db lives in order_manager; _append_live_trade is in run.
        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.95"}]}), \
             patch("src.execution.order_manager._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade"):
            self.om.check_take_profit_exits(trader, "ts-tp", db=mock_db)

        trader.sell_position.assert_called_once()
        assert trader.sell_position.call_args[0][0] == token
        assert token in self.om._sold_positions
        mock_db.close_positions_by_token.assert_called_with(token)

    def test_no_sell_when_bid_below_target(self):
        """best_bid < target → position is not touched."""
        token = "tok-tp-2"
        fill = _make_fill(token, predicted_price=95)
        # target = 93; bid 90 → no sell
        trader = self._make_trader()

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.90"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_not_called()
        assert token not in self.om._sold_positions

    def test_already_sold_token_is_skipped(self):
        """Tokens in _sold_positions are skipped without fetching the orderbook."""
        token = "tok-tp-3"
        fill = _make_fill(token)
        self.om._sold_positions.add(token)
        trader = self._make_trader()

        with patch("src.data.polymarket.get_orderbook") as mock_ob, \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        mock_ob.assert_not_called()
        trader.sell_position.assert_not_called()

    def test_no_positions_is_noop(self):
        """Empty position list → nothing happens."""
        trader = self._make_trader()

        with patch("src.execution.order_manager._load_open_no_positions", return_value=[]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_not_called()

    def test_position_without_predicted_price_is_skipped(self):
        """Fills missing predicted_price are skipped safely."""
        token = "tok-tp-4"
        fill = _make_fill(token)
        fill["predicted_price"] = None
        trader = self._make_trader()

        with patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]), \
             patch("src.data.polymarket.get_orderbook"):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_not_called()

    def test_empty_bids_skips_position(self):
        """No bids in orderbook → position skipped, no sell."""
        token = "tok-tp-5"
        fill = _make_fill(token, predicted_price=95)
        trader = self._make_trader()

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": []}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_not_called()

    def test_404_market_resolved_closes_db_positions(self):
        """Orderbook returns 404 → market resolved, DB positions closed."""
        token = "tok-tp-6"
        fill = _make_fill(token, predicted_price=95)
        trader = self._make_trader()
        db = MagicMock()
        db.close_positions_by_token.return_value = 1

        with patch("src.data.polymarket.get_orderbook",
                   side_effect=Exception("404 not found")), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=db)

        db.close_positions_by_token.assert_called_once_with(token)
        trader.sell_position.assert_not_called()

    def test_balance_zero_error_marks_sold_and_cleans_db(self):
        """sell_position raises balance=0 error → token marked sold, DB cleaned."""
        token = "tok-tp-7"
        fill = _make_fill(token, predicted_price=95)
        trader = self._make_trader()
        trader.sell_position.side_effect = Exception("balance 0 not enough")
        db = MagicMock()
        db.close_positions_by_token.return_value = 1

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.93"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=db)

        assert token in self.om._sold_positions
        db.close_positions_by_token.assert_called_once_with(token)

    def test_sell_calls_record_and_append(self):
        """Successful sell calls _record_sell_in_db and _append_live_trade."""
        token = "tok-tp-8"
        fill = _make_fill(token, predicted_price=90, price_cents=75)
        # target = 90 - 2 = 88; bid 88 → should sell
        trader = self._make_trader(sell_result=("sell-tp-8", 88))
        db = MagicMock()

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.88"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]), \
             patch("src.execution.order_manager._record_sell_in_db") as mock_record,\
             patch("src.scripts.run._append_live_trade") as mock_append:
            self.om.check_take_profit_exits(trader, "ts-tp", db=db)

        mock_record.assert_called_once()
        mock_append.assert_called_once()
        db.close_positions_by_token.assert_called_once_with(token)

    def test_risk_manager_pnl_recorded_on_sell(self):
        """risk_manager.record_pnl is called after a successful take-profit sell."""
        token = "tok-tp-9"
        fill = _make_fill(token, predicted_price=95, price_cents=80)
        # target = 93; bid 93 → sell at 93c; entry 80c; shares = 5.0/0.80 = 6.25
        trader = self._make_trader(sell_result=("sell-tp-9", 93))
        risk = MagicMock()

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.93"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]), \
             patch("src.execution.order_manager._record_sell_in_db"), \
             patch("src.scripts.run._append_live_trade"):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None, risk_manager=risk)

        risk.record_pnl.assert_called_once()
        pnl_arg = risk.record_pnl.call_args[0][0]
        # PnL = (93-80)/100 * 6.25 = 0.8125
        assert abs(pnl_arg - 0.8125) < 0.01

    def test_trigger_format_starts_with_take_profit(self):
        """The trade trigger string must start with 'take_profit@'."""
        token = "tok-tp-10"
        fill = _make_fill(token, predicted_price=95, price_cents=80)
        trader = self._make_trader(sell_result=("sell-tp-10", 93))

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.93"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]), \
             patch("src.execution.order_manager._record_sell_in_db"), \
             patch("src.scripts.run._append_live_trade") as mock_append:
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trade_row = mock_append.call_args[0][0]
        assert trade_row["trigger"].startswith("take_profit@")

    def test_multiple_fills_for_same_token_aggregated(self):
        """Multiple fills (partial fills) for same token are aggregated before sell."""
        token = "tok-tp-12"
        fills = [
            _make_fill(token, predicted_price=95, price_cents=80, size_eur=2.5),
            _make_fill(token, predicted_price=95, price_cents=82, size_eur=2.5),
        ]
        # total_shares = 2.5/0.80 + 2.5/0.82 ≈ 3.125 + 3.048 = 6.173
        trader = self._make_trader(sell_result=("sell-tp-12", 93))

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.93"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=fills), \
             patch("src.execution.order_manager._record_sell_in_db"), \
             patch("src.scripts.run._append_live_trade"):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_called_once()
        call_args = trader.sell_position.call_args[0]
        assert call_args[0] == token
        total_shares = call_args[1]
        assert abs(total_shares - 6.173) < 0.01

    def test_generic_orderbook_error_skips_position(self):
        """Non-404 orderbook error → position skipped (not sold, not errored)."""
        token = "tok-tp-13"
        fill = _make_fill(token, predicted_price=95)
        trader = self._make_trader()

        with patch("src.data.polymarket.get_orderbook",
                   side_effect=Exception("connection timeout")), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_not_called()
        assert token not in self.om._sold_positions

    def test_bid_clamped_to_minimum_1_cent(self):
        """Extremely low bid price is clamped to 1c minimum."""
        token = "tok-tp-14"
        fill = _make_fill(token, predicted_price=2, price_cents=50)
        # target = 2 - 2 = 0; bid clamped to 1 → 1 >= 0 → sell
        trader = self._make_trader(sell_result=("sell-tp-14", 1))

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.001"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]), \
             patch("src.execution.order_manager._record_sell_in_db"), \
             patch("src.scripts.run._append_live_trade"):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_called_once()

    def test_generic_sell_exception_logs_warning_no_sold(self):
        """Generic sell exception is handled — token not marked sold (retry next poll)."""
        token = "tok-tp-15"
        fill = _make_fill(token, predicted_price=95)
        trader = self._make_trader()
        trader.sell_position.side_effect = Exception("unexpected exchange error")

        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.93"}]}), \
             patch("src.execution.order_manager._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        assert token not in self.om._sold_positions


# ===========================================================================
# manual_sell_position
# ===========================================================================

class TestManualSellPosition:
    """Operator-triggered immediate sell of an open position."""

    @pytest.fixture(autouse=True)
    def _reset_om(self):
        self.om = _make_om()

    def _make_trader(self, sell_result=("sell-id-1", 68)):
        trader = MagicMock()
        trader.sell_position_immediate.return_value = sell_result
        return trader

    def test_sells_full_size_and_records(self):
        """A fill closes the DB rows, marks the token sold, and records a manual trade."""
        token = "tok-m-1"
        fill = _make_fill(token, price_cents=80)
        fill["side"] = "NO"
        trader = self._make_trader(sell_result=("sell-m-1", 90))
        mock_db = MagicMock()
        mock_db.get_open_position_by_token.return_value = [fill]
        mock_db.close_positions_by_token.return_value = 1

        with patch("src.execution.order_manager._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade") as appended:
            result = self.om.manual_sell_position(trader, token, "ts-m", db=mock_db)

        assert result["status"] == "sold"
        assert result["order_id"] == "sell-m-1"
        assert result["sell_price_cents"] == 90
        trader.sell_position_immediate.assert_called_once()
        assert trader.sell_position_immediate.call_args[0][0] == token
        assert token in self.om._sold_positions
        mock_db.close_positions_by_token.assert_called_with(token)
        # PnL = (90 - 80)/100 * (5/0.80 shares) = 0.10 * 6.25 = 0.625
        assert result["pnl"] == pytest.approx(0.625)
        record = appended.call_args[0][0]
        assert record["outcome"] == "sold"
        assert record["trigger"].startswith("manual@")
        assert record["entry_side"] == "NO"

    def test_not_found_when_no_open_fills(self):
        """No open fills for the token → not_found, no sell attempted."""
        trader = self._make_trader()
        mock_db = MagicMock()
        mock_db.get_open_position_by_token.return_value = []

        result = self.om.manual_sell_position(trader, "missing", "ts-m", db=mock_db)

        assert result["status"] == "not_found"
        trader.sell_position_immediate.assert_not_called()

    def test_already_sold_token_is_skipped(self):
        """A token already in _sold_positions is not sold again."""
        token = "tok-m-2"
        self.om._sold_positions.add(token)
        trader = self._make_trader()

        result = self.om.manual_sell_position(trader, token, "ts-m", db=MagicMock())

        assert result["status"] == "already_sold"
        trader.sell_position_immediate.assert_not_called()

    def test_cross_process_not_found_when_db_row_gone(self):
        """Fresh process (_sold_positions empty) but the bot already closed the DB
        row: the DB lookup is the real cross-process guard, so we return not_found
        instead of attempting a duplicate sell."""
        token = "tok-m-5"
        assert token not in self.om._sold_positions  # fresh process
        trader = self._make_trader()
        mock_db = MagicMock()
        mock_db.get_open_position_by_token.return_value = []  # row already removed by the bot

        result = self.om.manual_sell_position(trader, token, "ts-m", db=mock_db)

        assert result["status"] == "not_found"
        trader.sell_position_immediate.assert_not_called()
        mock_db.close_positions_by_token.assert_not_called()

    def test_no_fill_records_partial_and_returns_no_fill(self):
        """An unmatched/cancelled order returns no_fill and tracks any partial fill."""
        token = "tok-m-3"
        fill = _make_fill(token)
        fill["side"] = "NO"
        trader = self._make_trader(sell_result=(None, "cancelled-order-7"))
        trader.get_order_fill_size.return_value = 2.0
        mock_db = MagicMock()
        mock_db.get_open_position_by_token.return_value = [fill]

        result = self.om.manual_sell_position(trader, token, "ts-m", db=mock_db)

        assert result["status"] == "no_fill"
        assert token not in self.om._sold_positions
        assert self.om._partial_fill_shares[token] == 2.0
        mock_db.close_positions_by_token.assert_not_called()

    def test_yes_position_carries_entry_side(self):
        """A YES holding records entry_side=YES so the closed list labels it correctly."""
        token = "tok-m-4"
        fill = _make_fill(token, price_cents=40)
        fill["side"] = "YES"
        trader = self._make_trader(sell_result=("sell-m-4", 55))
        mock_db = MagicMock()
        mock_db.get_open_position_by_token.return_value = [fill]

        with patch("src.execution.order_manager._record_sell_in_db"), \
             patch.object(src.scripts.run, "_append_live_trade") as appended:
            result = self.om.manual_sell_position(trader, token, "ts-m", db=mock_db)

        assert result["status"] == "sold"
        assert appended.call_args[0][0]["entry_side"] == "YES"


# ===========================================================================
# _load_open_fills_for_token
# ===========================================================================

class TestLoadOpenFillsForToken:
    """The token-fills loader used to size a manual sell."""

    def test_db_path_returns_matching_fills_any_side(self):
        from src.execution.order_manager import _load_open_fills_for_token
        mock_db = MagicMock()
        mock_db.get_open_position_by_token.return_value = [
            {"no_token_id": "tok-a", "side": "YES", "price_cents": 40, "size_eur": 5.0},
        ]
        fills = _load_open_fills_for_token("tok-a", "2026-06-14", db=mock_db)
        assert len(fills) == 1
        assert fills[0]["side"] == "YES"
        mock_db.get_open_position_by_token.assert_called_once_with("tok-a")

    def test_jsonl_fallback_returns_todays_filled(self, tmp_path):
        from src.execution.order_manager import _load_open_fills_for_token
        today = datetime.now(timezone.utc).date().isoformat()
        f = tmp_path / "lt.jsonl"
        _write_jsonl(f, [
            {"no_token_id": "tok-x", "end_date": today, "outcome": "filled",
             "price_cents": 80, "size_eur": 5.0},
        ])
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            fills = _load_open_fills_for_token("tok-x", today, db=None)
        assert len(fills) == 1

    def test_jsonl_fallback_excludes_prior_day_record(self, tmp_path):
        """A settled prior-day 'filled' record must not be returned as sellable today."""
        from src.execution.order_manager import _load_open_fills_for_token
        today = datetime.now(timezone.utc).date().isoformat()
        f = tmp_path / "lt.jsonl"
        _write_jsonl(f, [
            {"no_token_id": "tok-old", "end_date": "2020-01-01", "outcome": "filled",
             "price_cents": 80, "size_eur": 5.0},
        ])
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            fills = _load_open_fills_for_token("tok-old", today, db=None)
        assert fills == []

    def test_jsonl_fallback_excludes_sold_token(self, tmp_path):
        from src.execution.order_manager import _load_open_fills_for_token
        today = datetime.now(timezone.utc).date().isoformat()
        f = tmp_path / "lt.jsonl"
        _write_jsonl(f, [
            {"no_token_id": "tok-s", "end_date": today, "outcome": "filled",
             "price_cents": 80, "size_eur": 5.0},
            {"no_token_id": "tok-s", "end_date": today, "outcome": "sold",
             "price_cents": 90, "size_eur": 5.0},
        ])
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            fills = _load_open_fills_for_token("tok-s", today, db=None)
        assert fills == []


# ===========================================================================
# get_open_position_by_token
# ===========================================================================

class TestGetOpenPositionByToken:
    """Database method for targeted token_id lookup to avoid full table scan."""

    def test_get_open_position_by_token_returns_matching_row(self):
        """Happy path: returns rows for the specified token_id."""
        from src.data.db import Database
        db = Database(":memory:")
        # Insert a trade first
        trade_id = db.insert_trade(
            ts="2026-06-14T12:00:00Z", station="KORD",
            ticker="KORD-2026-06-14-HIGH-32-36",
            bracket_low=32.0, bracket_high=36.0,
            side="NO", predicted_price=70, actual_price=71,
            predicted_edge=0.08, mode="live", capital_before=1000.0,
        )
        # Insert open positions
        db.open_position(
            trade_id=trade_id, station="KORD",
            ticker="KORD-2026-06-14-HIGH-32-36",
            token_id="tok-match", side="NO",
            order_id="ord-001", entry_price=70,
            shares=7.0, entry_ts="2026-06-14T12:01:00Z",
        )
        db.open_position(
            trade_id=trade_id, station="KORD",
            ticker="KORD-2026-06-14-HIGH-32-36",
            token_id="tok-other", side="YES",
            order_id="ord-002", entry_price=30,
            shares=10.0, entry_ts="2026-06-14T12:02:00Z",
        )
        # Query for specific token
        results = db.get_open_position_by_token("tok-match")
        assert len(results) == 1
        assert results[0]["token_id"] == "tok-match"
        assert results[0]["side"] == "NO"
        assert results[0]["order_id"] == "ord-001"

    def test_get_open_position_by_token_returns_empty_list_on_no_match(self):
        """No-match case: returns empty list when token_id does not exist."""
        from src.data.db import Database
        db = Database(":memory:")
        # Insert a trade
        trade_id = db.insert_trade(
            ts="2026-06-14T12:00:00Z", station="KORD",
            ticker="KORD-2026-06-14-HIGH-32-36",
            bracket_low=32.0, bracket_high=36.0,
            side="NO", predicted_price=70, actual_price=71,
            predicted_edge=0.08, mode="live", capital_before=1000.0,
        )
        # Insert one position
        db.open_position(
            trade_id=trade_id, station="KORD",
            ticker="KORD-2026-06-14-HIGH-32-36",
            token_id="tok-exists", side="NO",
            order_id="ord-001", entry_price=70,
            shares=7.0, entry_ts="2026-06-14T12:01:00Z",
        )
        # Query for non-existent token
        results = db.get_open_position_by_token("tok-does-not-exist")
        assert results == []

    def test_get_open_position_by_token_includes_trade_join_fields(self):
        """Result includes bracket_low, bracket_high, predicted_price from trades join."""
        from src.data.db import Database
        db = Database(":memory:")
        trade_id = db.insert_trade(
            ts="2026-06-14T12:00:00Z", station="KORD",
            ticker="KORD-2026-06-14-HIGH-32-36",
            bracket_low=32.0, bracket_high=36.0,
            side="NO", predicted_price=70, actual_price=71,
            predicted_edge=0.08, mode="live", capital_before=1000.0,
        )
        db.open_position(
            trade_id=trade_id, station="KORD",
            ticker="KORD-2026-06-14-HIGH-32-36",
            token_id="tok-join-test", side="NO",
            order_id="ord-001", entry_price=70,
            shares=7.0, entry_ts="2026-06-14T12:01:00Z",
        )
        results = db.get_open_position_by_token("tok-join-test")
        assert len(results) == 1
        assert results[0]["bracket_low"] == 32.0
        assert results[0]["bracket_high"] == 36.0
        assert results[0]["predicted_price"] == 70


# ===========================================================================
# Partial Fill Persistence
# ===========================================================================

class TestPartialFillPersistence:
    """Persist partial fills on no_fill path to survive process restart."""

    @pytest.fixture(autouse=True)
    def _reset_om(self):
        self.om = _make_om()

    def _make_trader(self, sell_result=(None, "cancelled-1")):
        trader = MagicMock()
        trader.sell_position_immediate.return_value = sell_result
        return trader

    def test_partial_fill_persisted_to_jsonl_on_no_fill(self, tmp_path):
        """When order is no_fill with partial > 0, append partial_fill record to JSONL."""
        from src.execution.order_manager import _load_open_fills_for_token
        token = "tok-pf-1"
        fill = _make_fill(token, price_cents=80)
        fill["side"] = "NO"
        today = datetime.now(timezone.utc).date().isoformat()
        f = tmp_path / "lt.jsonl"
        f.write_text("")  # empty file
        trader = self._make_trader(sell_result=(None, "cancelled-order-1"))
        trader.get_order_fill_size.return_value = 2.5
        mock_db = MagicMock()
        mock_db.get_open_position_by_token.return_value = [fill]

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f), \
             patch.object(src.scripts.run, "_append_live_trade") as mock_append:
            result = self.om.manual_sell_position(trader, token, "ts-pf", db=mock_db)

        assert result["status"] == "no_fill"
        # Verify _append_live_trade was called with partial_fill outcome
        mock_append.assert_called_once()
        record = mock_append.call_args[0][0]
        assert record["outcome"] == "partial_fill"
        assert record["shares"] == 2.5
        assert record["no_token_id"] == token

    def test_partial_fills_reduce_next_retry_size(self, tmp_path):
        """_load_open_fills_for_token accounts for partial_fill records when sizing."""
        from src.execution.order_manager import _load_open_fills_for_token
        today = datetime.now(timezone.utc).date().isoformat()
        f = tmp_path / "lt.jsonl"
        # Original fill: 10 EUR at 80c = 12.5 shares
        # Partial sell: 5 shares already sold
        # Expected next retry: 7.5 shares = 6.0 EUR
        _write_jsonl(f, [
            {"no_token_id": "tok-pf", "end_date": today, "outcome": "filled",
             "price_cents": 80, "size_eur": 10.0},
            {"no_token_id": "tok-pf", "end_date": today, "outcome": "partial_fill",
             "shares": 5.0},
        ])
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            fills = _load_open_fills_for_token("tok-pf", today, db=None)

        assert len(fills) == 1
        # size_eur should be reduced: 10.0 * (1 - 5/12.5) = 10.0 * 0.6 = 6.0
        assert fills[0]["size_eur"] == pytest.approx(6.0, abs=0.01)

    def test_full_partial_fill_returns_empty(self, tmp_path):
        """When partial shares equal or exceed total shares, return empty list."""
        from src.execution.order_manager import _load_open_fills_for_token
        today = datetime.now(timezone.utc).date().isoformat()
        f = tmp_path / "lt.jsonl"
        # Fill: 10 EUR at 80c = 12.5 shares
        # Partial: 13 shares (more than total available)
        _write_jsonl(f, [
            {"no_token_id": "tok-full", "end_date": today, "outcome": "filled",
             "price_cents": 80, "size_eur": 10.0},
            {"no_token_id": "tok-full", "end_date": today, "outcome": "partial_fill",
             "shares": 13.0},
        ])
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            fills = _load_open_fills_for_token("tok-full", today, db=None)

        assert fills == []

    def test_multiple_partial_fills_accumulated(self, tmp_path):
        """Multiple partial_fill records accumulate before being subtracted from size."""
        from src.execution.order_manager import _load_open_fills_for_token
        today = datetime.now(timezone.utc).date().isoformat()
        f = tmp_path / "lt.jsonl"
        # Fill: 10 EUR at 100c = 10 shares
        # Partial 1: 3 shares
        # Partial 2: 2 shares
        # Expected remainder: 5 shares = 5 EUR
        _write_jsonl(f, [
            {"no_token_id": "tok-multi", "end_date": today, "outcome": "filled",
             "price_cents": 100, "size_eur": 10.0},
            {"no_token_id": "tok-multi", "end_date": today, "outcome": "partial_fill",
             "shares": 3.0},
            {"no_token_id": "tok-multi", "end_date": today, "outcome": "partial_fill",
             "shares": 2.0},
        ])
        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", f):
            fills = _load_open_fills_for_token("tok-multi", today, db=None)

        assert len(fills) == 1
        assert fills[0]["size_eur"] == pytest.approx(5.0, abs=0.01)
