"""Unit tests for src/execution/order_manager.py.

Covers:
- OrderManager.reconcile_timeout_fills(): patches timeout->filled for held tokens,
  leaves non-held records alone, skips missing file / empty wallet.
- OrderManager.check_take_profit_exits(): sells when best_bid >= target, skips
  when below target, handles 404 resolved markets, handles balance=0 error.
- OrderManager.sync_open_orders(): open exchange orders are added to dedup guard;
  filled positions from DB or JSONL are added; expired / missing records are removed.
"""
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

        # Use patch.object with the already-imported module to avoid lazy-import
        # name-binding issues in Python 3.10.
        with patch("src.data.polymarket.get_orderbook",
                   return_value={"bids": [{"price": "0.95"}]}), \
             patch.object(src.scripts.run, "_record_sell_in_db"), \
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]):
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        mock_ob.assert_not_called()
        trader.sell_position.assert_not_called()

    def test_no_positions_is_noop(self):
        """Empty position list → nothing happens."""
        trader = self._make_trader()

        with patch("src.scripts.run._load_open_no_positions", return_value=[]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        trader.sell_position.assert_not_called()

    def test_position_without_predicted_price_is_skipped(self):
        """Fills missing predicted_price are skipped safely."""
        token = "tok-tp-4"
        fill = _make_fill(token)
        fill["predicted_price"] = None
        trader = self._make_trader()

        with patch("src.scripts.run._load_open_no_positions", return_value=[fill]), \
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]):
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]):
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]):
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]), \
             patch("src.scripts.run._record_sell_in_db") as mock_record, \
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]), \
             patch("src.scripts.run._record_sell_in_db"), \
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]), \
             patch("src.scripts.run._record_sell_in_db"), \
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
             patch("src.scripts.run._load_open_no_positions", return_value=fills), \
             patch("src.scripts.run._record_sell_in_db"), \
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]):
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]), \
             patch("src.scripts.run._record_sell_in_db"), \
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
             patch("src.scripts.run._load_open_no_positions", return_value=[fill]):
            self.om.check_take_profit_exits(trader, "ts-tp", db=None)

        assert token not in self.om._sold_positions
