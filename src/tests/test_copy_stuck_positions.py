"""Issue #1301: stuck copy-position escalation, abandon, incomplete denominator."""
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.data.db import Database
from src.scripts import copy_abandon
from src.scripts.copy_settle import run_once
from src.scripts.daily_health_report import _build_copy_stuck

W = "0xwallet1"


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _seed(db, market="0xm", hours_ago=60.0, address=W, price=0.45, stake=5.0):
    sid = db.insert_copy_signal(address=address, market=market, source_price=price,
                                detected_at=_iso(hours_ago), outcome_index=0)
    pid = db.insert_copy_position(signal_id=sid, address=address, market=market,
                                  outcome_index=0, entry_price=price,
                                  stake_usd=stake, entry_ts=_iso(hours_ago))
    db.link_copy_signal_to_position(sid, pid)
    return pid


def _state(**kw):
    base = {"closed": False, "end_date": datetime.now(timezone.utc) + timedelta(days=30),
            "uma": None, "yes_price_cents": 45}
    base.update(kw)
    return base


NOT_DUE = _state()
PAST_END = _state(end_date=datetime.now(timezone.utc) - timedelta(hours=60))
VOID = _state(closed=True, uma="resolved", yes_price_cents=50,
              end_date=datetime.now(timezone.utc) - timedelta(hours=3))


def _run(db, state=NOT_DUE, calls=None):
    esc: list = []

    def fake(market):
        if calls is not None:
            calls.append(market)
        if isinstance(state, Exception):
            raise state
        return state

    with patch("src.scripts.copy_settle.fetch_market_resolution", return_value=None), \
         patch("src.scripts.copy_settle.fetch_market_state", side_effect=fake):
        run_once(db=db, escalations=esc)
    return esc


def _status(db, pid):
    return db._conn.execute("SELECT status FROM copy_positions WHERE id=?", (pid,)).fetchone()[0]


def test_long_dated_open_market_is_not_escalated():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 20)
    assert _run(db, NOT_DUE) == []
    assert _status(db, pid) == "open"


def test_past_end_date_unresolved_escalates_from_end_date():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 20)
    esc = _run(db, PAST_END)
    assert len(esc) == 1
    rec = esc[0]
    assert rec["position_id"] == pid and rec["wallet"] == W and rec["market"] == "0xm"
    assert rec["category"] == "PAST_END_UNRESOLVED" and rec["level"] == 1
    assert rec["entry_price"] == 0.45 and rec["age_hours"] > 400
    assert _status(db, pid) == "open"


def test_past_end_under_48h_not_escalated_even_if_position_old():
    db = Database(":memory:")
    _seed(db, hours_ago=24 * 20)
    assert _run(db, _state(end_date=datetime.now(timezone.utc) - timedelta(hours=10))) == []


def test_past_end_over_7d_is_level_2():
    db = Database(":memory:")
    _seed(db, hours_ago=24 * 20)
    esc = _run(db, _state(end_date=datetime.now(timezone.utc) - timedelta(days=8)))
    assert esc[0]["level"] == 2 and esc[0]["step"] == "PATHOLOGICAL_7D"


def test_closed_ambiguous_void_escalates_immediately_once():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=60)
    esc = _run(db, VOID)
    assert [e["category"] for e in esc] == ["CLOSED_AMBIGUOUS"] and esc[0]["level"] == 3
    assert _run(db, VOID) == [] and _run(db, VOID) == []
    assert _status(db, pid) == "open"


def test_unretrievable_escalates_by_entry_age():
    db = Database(":memory:")
    _seed(db, hours_ago=60)
    esc = _run(db, None)
    assert esc[0]["category"] == "UNRETRIEVABLE" and esc[0]["level"] == 1


def test_state_lookup_failure_does_not_escalate():
    db = Database(":memory:")
    _seed(db, hours_ago=60)
    assert _run(db, RuntimeError("down")) == []


def test_under_48h_position_never_fetches_state():
    db = Database(":memory:")
    _seed(db, hours_ago=5)
    calls: list = []
    assert _run(db, PAST_END, calls) == [] and calls == []


def test_one_state_lookup_per_distinct_market():
    db = Database(":memory:")
    _seed(db, market="0xm", hours_ago=60)
    _seed(db, market="0xm", hours_ago=61)
    _seed(db, market="0xn", hours_ago=62)
    calls: list = []
    _run(db, NOT_DUE, calls)
    assert sorted(calls) == ["0xm", "0xn"]


def test_repeated_runs_do_not_reemit_and_escalation_steps_once():
    db = Database(":memory:")
    _seed(db, hours_ago=24 * 20)
    assert len(_run(db, PAST_END)) == 1
    assert _run(db, PAST_END) == [] and _run(db, PAST_END) == []
    esc = _run(db, _state(end_date=datetime.now(timezone.utc) - timedelta(days=9)))
    assert [e["level"] for e in esc] == [2]
    assert _run(db, _state(end_date=datetime.now(timezone.utc) - timedelta(days=9))) == []


def test_no_path_reaches_terminal_state_without_operator():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 30)
    for st in (NOT_DUE, PAST_END, VOID, None):
        for _ in range(3):
            _run(db, st)
            assert _status(db, pid) == "open"


def test_settle_at_half_for_void_keeps_loss_in_roi_and_is_idempotent():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=60, price=0.604, stake=5.0)
    assert copy_abandon.settle_at_price(db, pid, "andre", "50/50 void", 0.5) is True
    row = dict(db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (pid,)).fetchone())
    assert row["status"] == "settled"
    assert row["settled_pnl_usd"] == pytest.approx(5.0 * (0.5 / 0.604 - 1), abs=1e-5)
    assert row["abandoned_by"] == "andre" and row["abandon_reason"] == "50/50 void"
    assert row["abandon_observed_price"] == 0.5 and row["settled_at"]
    assert copy_abandon.settle_at_price(db, pid, "x", "again", 0.1) is False
    assert db._conn.execute("SELECT settled_pnl_usd FROM copy_positions WHERE id=?", (pid,)).fetchone()[0] == row["settled_pnl_usd"]


def test_settle_at_price_no_side_and_extremes_match_normal_formula():
    from src.data.copy_pnl import compute_pnl_at_observed_price_usd as f, compute_realized_pnl_usd as g
    for oi in (0, 1):
        for yes_won in (True, False):
            assert f(entry_price=0.4, stake_usd=10, outcome_index=oi, yes_price=1.0 if yes_won else 0.0) \
                == pytest.approx(g(entry_price=0.4, stake_usd=10, outcome_index=oi, yes_won=yes_won))
    assert f(entry_price=0.4, stake_usd=10, outcome_index=1, yes_price=0.5) == pytest.approx(10 * (0.5 / 0.4 - 1))


def test_settle_at_price_requires_operator_reason_valid_price():
    db = Database(":memory:")
    pid = _seed(db)
    with pytest.raises(ValueError):
        db.settle_copy_position_at_price(pid, "", "r", 0.5, "t")
    with pytest.raises(ValueError):
        db.settle_copy_position_at_price(pid, "op", "r", 1.5, "t")
    assert _status(db, pid) == "open"


def test_fetch_market_state_queries_both_ways_and_unions():
    from src.data import polymarket as pm
    calls = []

    class R:
        def __init__(s, j): s.j = j
        def raise_for_status(s): pass
        def json(s): return s.j

    row = {"conditionId": "0xabc", "closed": False, "endDate": "2026-10-16T12:00:00Z",
           "umaResolutionStatus": None, "outcomePrices": '["0.4","0.6"]'}

    def fake_fetch(url, timeout=None):
        calls.append(url)
        return R([] if "closed=true" in url else [row])

    with patch.object(pm, "fetch", fake_fetch):
        st = pm.fetch_market_state("0xabc")
    assert "closed=true" in calls[0] and "closed" not in calls[1]
    assert st["closed"] is False and st["yes_price_cents"] == 40 and st["end_date"].year == 2026

    with patch.object(pm, "fetch", lambda url, timeout=None: R([])):
        assert pm.fetch_market_state("0xabc") is None

    def boom(url, timeout=None):
        raise OSError("x")
    with patch.object(pm, "fetch", boom), pytest.raises(RuntimeError):
        pm.fetch_market_state("0xabc")


def test_abandon_is_audited_and_idempotent():
    db = Database(":memory:")
    pid = _seed(db)
    assert copy_abandon.abandon(db, pid, "andre", "UMA stalled", 0.55) is True
    row = dict(db._conn.execute("SELECT * FROM copy_positions WHERE id=?", (pid,)).fetchone())
    assert row["status"] == "abandoned" and row["abandoned_by"] == "andre"
    assert row["abandon_reason"] == "UMA stalled" and row["abandon_observed_price"] == 0.55
    assert row["abandoned_at"] and row["settled_pnl_usd"] is None
    assert copy_abandon.abandon(db, pid, "other", "again") is False
    assert db._conn.execute("SELECT abandoned_by FROM copy_positions WHERE id=?", (pid,)).fetchone()[0] == "andre"
    assert db.get_open_copy_positions() == []


def test_abandon_requires_operator_and_reason_and_valid_price():
    db = Database(":memory:")
    pid = _seed(db)
    with pytest.raises(ValueError):
        db.abandon_copy_position(pid, "", "r", "t")
    with pytest.raises(ValueError):
        db.abandon_copy_position(pid, "op", " ", "t")
    with pytest.raises(ValueError):
        copy_abandon.abandon(db, pid, "op", "r", 1.5)
    assert _status(db, pid) == "open"


def test_abandon_cannot_touch_settled_row():
    db = Database(":memory:")
    pid = _seed(db)
    db.settle_copy_position(pid, 1.0, _iso(0))
    assert copy_abandon.abandon(db, pid, "op", "r") is False
    assert _status(db, pid) == "settled"


def test_per_wallet_pnl_reports_incomplete_denominator():
    db = Database(":memory:")
    done = _seed(db, market="0xdone", hours_ago=300)
    db.settle_copy_position(done, 2.0, _iso(0))
    s1 = _seed(db, market="0xstuck", hours_ago=24 * 9, stake=7.0)
    _seed(db, market="0xlong", hours_ago=24 * 9, stake=3.0)   # long-dated, never escalated
    _seed(db, market="0xnew", hours_ago=5, stake=3.0)          # young: nowhere
    db.set_copy_position_stuck_level(s1, 2, _iso(0), "PAST_END_UNRESOLVED")
    ab = _seed(db, market="0xdead", hours_ago=24 * 10)
    db.abandon_copy_position(ab, "op", "dead", _iso(0))
    (row,) = db.get_copy_realized_pnl_by_wallet(W)
    assert row["n_settled"] == 1
    assert row["n_unresolved"] == 1 and row["unresolved_stake_usd"] == 7.0
    assert row["n_not_due"] == 1 and row["n_abandoned"] == 1


def test_health_report_lists_stuck_and_counts_not_due_separately():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 9)
    _seed(db, market="0xlong", hours_ago=24 * 9)
    db.set_copy_position_stuck_level(pid, 3, _iso(0), "CLOSED_AMBIGUOUS")
    lines, flags = _build_copy_stuck(db, datetime.now(timezone.utc))
    text = "\n".join(lines)
    assert flags == {"n_stuck": 1, "n_urgent": 1}
    assert f"id={pid}" in text and W in text and "0xm" in text and "CLOSED_AMBIGUOUS" in text
    assert "not due (long-dated, not stuck): 1" in text


def test_health_report_clean_when_only_long_dated():
    db = Database(":memory:")
    _seed(db, hours_ago=24 * 9)
    _, flags = _build_copy_stuck(db, datetime.now(timezone.utc))
    assert flags == {"n_stuck": 0, "n_urgent": 0}


def test_migration_rebuilds_old_check_and_preserves_rows(tmp_path):
    path = str(tmp_path / "old.db")
    db = Database(path)
    pid = _seed(db)
    db._conn.close()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE copy_positions RENAME TO cp_old")
    conn.execute(
        "CREATE TABLE copy_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "signal_id INTEGER NOT NULL REFERENCES copy_signals(id), address TEXT NOT NULL, "
        "market TEXT NOT NULL, outcome_index INTEGER NOT NULL, entry_price REAL NOT NULL, "
        "stake_usd REAL NOT NULL, entry_ts TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','settled')), "
        "settled_pnl_usd REAL, settled_at TEXT)"
    )
    conn.execute("INSERT INTO copy_positions (id,signal_id,address,market,outcome_index,"
                 "entry_price,stake_usd,entry_ts) SELECT id,signal_id,address,market,"
                 "outcome_index,entry_price,stake_usd,entry_ts FROM cp_old")
    conn.execute("DROP TABLE cp_old")
    conn.commit()
    conn.close()
    db2 = Database(path)
    assert _status(db2, pid) == "open"
    assert db2.abandon_copy_position(pid, "op", "r", _iso(0)) is True


# ---- #1388: PAST_END_TRADING (real shapes from the #1301 host diagnosis) ----
_PAST = datetime(2026, 10, 5, tzinfo=timezone.utc)


def _brazil(price, bid, ask, last):
    return _state(end_date=datetime.now(timezone.utc) - timedelta(days=9),
                  yes_price_cents=price, best_bid=bid, best_ask=ask, last_trade=last)


BRAZIL = [_brazil(87, 0.86, 0.88, 0.874), _brazil(12, 0.12, 0.13, 0.125), _brazil(98, 0.97, 0.99, 0.98)]


@pytest.mark.parametrize("st", BRAZIL)
def test_past_end_trading_real_shapes_not_escalated(st):
    from src.data.copy_stuck import PAST_END_TRADING, classify_market
    assert classify_market(st, _iso(24 * 20), datetime.now(timezone.utc)) == (PAST_END_TRADING, 0)
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 20)
    assert _run(db, st) == []
    row = db._conn.execute("SELECT stuck_level, stuck_category, status FROM copy_positions WHERE id=?", (pid,)).fetchone()
    assert tuple(row) == (0, "PAST_END_TRADING", "open")


@pytest.mark.parametrize("kw", [
    {"best_bid": 0.0}, {"best_ask": 1.0}, {"best_bid": 0.2, "best_ask": 0.8},
    {"best_bid": None}, {"last_trade": None}, {"last_trade": 0.0}, {"closed": True},
])
def test_not_trading_still_escalates(kw):
    from src.data.copy_stuck import PAST_END_UNRESOLVED, classify_market
    st = {**_brazil(87, 0.86, 0.88, 0.874), **kw}
    cat, lvl = classify_market(st, _iso(24 * 20), datetime.now(timezone.utc))
    assert cat == PAST_END_UNRESOLVED and lvl == 2


def test_escalated_position_deescalates_then_realerts_once():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 20)
    stale = _state(end_date=datetime.now(timezone.utc) - timedelta(days=9))
    assert [e["level"] for e in _run(db, stale)] == [2]
    assert _run(db, BRAZIL[0]) == []
    assert tuple(db._conn.execute("SELECT stuck_level, stuck_category FROM copy_positions WHERE id=?", (pid,)).fetchone()) == (0, "PAST_END_TRADING")
    assert [e["level"] for e in _run(db, stale)] == [2]   # stopped trading: alert again
    assert _run(db, stale) == []                           # but only once
    assert _status(db, pid) == "open"


def test_trading_category_cleared_when_no_longer_trading_under_48h_of_end():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 20)
    _run(db, BRAZIL[0])
    _run(db, _state(end_date=datetime.now(timezone.utc) - timedelta(hours=1)))
    assert db._conn.execute("SELECT stuck_category FROM copy_positions WHERE id=?", (pid,)).fetchone()[0] is None


def test_fetch_market_state_returns_liquidity_fields():
    from src.data import polymarket as pm

    class R:
        def raise_for_status(s): pass
        def json(s): return [{"conditionId": "0xabc", "closed": False, "endDate": "2026-10-05T00:00:00Z",
                              "umaResolutionStatus": None, "outcomePrices": '["0.874","0.126"]',
                              "bestBid": 0.86, "bestAsk": "0.88", "lastTradePrice": 0.874}]

    with patch.object(pm, "fetch", lambda url, timeout=None: R()):
        st = pm.fetch_market_state("0xabc")
    assert (st["best_bid"], st["best_ask"], st["last_trade"]) == (0.86, 0.88, 0.874)


def test_health_report_lists_past_end_trading_and_does_not_warn():
    db = Database(":memory:")
    pid = _seed(db, hours_ago=24 * 20)
    _run(db, BRAZIL[0])
    lines, flags = _build_copy_stuck(db, datetime.now(timezone.utc))
    text = "\n".join(lines)
    assert flags == {"n_stuck": 0, "n_urgent": 0}
    assert f"[PAST_END_TRADING] id={pid}" in text
    assert "still trading (not escalated): 1" in text and "not due (long-dated, not stuck): 0" in text
