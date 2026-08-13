"""Regression tests for issue #993.

``OrderManager.reconcile_timeout_fills()`` used to patch a JSONL/``trades``
record from ``outcome='timeout'`` to ``outcome='filled'`` purely because its
``token_id`` was present in the wallet. That inference is unsound whenever a
#743 reprice-retry has run: the token is in the wallet because of the
*replacement* order, not the original, timed-out one.

Live sequence this reproduces (KORD, 2026-08-11, see #993):

    12:42:29  placed  0x6f780a  NO @ 74c
    12:47:30  timeout 0x6f780a          <- cleanly cancelled, never filled
    12:47:31  reprice-retry: re-placing NO at 75c
    12:47:31  placed  0x418b18  NO @ 75c
    12:48:01  filled  0x418b18          <- the only real fill, 6.67 shares

Before the fix, reconcile_timeout_fills() would see the token held in the
wallet (because of 0x418b18's fill) and falsely patch 0x6f780a's own
``timeout`` record to ``filled`` -- fabricating a second trade for an order
that never filled.

The fix (mirroring #977/#983's authority for ``open_positions``): the
per-order fill size from ``LiveTrader.get_order_fill_size`` now gates the
outcome patch itself, not just the ``open_positions`` insert.
"""
from __future__ import annotations

import json
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# py_clob_client_v2 is not installed in the test environment; stub it out
# before importing anything that touches it (mirrors test_order_manager.py).
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in (
    "AssetType", "BalanceAllowanceParams", "CreateOrderOptions",
    "OrderArgs", "OpenOrderParams", "OrderPayload",
):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

from src.data.db import Database  # noqa: E402
from src.execution.order_manager import OrderManager  # noqa: E402


def _db() -> Database:
    return Database(":memory:")


def _write_jsonl(path, records: list) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


class TestRepriceSupersededOrderNotPatchedToFilled:
    """(a) timed-out order + same-token reprice that fills: the timed-out
    order's ``trades`` row must stay 'timeout', and exactly one
    ``open_positions`` row must exist (for the reprice, not the original)."""

    def test_superseded_order_stays_timeout_single_open_position(self, tmp_path):
        om = OrderManager()
        db = _db()

        token_id = "tok-KORD-shared"
        original_order = "0x6f780a"
        reprice_order = "0x418b18"

        # The reprice's own placement flow already recorded its trade + open
        # position through the normal (non-reconcile) path -- this is what
        # run.py / LiveTrader do on a genuine fill, independent of
        # reconcile_timeout_fills().
        reprice_trade_id = db.insert_trade(
            ts="2026-08-11T12:47:31+00:00", station="KORD", ticker="0xrealcondition",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=75,
            actual_price=75, predicted_edge=10.0, mode="live", capital_before=5.0,
            order_id=reprice_order, outcome="filled",
        )
        db.open_position(
            trade_id=reprice_trade_id, station="KORD", ticker="0xrealcondition",
            token_id=token_id, side="NO", order_id=reprice_order,
            entry_price=75, shares=6.67, entry_ts="2026-08-11T12:48:01+00:00",
        )

        # The original order's own trade row, still timeout (as placed).
        db.insert_trade(
            ts="2026-08-11T12:42:29+00:00", station="KORD", ticker="0xrealcondition",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=74,
            actual_price=74, predicted_edge=10.0, mode="live", capital_before=5.0,
            order_id=original_order, outcome="timeout",
        )

        # Only the ORIGINAL order's JSONL record is still outcome='timeout' --
        # the reprice appended its own 'filled' record separately (not shown
        # here since reconcile_timeout_fills only rewrites 'timeout' records).
        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-08-11.jsonl"
        _write_jsonl(dated, [{
            "asset_id": token_id, "outcome": "timeout", "order_id": original_order,
            "price_cents": 74, "ts": "2026-08-11T12:42:29+00:00", "station": "KORD",
            "ticker": "0xrealcondition", "bracket_low": 32.0, "bracket_high": 36.0,
            "side": "NO", "predicted_price": 74, "edge_cents": 10.0, "size_eur": 5.0,
        }])

        live_trader = MagicMock()
        # The exchange confirms the ORIGINAL order matched nothing -- it was
        # cleanly cancelled and superseded by the reprice on the same token.
        live_trader.get_order_fill_size.return_value = 0.0

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={token_id}):
            om.reconcile_timeout_fills("ts-993", db=db, live_trader=live_trader)

        # The exchange authority was consulted for the superseded order.
        live_trader.get_order_fill_size.assert_called_once_with(original_order)

        # JSONL record for the original order is untouched.
        lines = [json.loads(ln) for ln in dated.read_text().splitlines() if ln.strip()]
        assert lines[0]["outcome"] == "timeout"
        assert "reconciled_at" not in lines[0]

        # DB trades row for the original order stays 'timeout'.
        original_row = db.get_trade_by_order_id(original_order)
        assert original_row["outcome"] == "timeout"

        # The reprice's own row is untouched.
        reprice_row = db.get_trade_by_order_id(reprice_order)
        assert reprice_row["outcome"] == "filled"

        # Exactly one open_positions row exists -- the reprice's, not a
        # phantom second one for the superseded original order.
        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["order_id"] == reprice_order
        assert positions[0]["shares"] == pytest.approx(6.67)


class TestGenuineLateFillStillPatched:
    """(b) a timed-out order that genuinely filled late (no reprice) is still
    correctly patched to 'filled' -- don't break the case reconciliation
    exists for."""

    def test_late_fill_no_reprice_patches_outcome_and_opens_position(self, tmp_path):
        om = OrderManager()
        db = _db()

        token_id = "tok-late-fill"
        order_id = "0xlatefill"

        db.insert_trade(
            ts="2026-08-11T09:00:00+00:00", station="KATL", ticker="0xanothercondition",
            bracket_low=80.0, bracket_high=82.0, side="NO", predicted_price=68,
            actual_price=68, predicted_edge=12.0, mode="live", capital_before=5.0,
            order_id=order_id, outcome="timeout",
        )

        base = tmp_path / "live_trades.jsonl"
        dated = tmp_path / "live_trades.2026-08-11.jsonl"
        _write_jsonl(dated, [{
            "asset_id": token_id, "outcome": "timeout", "order_id": order_id,
            "price_cents": 68, "ts": "2026-08-11T09:00:00+00:00", "station": "KATL",
            "ticker": "0xanothercondition", "bracket_low": 80.0, "bracket_high": 82.0,
            "side": "NO", "predicted_price": 68, "edge_cents": 12.0, "size_eur": 5.0,
        }])

        live_trader = MagicMock()
        # GTC order matched fully after our wait window expired -- a genuine
        # late fill, no reprice involved.
        live_trader.get_order_fill_size.return_value = 7.35

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids",
                   return_value={token_id}):
            om.reconcile_timeout_fills("ts-993b", db=db, live_trader=live_trader)

        live_trader.get_order_fill_size.assert_called_once_with(order_id)

        lines = [json.loads(ln) for ln in dated.read_text().splitlines() if ln.strip()]
        assert lines[0]["outcome"] == "filled"
        assert lines[0]["reconciled_at"] == "ts-993b"

        row = db.get_trade_by_order_id(order_id)
        assert row["outcome"] == "filled"

        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["order_id"] == order_id
        assert positions[0]["shares"] == pytest.approx(7.35)
