"""Regression tests for issue #977.

Two defects on live KORD 2026-08-11:

1. Entry gate: a second order was placed onto an already-held token_id.
   Root cause: LiveTrader.place_order() wrote a synthetic
   ``<STATION>-order-<id>`` ticker into trades.ticker at placement time; the
   real ticker was only corrected later, at the end of _attempt(), via
   _append_live_trade()/update_trade_by_order(). If the process crashes
   between placement and that correction (e.g. a power outage mid-wait),
   trades.ticker stays synthetic forever and
   Database.has_live_trade_today()'s ticker-matched query can never find the
   row -- so a same-day re-entry on that bracket sails through unblocked
   once its open_positions row is also gone (e.g. deleted by a successful
   cancel_order() that completed before the crash).

2. Reconciliation: OrderManager._reconcile_db_row() computed the shares for
   a newly-recovered open_positions row from JSONL fields
   (``size_matched`` / ``shares``) that _append_live_trade() never writes --
   always producing shares=0.0, an unclearable phantom position.

Covers every acceptance criterion and test requirement listed on #977.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta, timezone
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
from src.execution.live_trader import LiveTrader  # noqa: E402
from src.execution.order_manager import OrderManager, _reconcile_db_row  # noqa: E402


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _db() -> Database:
    return Database(":memory:")


# ===========================================================================
# Defect 1a -- place_order() must write the real ticker, not a synthetic one
# ===========================================================================

class TestPlaceOrderRealTicker:
    def test_place_order_writes_real_ticker_when_provided(self):
        db = _db()
        trader = LiveTrader(MagicMock(), db)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-real-ticker"}

        trader.place_order(
            token_id="tok-real-ticker", side="NO", price_cents=70, size_usdc=5.0,
            station="KORD", ticker="0xrealcondition123", bracket_low=32.0, bracket_high=36.0,
        )

        trade = db.get_trade_by_order_id("ord-real-ticker")
        assert trade["ticker"] == "0xrealcondition123"
        position = db.get_open_position_by_token("tok-real-ticker")[0]
        assert position["ticker"] == "0xrealcondition123"

    def test_place_order_omitted_ticker_raises_instead_of_falling_back(self):
        """PR review on #977: the old synthetic '<STATION>-order-<id>' fallback
        must not survive -- it's exactly the mechanism that broke the entry gate.
        A caller that forgets ticker= must fail loudly (TypeError, no default),
        never silently reintroduce the placeholder."""
        db = _db()
        trader = LiveTrader(MagicMock(), db)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-no-ticker"}

        with pytest.raises(TypeError):
            trader.place_order(
                token_id="tok-no-ticker", side="NO", price_cents=70, size_usdc=5.0, station="KORD",
            )
        # The order must never have reached the exchange for a call missing ticker.
        trader.client.create_and_post_order.assert_not_called()
        assert db.get_open_positions() == []

    def test_place_order_empty_ticker_raises(self):
        db = _db()
        trader = LiveTrader(MagicMock(), db)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-empty-ticker"}

        with pytest.raises(ValueError, match="non-empty ticker"):
            trader.place_order(
                token_id="tok-empty-ticker", side="NO", price_cents=70, size_usdc=5.0,
                station="KORD", ticker="",
            )
        trader.client.create_and_post_order.assert_not_called()
        assert db.get_open_positions() == []


class TestHasLiveTradeTodaySurvivesCrashBeforeAppend:
    """The concrete #977 defect-1 scenario: an order is placed, its
    open_positions row is later cleared (e.g. by a successful cancel), and
    the process crashes before _append_live_trade() would have run -- so
    trades.ticker is whatever place_order() wrote at placement time.
    """

    def test_synthetic_ticker_defeats_has_live_trade_today(self):
        """Pre-#977 mechanism, reproduced directly against the DB (place_order()
        can no longer produce a synthetic ticker itself -- ticker is now a
        required arg, per PR review -- so this writes the rows the way the old,
        no-longer-reachable fallback used to): a synthetic ticker means the
        same-day guard can never find the row. This is the bug that let the
        second order through."""
        db = _db()
        synthetic_ticker = "KORD-order-ord-cras"  # f"{station}-order-{order_id[:8]}"
        trade_id = db.insert_trade(
            ts="2026-08-11T11:41:52Z", station="KORD", ticker=synthetic_ticker,
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
            actual_price=70, predicted_edge=16.0, mode="live", capital_before=5.0,
            order_id="ord-crash-1", end_date=_today(),
        )
        db.open_position(
            trade_id=trade_id, station="KORD", ticker=synthetic_ticker,
            token_id="tok-crash-1", side="NO", order_id="ord-crash-1",
            entry_price=70, shares=7.14, entry_ts="2026-08-11T11:41:52Z",
        )
        # Simulate a successful cancel (deletes the open_positions row) with
        # the crash happening before _append_live_trade() would have corrected
        # the ticker.
        db.close_position("ord-crash-1")

        assert db.get_open_position_by_token("tok-crash-1") == []
        assert db.has_live_trade_today("KORD", "0xrealcondition-crash-1", "NO", _today()) is False

    def test_real_ticker_at_placement_survives_the_same_crash(self):
        """#977 fix: with the real ticker passed at placement, the same crash
        scenario still leaves has_live_trade_today() able to find the row."""
        db = _db()
        trader = LiveTrader(MagicMock(), db)
        trader.client.create_and_post_order.return_value = {"orderID": "ord-crash-2"}
        trader.place_order(
            token_id="tok-crash-2", side="NO", price_cents=70, size_usdc=5.0,
            station="KORD", ticker="0xrealcondition-crash-2",
            bracket_low=32.0, bracket_high=36.0, end_date=_today(),
        )
        db.close_position("ord-crash-2")

        assert db.get_open_position_by_token("tok-crash-2") == []
        assert db.has_live_trade_today("KORD", "0xrealcondition-crash-2", "NO", _today()) is True


class TestEntryGateOpenPositionByToken:
    """Baseline acceptance criterion: an existing open_positions row always
    blocks a second entry on the same token_id (independent of the ticker
    bug -- this path was already correct)."""

    def test_open_position_on_token_blocks(self):
        db = _db()
        trade_id = db.insert_trade(
            ts=datetime.now(timezone.utc).isoformat(), station="KORD",
            ticker="0xheld", bracket_low=32.0, bracket_high=36.0, side="NO",
            predicted_price=70, actual_price=70, predicted_edge=16.0,
            mode="live", capital_before=5.0, order_id="ord-held",
        )
        db.open_position(
            trade_id=trade_id, station="KORD", ticker="0xheld", token_id="tok-held",
            side="NO", order_id="ord-held", entry_price=70, shares=7.0,
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )
        assert db.get_open_position_by_token("tok-held") != []


# ===========================================================================
# Defect 1b -- defense-in-depth: order_manager._open_orders also blocks
# ===========================================================================

class TestOpenOrdersDefenseInDepth:
    def test_token_in_open_orders_set_blocks_before_db_is_even_consulted(self):
        """run.py's entry gate must also refuse a candidate whose token is
        in order_manager._open_orders, even if the DB has no row for it yet
        (the exact #977 discovery-latency window)."""
        import contextlib
        import src.scripts.run as run_module
        from src.scripts.run import poll_once
        from src.model.envelope import Bracket
        from src.strategy.scanner import Candidate

        db = _db()
        bracket = Bracket(
            ticker="0xoos", low_f=98.0, high_f=99.0,
            yes_ask_cents=30, yes_ask_size=100, no_ask_cents=70, no_ask_size=100,
            yes_token_id="tok-oos-yes", no_token_id="tok-oos-no",
        )
        cand = Candidate(
            station="KORD", bracket=bracket, side="NO", edge_cents=16.0,
            price_cents=70, confidence=0.86, p_yes=0.14, ev_yes=-10.0, ev_no=16.0,
            minutes_to_settlement=300.0,
            market={"question": "KORD high temp", "endDate": f"{_today()}T23:59:00Z"},
            shadow=False,
        )

        run_module.order_manager._open_orders.add("tok-oos-no")
        try:
            live_trader = MagicMock()
            live_trader.get_usdc_balance.return_value = 100.0
            live_trader._client_factory = MagicMock()
            risk = MagicMock()
            risk.allow_trade.return_value = (True, "ok")
            risk._daily_pnl = 0.0
            calls: list = []

            with contextlib.ExitStack() as stack:
                for p in (
                    patch("src.scripts.run._build_weather", return_value={"KORD": MagicMock()}),
                    patch("src.scripts.run.build_weather_low_for_scanning", return_value={}),
                    patch("src.scripts.run.build_weather_for_pricing", return_value={}),
                    patch("src.scripts.run.get_weather_markets", return_value=[]),
                    patch("src.scripts.run.scan_markets", return_value=([cand], [])),
                    patch("src.scripts.run.fetch_orderbooks_batch", return_value={}),
                    patch("src.scripts.run._load_open_no_positions", return_value=[]),
                    patch("src.scripts.run._execute_live", side_effect=lambda *a, **k: calls.append(a)),
                    patch("src.scripts.run._maybe_run_emos_shadow"),
                    patch.object(run_module.order_manager, "reconcile_timeout_fills"),
                    patch.object(run_module.order_manager, "sync_open_orders"),
                    patch.object(run_module.order_manager, "check_take_profit_exits"),
                    patch("src.scripts.run._log_open_position_snapshots", return_value=[]),
                    patch("src.scripts.run._check_forced_exits"),
                    patch("src.scripts.run._check_stop_loss_exits"),
                    patch("src.scripts.run.FreshnessMonitor"),
                    patch("src.scripts.run.get_source_priority", return_value=[]),
                    patch("src.scripts.run._append_candidate"),
                    patch("src.scripts.run._append_snapshot"),
                    patch("src.monitoring.dashboard.last_poll_ts", None, create=True),
                ):
                    stack.enter_context(p)
                poll_once(risk, live_trader=live_trader, alert_manager=None, db=db)

            assert calls == [], "a token already in order_manager._open_orders must block placement"
        finally:
            run_module.order_manager._open_orders.discard("tok-oos-no")


# ===========================================================================
# Defect 2 -- reconciliation must never insert a zero-share phantom row
# ===========================================================================

class TestReconcileDbRowFillSize:
    def _record(self, **overrides) -> dict:
        record = {
            "order_id": "ord-recon-1", "asset_id": "tok-recon-1",
            "station": "KORD", "ticker": "0xreconticker", "side": "NO",
            "price_cents": 70, "ts": "2026-08-11T11:41:52+00:00",
            "bracket_low": 32.0, "bracket_high": 36.0,
        }
        record.update(overrides)
        return record

    def test_zero_fill_inserts_no_open_positions_row(self):
        db = _db()
        _reconcile_db_row(self._record(), "2026-08-11T12:53:01+00:00", db=db, get_fill_size=lambda oid: 0.0)
        assert db.get_open_positions() == []
        # The trade row is still tracked (outcome patched) -- only the phantom
        # position insert is skipped.
        assert db.get_trade_by_order_id("ord-recon-1") is not None

    def test_positive_fill_inserts_row_with_correct_shares(self):
        db = _db()
        _reconcile_db_row(self._record(), "2026-08-11T12:53:01+00:00", db=db, get_fill_size=lambda oid: 4.32)
        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["shares"] == pytest.approx(4.32)
        assert positions[0]["ticker"] == "0xreconticker"

    def test_no_fill_size_lookup_available_skips_insert(self):
        """Back-compat / defensive default: without a fill-size lookup, never
        guess -- the pre-#977 default of shares=0.0 is no longer written."""
        db = _db()
        _reconcile_db_row(self._record(), "2026-08-11T12:53:01+00:00", db=db, get_fill_size=None)
        assert db.get_open_positions() == []

    def test_fill_size_lookup_raising_skips_insert_without_crashing(self):
        db = _db()

        def _boom(order_id):
            raise RuntimeError("exchange API unreachable")

        _reconcile_db_row(self._record(), "2026-08-11T12:53:01+00:00", db=db, get_fill_size=_boom)
        assert db.get_open_positions() == []

    def test_existing_row_for_order_id_is_left_alone(self):
        """Idempotency: a second reconcile pass for an order that already has
        a row must not call get_fill_size again or touch the row."""
        db = _db()
        trade_id = db.insert_trade(
            ts="2026-08-11T11:41:52Z", station="KORD", ticker="0xreconticker",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
            actual_price=70, predicted_edge=16.0, mode="live", capital_before=5.0,
            order_id="ord-recon-1", outcome="filled",
        )
        db.open_position(
            trade_id=trade_id, station="KORD", ticker="0xreconticker",
            token_id="tok-recon-1", side="NO", order_id="ord-recon-1",
            entry_price=70, shares=4.32, entry_ts="2026-08-11T11:41:52Z",
        )
        calls: list = []

        def _tracker(order_id):
            calls.append(order_id)
            return 999.0  # would be very wrong if actually used

        _reconcile_db_row(self._record(), "2026-08-11T12:53:01+00:00", db=db, get_fill_size=_tracker)
        assert calls == [], "existing row must short-circuit before the fill-size lookup"
        assert db.get_open_positions()[0]["shares"] == pytest.approx(4.32)


class TestReconcileTimeoutFillsThreadsLiveTrader:
    def test_live_trader_get_order_fill_size_used_for_shares(self, tmp_path):
        import json
        om = OrderManager()
        db = _db()
        dated = tmp_path / "live_trades.2026-08-11.jsonl"
        base = tmp_path / "live_trades.jsonl"
        record = {
            "asset_id": "tok-live-thread", "outcome": "timeout", "order_id": "ord-live-thread",
            "price_cents": 70, "ts": "2026-08-11T11:41:52+00:00", "station": "KORD",
            "ticker": "0xlivethread", "bracket_low": 32.0, "bracket_high": 36.0, "side": "NO",
            "predicted_price": 70, "edge_cents": 16.0, "size_eur": 5.0,
        }
        dated.write_text(json.dumps(record) + "\n")

        live_trader = MagicMock()
        live_trader.get_order_fill_size.return_value = 7.14

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-live-thread"}):
            om.reconcile_timeout_fills("ts-977", db=db, live_trader=live_trader)

        live_trader.get_order_fill_size.assert_called_once_with("ord-live-thread")
        positions = db.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["shares"] == pytest.approx(7.14)

    def test_without_live_trader_no_phantom_row_created(self, tmp_path):
        """The exact #977 production call before the fix: reconcile_timeout_fills
        was called without a fill-size source. It must now skip the position
        insert rather than write shares=0.0."""
        import json
        om = OrderManager()
        db = _db()
        dated = tmp_path / "live_trades.2026-08-11.jsonl"
        base = tmp_path / "live_trades.jsonl"
        record = {
            "asset_id": "tok-no-trader", "outcome": "timeout", "order_id": "0x6f780a",
            "price_cents": 70, "ts": "2026-08-11T11:41:52+00:00", "station": "KORD",
            "ticker": "0xrealconditionhash", "bracket_low": 32.0, "bracket_high": 36.0,
            "side": "NO", "predicted_price": 70, "edge_cents": 16.0, "size_eur": 5.0,
        }
        dated.write_text(json.dumps(record) + "\n")

        with patch("src.execution.order_manager.LIVE_TRADES_JSONL", base), \
             patch("src.execution.order_manager._wallet_held_token_ids", return_value={"tok-no-trader"}):
            om.reconcile_timeout_fills("ts-977b", db=db)

        assert db.get_open_positions() == [], "no live_trader means no authoritative fill size -- skip"


# ===========================================================================
# entry_ts timestamp format consistency
# ===========================================================================

class TestEntryTsNormalization:
    def test_plus_offset_and_z_suffix_converge(self):
        db = _db()
        trade_id_a = db.insert_trade(
            ts="2026-08-11T11:47:31Z", station="KORD", ticker="0xa",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
            actual_price=70, predicted_edge=16.0, mode="live", capital_before=5.0,
            order_id="ord-ts-a",
        )
        trade_id_b = db.insert_trade(
            ts="2026-08-11T11:41:52+00:00", station="KORD", ticker="0xb",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
            actual_price=70, predicted_edge=16.0, mode="live", capital_before=5.0,
            order_id="ord-ts-b",
        )
        db.open_position(
            trade_id=trade_id_a, station="KORD", ticker="0xa", token_id="tok-ts-a",
            side="NO", order_id="ord-ts-a", entry_price=70, shares=1.0,
            entry_ts="2026-08-11T11:47:31.021528Z",
        )
        db.open_position(
            trade_id=trade_id_b, station="KORD", ticker="0xb", token_id="tok-ts-b",
            side="NO", order_id="ord-ts-b", entry_price=70, shares=1.0,
            entry_ts="2026-08-11T11:41:52.086584+00:00",
        )
        pos_a = db.get_open_position_by_token("tok-ts-a")[0]
        pos_b = db.get_open_position_by_token("tok-ts-b")[0]
        assert pos_a["entry_ts"].endswith("Z") and "+00:00" not in pos_a["entry_ts"]
        assert pos_b["entry_ts"].endswith("Z") and "+00:00" not in pos_b["entry_ts"]
        assert pos_a["entry_ts"] == "2026-08-11T11:47:31.021528Z"
        assert pos_b["entry_ts"] == "2026-08-11T11:41:52.086584Z"

    def test_unparseable_timestamp_passes_through_unchanged(self):
        db = _db()
        trade_id = db.insert_trade(
            ts="not-a-timestamp", station="KORD", ticker="0xc",
            bracket_low=32.0, bracket_high=36.0, side="NO", predicted_price=70,
            actual_price=70, predicted_edge=16.0, mode="live", capital_before=5.0,
            order_id="ord-ts-c",
        )
        db.open_position(
            trade_id=trade_id, station="KORD", ticker="0xc", token_id="tok-ts-c",
            side="NO", order_id="ord-ts-c", entry_price=70, shares=1.0,
            entry_ts="not-a-timestamp",
        )
        assert db.get_open_position_by_token("tok-ts-c")[0]["entry_ts"] == "not-a-timestamp"
