"""Tests for issue #743: one reprice-retry after a GTC fill timeout.

Verifies:
- timeout + successful cancel + gates still pass at the new book price -> exactly
  one re-placement (place_order called twice)
- new price fails the UNCHANGED gates (or no book) -> no retry
- retry disabled via config -> no retry, book not even fetched
- a fill on the first attempt -> no retry
The same-day entry guard is untouched (the retry is in-process only), so it is
not exercised here.
"""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

# Stub heavy optional imports (mirrors test_order_executor_sizing.py).
_clob_stub = ModuleType("py_clob_client_v2")
_clob_stub.ClobClient = MagicMock  # type: ignore[attr-defined]
_clob_types_stub = ModuleType("py_clob_client_v2.clob_types")
for _name in ("AssetType", "BalanceAllowanceParams", "CreateOrderOptions",
              "OrderArgs", "OpenOrderParams", "OrderPayload", "BookParams",
              "TradeParams", "DropNotifications", "TickSize", "Side", "TimeInForce"):
    setattr(_clob_types_stub, _name, MagicMock)
sys.modules.setdefault("py_clob_client_v2", _clob_stub)
sys.modules.setdefault("py_clob_client_v2.clob_types", _clob_types_stub)

_run_stub = ModuleType("src.scripts.run")
_run_stub._append_live_trade = MagicMock()  # type: ignore[attr-defined]
sys.modules.setdefault("src.scripts.run", _run_stub)


def _make_candidate(side="NO", p_yes=0.02, price_cents=72):
    bracket = MagicMock()
    bracket.no_token_id = "tok_no_743"
    bracket.yes_token_id = "tok_yes_743"
    bracket.ticker = "KORD-bracket-743"
    bracket.low_f = 70.0
    bracket.high_f = 75.0

    candidate = MagicMock()
    candidate.side = side
    candidate.p_yes = p_yes
    candidate.confidence = (1 - p_yes) if side == "NO" else p_yes
    candidate.price_cents = price_cents
    candidate.station = "KORD"
    candidate.bracket = bracket
    candidate.market = {"endDate": "2026-07-19", "question": "Will it be hot?"}
    candidate.edge_cents = 17.0
    candidate.p_yes_raw = p_yes
    return candidate


def _make_db(retry="true"):
    db = MagicMock()
    db.get_all_config.return_value = {
        "POSITION_SIZE_EUR": "5.0",
        "SIZING_MODE": "flat",
        "LIVE_TIMEOUT_REPRICE_RETRY": retry,
    }
    return db


def _run(candidate, db, *, book_asks, timeout=True, fill_status="open"):
    """Run _execute_live; return (trader_mock, get_orderbook_mock)."""
    from src.execution import order_executor as oe

    trader = MagicMock()
    trader.place_order.return_value = "order-id-abcdef123"
    trader.check_fill.return_value = fill_status
    gob = MagicMock(return_value={"asks": book_asks, "bids": []})

    with patch.object(oe, "LiveTrader", return_value=trader), \
         patch.object(oe, "get_orderbook", gob), \
         patch.object(oe, "estimate_fee_cents", return_value=1.0), \
         patch.object(oe.time, "sleep", lambda s: None), \
         patch.object(oe, "FILL_MAX_WAIT_S", 0 if timeout else 100), \
         patch.object(sys.modules["src.scripts.run"], "_append_live_trade", MagicMock()):
        oe._execute_live(
            candidate, MagicMock(), MagicMock(),
            "2026-07-19T12:00:00Z", db=db, bankroll=100.0,
        )
    return trader, gob


class TestRepriceRetry743:
    def test_timeout_then_gates_pass_places_second_order(self):
        # new NO price 78c: ev_no = 0.98*100 - 78 - 1 = 19c (in [15,20]); 78>=70; p_yes 0.02<=0.05
        trader, gob = _run(_make_candidate(), _make_db(),
                           book_asks=[{"price": "0.78", "size": "100"}], timeout=True)
        assert gob.call_count == 1                 # book re-fetched once
        assert trader.place_order.call_count == 2  # original + one retry
        # the retry was placed at the repriced 78c
        assert trader.place_order.call_args_list[1].kwargs["price_cents"] == 78

    def test_new_price_below_min_price_no_retry(self):
        # 60c < MIN_PRICE_CENTS(70) -> gate fails -> no retry
        trader, gob = _run(_make_candidate(), _make_db(),
                           book_asks=[{"price": "0.60", "size": "100"}], timeout=True)
        assert gob.call_count == 1
        assert trader.place_order.call_count == 1

    def test_new_price_edge_too_high_no_retry(self):
        # 76c: ev_no = 98 - 76 - 1 = 21c > MAX_EDGE_CENTS(20) -> gate fails -> no retry
        trader, gob = _run(_make_candidate(), _make_db(),
                           book_asks=[{"price": "0.76", "size": "100"}], timeout=True)
        assert trader.place_order.call_count == 1

    def test_empty_book_no_retry(self):
        trader, gob = _run(_make_candidate(), _make_db(), book_asks=[], timeout=True)
        assert gob.call_count == 1
        assert trader.place_order.call_count == 1

    def test_retry_disabled_does_not_refetch_or_replace(self):
        trader, gob = _run(_make_candidate(), _make_db(retry="false"),
                           book_asks=[{"price": "0.78", "size": "100"}], timeout=True)
        assert gob.call_count == 0                 # never even fetched the book
        assert trader.place_order.call_count == 1

    def test_fill_on_first_attempt_no_retry(self):
        trader, gob = _run(_make_candidate(), _make_db(),
                           book_asks=[{"price": "0.78", "size": "100"}],
                           timeout=False, fill_status="filled")
        assert gob.call_count == 0
        assert trader.place_order.call_count == 1
        trader.cancel_order.assert_not_called()
