"""Issue #1395 (design #1365): live exposure for partials and ghosts.

Live order gate. Ghosts count at stake_usd until recovery resolves them;
partials stay at stake_usd (the executor does not prove the remainder is
cancelled); pending never lowers.
"""
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.data.copy_pnl import live_exposure_usd
from src.data.db import Database
from src.scripts.copy_live_settle import recover_ghost_orders
from src.scripts.copy_signal_loop import _handle_live_order, live_startup_sanity_check

W1, W2 = "0xw1", "0xw2"


def _seed(db, *, address=W1, status="filled", stake=10.0, filled=None,
          reason=None, order_id=None, fill_price=None, market="0xm"):
    sid = db.insert_copy_signal(address=address, market=market, source_price=0.4,
                                detected_at="2026-10-14T00:00:00+00:00", outcome_index=0)
    pid = db.insert_copy_live_position(signal_id=sid, address=address, market=market,
                                       outcome_index=0, stake_usd=stake,
                                       entry_ts="2026-10-14T00:00:00+00:00",
                                       order_id=order_id)
    with db._lock:
        db._conn.execute(
            "UPDATE copy_live_positions SET status=?, filled_stake_usd=?, "
            "rejected_reason=?, fill_price=? WHERE id=?",
            (status, filled, reason, fill_price, pid),
        )
        db._conn.commit()
    return pid


def _cfg(wallet_cap, total_cap):
    return {
        "COPY_LIVE_TRADING_ENABLED": True,
        "COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD": wallet_cap,
        "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD": total_cap,
    }


def _gate(db, *, stake=10.0, address=W1, wallet_cap=1000.0, total_cap=1000.0):
    """Run the real gate; return (executed, rejected_reason_of_new_row)."""
    with patch("src.scripts.copy_signal_loop.execute_live_copy_order",
               return_value={"status": "filled", "order_id": "o"}) as ex:
        _handle_live_order(
            db, address=address, market="0xnew", outcome_index=0, signal_id=1,
            live_stake=stake, fill_price=0.4, token_id="tok", side_label="YES",
            live_config=_cfg(wallet_cap, total_cap), live_gate_reason=None,
            clob_client_factory=MagicMock(), now_iso="2026-10-14T01:00:00+00:00",
            wallet_live_enabled=True,
        )
    row = db.get_copy_live_positions()[0]
    return ex.called, row["rejected_reason"]


def test_partial_stays_at_intended_stake_even_with_cancelled_remainder_fill_recorded():
    # Not provable from copy_live_executor that the remainder is dead.
    db = Database(":memory:")
    _seed(db, status="partial", stake=10.0, filled=3.0)
    assert live_exposure_usd(db) == 10.0
    assert live_exposure_usd(db, W1) == 10.0


def test_filled_and_pending_unchanged():
    db = Database(":memory:")
    _seed(db, status="filled", stake=10.0, filled=4.0, market="a")
    _seed(db, status="pending", stake=7.0, filled=1.0, market="b")
    assert live_exposure_usd(db) == 17.0


def test_executor_partial_not_proven_cancelled_comment_evidence():
    # Evidence guard: executor ignores cancel_order's bool result, so a
    # "partial" can carry a still-resting remainder. If this ever changes,
    # revisit COALESCE for partials (#1395).
    src = Path("src/execution/copy_live_executor.py").read_text()
    assert re.search(r"^\s+trader\.cancel_order\(order_id\)\s*$", src, re.M)


def test_ghost_counted_per_wallet_and_total_then_released_by_recovery():
    db = Database(":memory:")
    _seed(db, address=W1, status="rejected", reason="cancel_failed_ghost",
          order_id="g1", stake=9.0, fill_price=0.5)
    _seed(db, address=W2, status="filled", stake=1.0, market="x")
    assert live_exposure_usd(db, W1) == 9.0
    assert live_exposure_usd(db, W2) == 1.0
    assert live_exposure_usd(db) == 10.0

    # Recovery: confirmed zero fill + cancelled -> released.
    trader = MagicMock()
    trader.check_fill.return_value = "cancelled"
    trader.get_order_fill_size.return_value = 0.0
    with patch("src.scripts.copy_live_settle.LiveTrader", return_value=trader):
        recover_ghost_orders(db, MagicMock())
    assert live_exposure_usd(db, W1) == 0.0
    assert live_exposure_usd(db) == 1.0


def test_ghost_recovered_as_fill_counts_once_as_open_row():
    db = Database(":memory:")
    _seed(db, status="rejected", reason="cancel_failed_ghost", order_id="g1",
          stake=10.0, fill_price=0.5)
    trader = MagicMock()
    trader.check_fill.return_value = "filled"
    trader.get_order_fill_size.return_value = 20.0
    trader.get_order_fill_cost_usd.return_value = 10.0
    with patch("src.scripts.copy_live_settle.LiveTrader", return_value=trader):
        recover_ghost_orders(db, MagicMock())
    assert live_exposure_usd(db) == 10.0  # not double counted


def test_still_ambiguous_ghost_keeps_counting():
    db = Database(":memory:")
    _seed(db, status="rejected", reason="cancel_failed_ghost", order_id="g1",
          stake=10.0, fill_price=0.5)
    trader = MagicMock()
    trader.check_fill.return_value = "open"
    trader.get_order_fill_size.return_value = 0.0
    with patch("src.scripts.copy_live_settle.LiveTrader", return_value=trader):
        recover_ghost_orders(db, MagicMock())
    assert live_exposure_usd(db) == 10.0


def test_confirmed_rejections_and_settled_not_counted():
    db = Database(":memory:")
    _seed(db, status="rejected", reason="timeout", order_id="o", market="a")
    _seed(db, status="rejected", reason="cancel_confirmed_zero_fill", order_id="o2", market="b")
    _seed(db, status="rejected", reason="cancel_failed_ghost", order_id=None, market="c")
    _seed(db, status="settled", market="d")
    assert live_exposure_usd(db) == 0.0


def test_ghost_makes_previously_passing_order_fail_total_cap():
    db = Database(":memory:")
    _seed(db, address=W2, status="filled", stake=40.0, market="a")
    # Before: 40 + 10 <= 50 passes.
    executed, reason = _gate(db, stake=10.0, total_cap=50.0)
    assert executed and reason is None

    db = Database(":memory:")
    _seed(db, address=W2, status="filled", stake=40.0, market="a")
    _seed(db, address=W2, status="rejected", reason="cancel_failed_ghost",
          order_id="g", stake=5.0, market="g", fill_price=0.5)
    # Now: 45 + 10 > 50 fails.
    executed, reason = _gate(db, stake=10.0, total_cap=50.0)
    assert not executed and reason == "live_total_exposure_limit"


def test_ghost_makes_previously_passing_order_fail_wallet_cap():
    db = Database(":memory:")
    _seed(db, status="rejected", reason="cancel_failed_ghost", order_id="g",
          stake=45.0, fill_price=0.5)
    executed, reason = _gate(db, stake=10.0, wallet_cap=50.0)
    assert not executed and reason == "live_wallet_exposure_limit"


def test_other_wallet_ghost_does_not_hit_wallet_cap():
    db = Database(":memory:")
    _seed(db, address=W2, status="rejected", reason="cancel_failed_ghost",
          order_id="g", stake=45.0, fill_price=0.5)
    executed, reason = _gate(db, stake=10.0, wallet_cap=50.0, total_cap=1000.0)
    assert executed and reason is None


def test_partial_does_not_loosen_gate():
    db = Database(":memory:")
    _seed(db, status="partial", stake=45.0, filled=2.0)
    executed, reason = _gate(db, stake=10.0, wallet_cap=50.0)
    assert not executed and reason == "live_wallet_exposure_limit"


def test_startup_bound_unchanged():
    """#1310 bound is config-only; this PR must not touch it."""
    at = {"COPY_LIVE_MAX_TOTAL_EXPOSURE_USD": 25.0, "COPY_LIVE_DRAWDOWN_STOP_PCT": 0.1}
    over = {"COPY_LIVE_MAX_TOTAL_EXPOSURE_USD": 26.0, "COPY_LIVE_DRAWDOWN_STOP_PCT": 0.1}
    with patch("src.scripts.copy_signal_loop.COPY_LIVE_CAPITAL_USD", 250.0):
        assert live_startup_sanity_check(at) is None
        assert "COPY_LIVE_DRAWDOWN_STOP_PCT" in live_startup_sanity_check(over)
