"""Tests for src/scripts/copy_live_fill_cost_backfill.py (issue #1342).

The backfill is a DRY-RUN report that matches historical settled
``copy_live_positions`` rows against the deposit wallet's Data API trade
tape (NOT the dead CLOB ``get_order()`` lookup the pre-#1342 version used).
These tests pin: the matching rules (exactly-one-match vs no-match vs
ambiguous), the per-row P&L recompute, the resolved/unresolved split in the
summary (never blended), and that nothing is ever written.
"""
import sqlite3

import pytest

from src.data.db import Database
from src.scripts import copy_live_fill_cost_backfill as bf

MARKET = "0xm1"
NOW_ISO = "2026-10-06T00:00:00+00:00"
ENTRY_TS_UNIX = bf._parse_ts_to_unix(NOW_ISO)


def _seed(db: Database, *, market: str = MARKET, status: str = "settled",
          stake_usd: float = 10.0, fill_price: float = 0.40, outcome_index: int = 0,
          filled_stake_usd: "float | None" = None, order_id: "str | None" = "0xo",
          settle_pnl: "float | None" = None, entry_ts: str = NOW_ISO) -> int:
    signal_id = db.insert_copy_signal(
        address="0xfollowed", market=market, source_price=fill_price, detected_at=entry_ts,
        outcome_index=outcome_index,
    )
    pid = db.insert_copy_live_position(
        signal_id=signal_id, address="0xfollowed", market=market,
        outcome_index=outcome_index, stake_usd=stake_usd, entry_ts=entry_ts,
    )
    db.update_copy_live_position_status(
        pid, "filled" if status == "settled" else status, order_id=order_id,
        fill_price=fill_price, filled_stake_usd=filled_stake_usd,
    )
    if status == "settled":
        db.settle_copy_live_position(pid, settle_pnl, entry_ts)
    return pid


def _trade(*, market=MARKET, outcome_index=0, price=0.12, size=25.0,
           timestamp=ENTRY_TS_UNIX, side="BUY"):
    """A raw Data API trade record, field names as the real endpoint uses."""
    return {
        "conditionId": market, "outcomeIndex": outcome_index, "side": side,
        "price": price, "size": size, "timestamp": timestamp, "outcome": "Yes",
    }


def _index(*raw_trades):
    """Build a buy_index from raw Data API trade dicts, via the module's
    own normalize_trade() so tests exercise real field parsing."""
    from src.data.polymarket_traders import normalize_trade

    index: dict = {}
    for raw in raw_trades:
        t = normalize_trade(raw)
        key = (t["market"], t["outcome_index"])
        index.setdefault(key, []).append(t)
    for bucket in index.values():
        bucket.sort(key=lambda t: t["timestamp"])
    return index


def _row(report, pid):
    return next(r for r in report["rows"] if r["id"] == pid)


class TestCleanSingleMatch:
    def test_losing_short_fill_recomputes_smaller_loss(self):
        db = Database(":memory:")
        # Booked at the full $10 stake, but the real fill was 25 shares @
        # 0.12 = $3.00 -> loss shrinks from -10 to -3.
        pid = _seed(db, status="settled", settle_pnl=-10.0, stake_usd=10.0)
        buy_index = _index(_trade(price=0.12, size=25.0))
        rep = bf.build_report(db._conn, buy_index=buy_index, get_resolution=lambda m: False)

        r = _row(rep, pid)
        assert r["match_status"] == "resolved"
        assert r["cost_after"] == 3.0
        assert r["pnl_after"] == -3.0
        assert r["pnl_delta"] == 7.0
        assert rep["summary"]["resolved"]["count"] == 1
        assert rep["summary"]["resolved"]["realized_pnl_delta_usd"] == 7.0
        assert rep["summary"]["unresolved"]["count"] == 0

    def test_winning_short_fill_recomputes_smaller_win(self):
        db = Database(":memory:")
        # Intended $10 at 0.40 (win pnl would be 10*(1-0.4)/0.4=15), but the
        # real fill was only 12 shares @ 0.40 = $4.80 -> win pnl = 12-4.8=7.2.
        pid = _seed(db, status="settled", settle_pnl=15.0, stake_usd=10.0, fill_price=0.40)
        buy_index = _index(_trade(price=0.40, size=12.0))
        rep = bf.build_report(db._conn, buy_index=buy_index, get_resolution=lambda m: True)

        r = _row(rep, pid)
        assert r["match_status"] == "resolved"
        assert r["cost_after"] == 4.8
        assert r["pnl_after"] == 7.2
        assert r["pnl_delta"] == pytest.approx(-7.8)

    def test_near_exact_stake_match_has_near_zero_diff(self):
        db = Database(":memory:")
        # Real fill cost (25 * 0.40 = 10.0) matches the booked $10 stake
        # almost exactly -- correction should be ~0.
        pid = _seed(db, status="settled", settle_pnl=-10.0, stake_usd=10.0, fill_price=0.40)
        buy_index = _index(_trade(price=0.40, size=25.0))
        rep = bf.build_report(db._conn, buy_index=buy_index, get_resolution=lambda m: False)

        r = _row(rep, pid)
        assert r["cost_after"] == 10.0
        assert r["pnl_delta"] == 0.0


class TestNoMatch:
    def test_zero_candidate_trades_is_unresolved_no_match(self):
        db = Database(":memory:")
        pid = _seed(db, status="settled", settle_pnl=-10.0)
        rep = bf.build_report(db._conn, buy_index={}, get_resolution=lambda m: False)

        r = _row(rep, pid)
        assert r["match_status"] == "unresolved"
        assert r["reason"] == "no_match"
        assert r["cost_after"] is None
        assert r["pnl_after"] is None
        # Stored ledger value is reported, untouched.
        assert r["pnl_stored"] == -10.0
        assert rep["summary"]["unresolved"]["count"] == 1
        assert rep["summary"]["unresolved"]["by_reason"]["no_match"] == 1
        assert rep["summary"]["unresolved"]["stake_usd_still_unverified"] == 10.0
        assert rep["summary"]["resolved"]["count"] == 0

    def test_known_data_api_gap_order_ids_report_as_no_match_not_guessed(self):
        """Regression fixture (issue #1342): these order IDs are confirmed
        real fills (per logs/copy_signals.log) that the Data API feed is
        known to miss. A row for one of them must surface as no_match,
        never a fabricated cost."""
        db = Database(":memory:")
        pid = _seed(
            db, status="settled", settle_pnl=-10.0,
            order_id=bf.KNOWN_DATA_API_GAP_ORDER_IDS[0],
        )
        rep = bf.build_report(db._conn, buy_index={}, get_resolution=lambda m: False)

        r = _row(rep, pid)
        assert r["match_status"] == "unresolved"
        assert r["reason"] == "no_match"
        assert r["pnl_stored"] == -10.0  # conservative upper-bound value kept as-is

    def test_trade_outside_window_does_not_count_as_a_match(self):
        db = Database(":memory:")
        pid = _seed(db, status="settled", settle_pnl=-10.0)
        far_away = _trade(timestamp=ENTRY_TS_UNIX + int(bf.DEFAULT_WINDOW_SECONDS) + 1)
        rep = bf.build_report(db._conn, buy_index=_index(far_away), get_resolution=lambda m: False)

        assert _row(rep, pid)["reason"] == "no_match"


class TestAmbiguousMatch:
    def test_two_candidate_trades_in_window_is_unresolved_ambiguous(self):
        db = Database(":memory:")
        pid = _seed(db, status="settled", settle_pnl=-10.0)
        t1 = _trade(price=0.12, size=25.0, timestamp=ENTRY_TS_UNIX)
        t2 = _trade(price=0.13, size=20.0, timestamp=ENTRY_TS_UNIX + 60)
        rep = bf.build_report(db._conn, buy_index=_index(t1, t2), get_resolution=lambda m: False)

        r = _row(rep, pid)
        assert r["match_status"] == "unresolved"
        assert r["reason"] == "ambiguous_match"
        assert r["candidate_count"] == 2
        assert r["cost_after"] is None
        assert rep["summary"]["unresolved"]["by_reason"]["ambiguous_match"] == 1


class TestOtherUnresolvedReasons:
    def test_unparseable_entry_ts_is_unresolved(self, monkeypatch):
        db = Database(":memory:")
        pid = _seed(db, status="settled", settle_pnl=-10.0)
        # Corrupt entry_ts directly (insert path always writes a valid ISO
        # string, so simulate a legacy-bad value post-hoc).
        db._conn.execute("UPDATE copy_live_positions SET entry_ts='not-a-timestamp' WHERE id=?", (pid,))
        rep = bf.build_report(
            db._conn, buy_index=_index(_trade()), get_resolution=lambda m: False,
        )
        r = _row(rep, pid)
        assert r["match_status"] == "unresolved"
        assert r["reason"] == "unparseable_entry_ts"

    def test_matched_trade_but_unresolved_market_is_unresolved(self):
        db = Database(":memory:")
        pid = _seed(db, status="settled", settle_pnl=-10.0)
        rep = bf.build_report(
            db._conn, buy_index=_index(_trade(price=0.12, size=25.0)),
            get_resolution=lambda m: None,
        )
        r = _row(rep, pid)
        assert r["match_status"] == "unresolved"
        assert r["reason"] == "market_unresolved"
        # Cost could still be computed even though P&L could not.
        assert r["cost_after"] == 3.0
        assert r["pnl_after"] is None


class TestScope:
    def test_rows_with_recorded_cost_are_never_repriced_or_looked_up(self):
        db = Database(":memory:")
        pid = _seed(db, status="settled", stake_usd=10.0, filled_stake_usd=3.0, settle_pnl=-3.0)
        rep = bf.build_report(db._conn, buy_index={}, get_resolution=lambda m: False)

        assert all(r["id"] != pid for r in rep["rows"])
        assert rep["summary"]["rows_in_scope"] == 0
        assert rep["summary"]["rows_already_recorded_untouched"] == 1

    def test_open_filled_and_pending_rows_are_out_of_scope(self):
        db = Database(":memory:")
        _seed(db, status="filled", order_id="0xa")
        _seed(db, status="pending", order_id=None)
        rep = bf.build_report(db._conn, buy_index={}, get_resolution=lambda m: False)
        assert rep["rows"] == []
        assert rep["summary"]["rows_in_scope"] == 0


class TestDriftContext:
    def test_drift_before_and_after_uses_only_resolved_delta(self):
        db = Database(":memory:")
        # Settled loss booked at $10 (actually $3): realized stored = -10.
        _seed(db, market="0xm1", status="settled", settle_pnl=-10.0, stake_usd=10.0)
        # An unrelated open position contributes to committed exposure but
        # is never touched by this script.
        _seed(db, market="0xm2", status="filled", stake_usd=5.0)
        rep = bf.build_report(
            db._conn, buy_index=_index(_trade(market="0xm1", price=0.12, size=25.0)),
            get_resolution=lambda m: False,
            capital_usd=40.0, actual_balance_usd=33.0,
        )
        s = rep["summary"]
        # expected_before = 40 - 5 (committed) + (-10) (realized) = 25
        assert s["expected_balance_before_usd"] == 25.0
        # resolved delta = +7 (loss shrinks 10 -> 3)
        assert s["resolved"]["realized_pnl_delta_usd"] == 7.0
        assert s["expected_balance_after_usd"] == 32.0
        assert s["drift_before_usd"] == 8.0
        assert s["drift_after_usd"] == 1.0

    def test_no_capital_means_no_drift_fields(self):
        db = Database(":memory:")
        _seed(db, status="settled", settle_pnl=-10.0)
        rep = bf.build_report(db._conn, buy_index={}, get_resolution=lambda m: False)
        assert "expected_balance_before_usd" not in rep["summary"]
        assert "drift_before_usd" not in rep["summary"]


class TestDryRunNeverWrites:
    def test_build_report_does_not_modify_any_row(self):
        db = Database(":memory:")
        pid = _seed(db, status="settled", settle_pnl=-10.0)
        before = db._conn.execute("SELECT * FROM copy_live_positions ORDER BY id").fetchall()
        bf.build_report(
            db._conn, buy_index=_index(_trade(price=0.12, size=25.0)),
            get_resolution=lambda m: False,
        )
        after = db._conn.execute("SELECT * FROM copy_live_positions ORDER BY id").fetchall()

        assert [tuple(r) for r in before] == [tuple(r) for r in after]
        stored = db._conn.execute(
            "SELECT settled_pnl_usd FROM copy_live_positions WHERE id=?", (pid,)
        ).fetchone()["settled_pnl_usd"]
        assert stored == -10.0

    def test_report_is_marked_dry_run(self):
        db = Database(":memory:")
        rep = bf.build_report(db._conn, buy_index={}, get_resolution=lambda m: False)
        assert rep["dry_run"] is True
        assert "DRY RUN" in bf.format_report(rep)

    def test_cli_connection_is_read_only(self, tmp_path):
        path = tmp_path / "ledger.db"
        seeded = Database(str(path))
        _seed(seeded, status="settled", settle_pnl=-10.0)

        conn = bf._open_readonly(str(path))
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("UPDATE copy_live_positions SET settled_pnl_usd=0")
        finally:
            conn.close()

        check = sqlite3.connect(str(path))
        try:
            (pnl,) = check.execute("SELECT settled_pnl_usd FROM copy_live_positions").fetchone()
        finally:
            check.close()
        assert pnl == -10.0


class TestFindMatchingBuys:
    def test_returns_all_candidates_within_window(self):
        from src.data.polymarket_traders import normalize_trade
        t1 = normalize_trade(_trade(timestamp=ENTRY_TS_UNIX))
        t2 = normalize_trade(_trade(timestamp=ENTRY_TS_UNIX + 100))
        buy_index = {(MARKET, 0): [t1, t2]}
        matches = bf.find_matching_buys(buy_index, MARKET, 0, ENTRY_TS_UNIX, window_seconds=1200)
        assert matches == [t1, t2]

    def test_excludes_candidates_outside_window(self):
        from src.data.polymarket_traders import normalize_trade
        t1 = normalize_trade(_trade(timestamp=ENTRY_TS_UNIX + 5000))
        buy_index = {(MARKET, 0): [t1]}
        matches = bf.find_matching_buys(buy_index, MARKET, 0, ENTRY_TS_UNIX, window_seconds=1200)
        assert matches == []


class TestIndexWalletBuys:
    def test_indexes_only_buy_trades_with_outcome_index(self, monkeypatch):
        from src.data import polymarket_traders as traders

        def fake_get_wallet_trades(address):
            return traders.TradeList([
                _trade(side="BUY", market="0xm1", outcome_index=0, timestamp=100),
                _trade(side="SELL", market="0xm1", outcome_index=0, timestamp=200),
                {**_trade(side="BUY", market="0xm2", outcome_index=1, timestamp=50)},
            ])

        monkeypatch.setattr(bf, "get_wallet_trades", fake_get_wallet_trades)
        index = bf.index_wallet_buys("0xdeposit")
        assert set(index.keys()) == {("0xm1", 0), ("0xm2", 1)}
        assert len(index[("0xm1", 0)]) == 1  # SELL excluded


def test_format_report_notes_unresolved_rows():
    db = Database(":memory:")
    _seed(db, status="settled", settle_pnl=-10.0)
    rep = bf.build_report(db._conn, buy_index={}, get_resolution=lambda m: False)
    out = bf.format_report(rep)
    assert "NOTE:" in out
    assert "never guessed" in out
