"""Tests for live config reading in _execute_live (issue #662).

Verifies that _execute_live reads POSITION_SIZE_EUR and SIZING_MODE from the
database on each call, and falls back to module-level constants when the DB is
unavailable or returns an invalid/missing value.
"""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavy optional imports so the test can run without Polymarket SDKs
# ---------------------------------------------------------------------------
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in ("AssetType", "BalanceAllowanceParams", "CreateOrderOptions",
              "OrderArgs", "OpenOrderParams", "OrderPayload", "BookParams",
              "OpenOrderParams", "TradeParams", "DropNotifications",
              "TickSize", "Side", "TimeInForce"):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

# _execute_live does a local import of _append_live_trade from src.scripts.run.
# Stub the whole module so we don't pull in run.py's heavy top-level imports.
_run_stub = ModuleType("src.scripts.run")
_run_stub._append_live_trade = MagicMock()  # type: ignore[attr-defined]
sys.modules.setdefault("src.scripts.run", _run_stub)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(confidence=0.85, price_cents=30, side="NO"):
    bracket = MagicMock()
    bracket.no_token_id = "tok_no"
    bracket.yes_token_id = "tok_yes"
    bracket.ticker = "KORD-bracket"
    bracket.low_f = 70.0
    bracket.high_f = 75.0

    candidate = MagicMock()
    candidate.confidence = confidence
    candidate.price_cents = price_cents
    candidate.side = side
    candidate.station = "KORD"
    candidate.bracket = bracket
    candidate.market = {"endDate": "2026-07-10", "question": "Will it be hot?"}
    candidate.edge_cents = 5.0
    candidate.p_yes_raw = 0.15
    return candidate


def _make_db(position_size_eur=2.0, sizing_mode="flat"):
    """DB mock whose get_all_config returns the given sizing params."""
    db = MagicMock()
    db.get_all_config.return_value = {
        "POSITION_SIZE_EUR": str(position_size_eur),
        "SIZING_MODE": sizing_mode,
    }
    return db


def _run(candidate, db, bankroll=100.0):
    """Run _execute_live and return the trade record passed to _append_live_trade."""
    from src.execution.order_executor import _execute_live

    trader = MagicMock()
    trader.place_order.return_value = "order-id-abcdef"
    trader.check_fill.return_value = "filled"

    captured = {}

    def _capture(record, db=None):
        captured.update(record)

    with patch("src.execution.order_executor.LiveTrader", return_value=trader), \
         patch.object(sys.modules["src.scripts.run"], "_append_live_trade", side_effect=_capture):
        _execute_live(
            candidate, MagicMock(), MagicMock(),
            "2026-07-09T12:00:00Z", db=db, bankroll=bankroll,
        )

    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExecuteLiveSizingFromDB:
    def test_db_position_size_used(self):
        """DB value of 2.0 is used instead of the module-level default."""
        result = _run(_make_candidate(), _make_db(position_size_eur=2.0))
        assert result["size_eur"] == pytest.approx(2.0)

    def test_db_sizing_mode_recorded(self):
        """Sizing mode read from DB is recorded in the trade record."""
        result = _run(_make_candidate(), _make_db(sizing_mode="flat"))
        assert result["sizing_mode"] == "flat"

    def test_db_none_falls_back_to_constant(self):
        """When db=None the module-level POSITION_SIZE_EUR constant is used."""
        import src.execution.order_executor as oe
        original = oe.POSITION_SIZE_EUR
        oe.POSITION_SIZE_EUR = 7.0
        try:
            result = _run(_make_candidate(), db=None)
            assert result["size_eur"] == pytest.approx(7.0)
        finally:
            oe.POSITION_SIZE_EUR = original

    def test_missing_key_in_db_falls_back_to_constant(self):
        """If POSITION_SIZE_EUR is absent from get_live_config, fall back to constant."""
        import src.execution.order_executor as oe
        db = MagicMock()
        db.get_all_config.return_value = {"SIZING_MODE": "flat"}  # key absent
        original = oe.POSITION_SIZE_EUR
        oe.POSITION_SIZE_EUR = 5.0
        try:
            result = _run(_make_candidate(), db)
            assert result["size_eur"] == pytest.approx(5.0)
        finally:
            oe.POSITION_SIZE_EUR = original

    def test_invalid_sizing_mode_falls_back_to_constant(self):
        """An unrecognised SIZING_MODE value in the DB falls back to the constant."""
        import src.execution.order_executor as oe
        db = _make_db(sizing_mode="bogus")
        original = oe.SIZING_MODE
        oe.SIZING_MODE = "flat"
        try:
            result = _run(_make_candidate(), db)
            assert result["sizing_mode"] == "flat"
        finally:
            oe.SIZING_MODE = original

    def test_zero_position_size_falls_back_to_constant(self):
        """A zero POSITION_SIZE_EUR in the DB (invalid) falls back to constant."""
        import src.execution.order_executor as oe
        db = _make_db(position_size_eur=0.0)
        original = oe.POSITION_SIZE_EUR
        oe.POSITION_SIZE_EUR = 5.0
        try:
            result = _run(_make_candidate(), db)
            assert result["size_eur"] == pytest.approx(5.0)
        finally:
            oe.POSITION_SIZE_EUR = original

    def test_negative_position_size_falls_back_to_constant(self):
        """A negative POSITION_SIZE_EUR in the DB falls back to constant."""
        import src.execution.order_executor as oe
        db = _make_db(position_size_eur=-1.0)
        original = oe.POSITION_SIZE_EUR
        oe.POSITION_SIZE_EUR = 5.0
        try:
            result = _run(_make_candidate(), db)
            assert result["size_eur"] == pytest.approx(5.0)
        finally:
            oe.POSITION_SIZE_EUR = original
