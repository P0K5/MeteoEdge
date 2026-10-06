"""Tests for src/scripts/copy_live_fill_cost_backfill.py (issue #1336).

The backfill is a DRY-RUN report. These tests pin three things: the
per-row and drift arithmetic, the scope rules (rows that already carry a
recorded cost are never re-priced), and that nothing is written -- both
the report itself and the CLI's read-only connection.
"""
import sqlite3

import pytest

from src.data.db import Database
from src.scripts import copy_live_fill_cost_backfill as bf

ADDRESS = "0xwallet1"
NOW_ISO = "2026-10-06T00:00:00+00:00"


def _seed(db: Database, *, market: str, status: str, stake_usd: float = 10.0,
          fill_price: float = 0.40, outcome_index: int = 0,
          filled_stake_usd: "float | None" = None, order_id: "str | None" = "0xo",
          settle_pnl: "float | None" = None) -> int:
    signal_id = db.insert_copy_signal(
        address=ADDRESS, market=market, source_price=fill_price, detected_at=NOW_ISO,
        outcome_index=outcome_index,
    )
    pid = db.insert_copy_live_position(
        signal_id=signal_id, address=ADDRESS, market=market,
        outcome_index=outcome_index, stake_usd=stake_usd, entry_ts=NOW_ISO,
    )
    # Settlement is status-scoped (filled/partial -> settled), so a settled
    # fixture always passes through 'filled' first, like production does.
    db.update_copy_live_position_status(
        pid, "filled" if status == "settled" else status, order_id=order_id,
        fill_price=fill_price, filled_stake_usd=filled_stake_usd,
    )
    if status == "settled":
        db.settle_copy_live_position(pid, settle_pnl, NOW_ISO)
    return pid


def _stored_pnl(db: Database, pid: int):
    return db._conn.execute(
        "SELECT settled_pnl_usd FROM copy_live_positions WHERE id=?", (pid,)
    ).fetchone()["settled_pnl_usd"]


def _report(db, *, shares=None, resolution=None, capital=None, actual=None):
    shares = shares or {}
    resolution = resolution or {}

    def get_fill_shares(order_id):
        v = shares.get(order_id, 0.0)
        if isinstance(v, Exception):
            raise v
        return v

    return bf.build_report(
        db._conn,
        get_fill_shares=get_fill_shares,
        get_resolution=lambda market: resolution.get(market),
        capital_usd=capital,
        actual_balance_usd=actual,
    )


def _row(report, pid):
    return next(r for r in report["rows"] if r["id"] == pid)


class TestPerRowRecompute:
    def test_full_fill_recorded_in_fill_record_reprices_settled_loss(self):
        db = Database(":memory:")
        # Intended $10 at 0.40 but the CLOB fill record shows 7.5 shares
        # matched -> real cost $3.00. Stored P&L booked the full $10 loss.
        pid = _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)
        rep = _report(db, shares={"0xo": 7.5}, resolution={"0xm1": False})

        r = _row(rep, pid)
        assert r["cost_before"] == 10.0
        assert r["cost_after"] == 3.0
        assert r["pnl_stored"] == -10.0
        assert r["pnl_after"] == -3.0
        assert r["pnl_delta"] == 7.0
        assert rep["summary"]["realized_pnl_delta_usd"] == 7.0

    def test_no_fill_record_keeps_stake_and_reports_it(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)
        rep = _report(db, shares={"0xo": 0.0}, resolution={"0xm1": False})

        r = _row(rep, pid)
        assert r["cost_after"] == 10.0
        assert r["pnl_delta"] == 0.0
        assert r["note"] == "no_fill_record"
        assert rep["summary"]["no_fill_record"] == 1

    def test_lookup_error_is_reported_and_leaves_cost_unchanged(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)
        rep = _report(db, shares={"0xo": RuntimeError("CLOB 503")}, resolution={"0xm1": False})

        r = _row(rep, pid)
        assert r["cost_after"] == 10.0
        assert r["note"].startswith("lookup_error")
        assert rep["summary"]["lookup_errors"] == 1

    def test_unresolved_market_leaves_pnl_after_empty(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)
        rep = _report(db, shares={"0xo": 7.5}, resolution={})

        r = _row(rep, pid)
        assert r["pnl_after"] is None
        assert "market_unresolved" in r["note"]
        assert rep["summary"]["settled_unresolved"] == 1

    def test_short_full_status_open_position_reports_committed_delta(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="filled", stake_usd=10.0)
        rep = _report(db, shares={"0xo": 7.5})

        r = _row(rep, pid)
        assert r["cost_before"] == 10.0
        assert r["cost_after"] == 3.0
        assert r["pnl_after"] is None
        assert rep["summary"]["committed_delta_usd"] == -7.0

    def test_stored_pnl_inconsistent_with_recompute_is_flagged(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="settled", settle_pnl=-4.0)
        rep = _report(db, shares={"0xo": 0.0}, resolution={"0xm1": False})

        assert "stored_pnl_differs_from_recompute" in _row(rep, pid)["note"]
        assert rep["summary"]["stored_pnl_mismatch"] == 1


class TestScope:
    def test_rows_with_recorded_cost_are_never_repriced_or_looked_up(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="settled", stake_usd=10.0,
                    filled_stake_usd=3.0, settle_pnl=-3.0)
        calls = []

        def get_fill_shares(order_id):
            calls.append(order_id)
            return 99.0

        rep = bf.build_report(
            db._conn, get_fill_shares=get_fill_shares,
            get_resolution=lambda m: False,
        )
        assert calls == []
        assert all(r["id"] != pid for r in rep["rows"])
        assert rep["summary"]["rows_in_scope"] == 0
        assert rep["summary"]["rows_already_recorded_untouched"] == 1

    def test_pending_and_rejected_rows_are_out_of_scope(self):
        db = Database(":memory:")
        _seed(db, market="0xm1", status="pending", order_id=None)
        rep = _report(db, shares={"0xo": 99.0})
        assert rep["rows"] == []


class TestDriftImpact:
    def test_drift_before_and_after_with_settled_and_open_rows(self):
        db = Database(":memory:")
        # Settled loss booked at $10 (actually $3): realized stored = -10.
        _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)
        # Open fill booked at $10 (actually $3): committed stored = 10.
        _seed(db, market="0xm2", status="filled", stake_usd=10.0)
        rep = _report(
            db, shares={"0xo": 7.5}, resolution={"0xm1": False},
            capital=40.0, actual=23.0,
        )
        s = rep["summary"]
        # expected_before = 40 - 10 + (-10) = 20
        assert s["expected_balance_before_usd"] == 20.0
        # realized delta +7 (loss shrinks 10 -> 3), committed delta -7 (open cost 10 -> 3)
        # expected delta = +7 - (-7) = +14 -> expected_after = 34
        assert s["expected_balance_delta_usd"] == 14.0
        assert s["expected_balance_after_usd"] == 34.0
        assert s["drift_before_usd"] == 3.0
        assert s["drift_after_usd"] == -11.0


class TestDryRunNeverWrites:
    def test_build_report_does_not_modify_any_row(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)
        _seed(db, market="0xm2", status="filled", stake_usd=10.0)
        before = db._conn.execute("SELECT * FROM copy_live_positions ORDER BY id").fetchall()
        _report(db, shares={"0xo": 7.5}, resolution={"0xm1": False})
        after = db._conn.execute("SELECT * FROM copy_live_positions ORDER BY id").fetchall()

        assert [tuple(r) for r in before] == [tuple(r) for r in after]
        assert _stored_pnl(db, pid) == -10.0

    def test_report_is_marked_dry_run(self):
        db = Database(":memory:")
        rep = _report(db)
        assert rep["dry_run"] is True
        assert "DRY RUN" in bf.format_report(rep)

    def test_cli_connection_is_read_only(self, tmp_path):
        path = tmp_path / "ledger.db"
        seeded = Database(str(path))
        _seed(seeded, market="0xm1", status="settled", settle_pnl=-10.0)

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


class TestFillShareParsing:
    """The raw CLOB read must distinguish a real zero fill from a failed or
    empty response -- LiveTrader.get_order_fill_size conflates them."""

    def test_empty_response_raises_not_zero(self):
        with pytest.raises(ValueError):
            bf.fill_shares_from_order(None)
        with pytest.raises(ValueError):
            bf.fill_shares_from_order({})

    def test_matched_size_string_is_parsed(self):
        assert bf.fill_shares_from_order({"size_matched": "7.5"}) == 7.5

    def test_present_zero_is_a_real_no_fill(self):
        assert bf.fill_shares_from_order({"status": "CANCELED", "size_matched": "0"}) == 0.0

    def test_failed_lookup_is_reported_as_lookup_error_in_report(self):
        db = Database(":memory:")
        pid = _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)

        def boom(order_id):
            return bf.fill_shares_from_order(None)

        rep = bf.build_report(
            db._conn, get_fill_shares=boom, get_resolution=lambda m: False,
        )
        r = _row(rep, pid)
        assert r["note"].startswith("lookup_error")
        assert r["cost_after"] == 10.0
        assert rep["summary"]["no_fill_record"] == 0


def test_format_report_warns_when_lookups_failed():
    db = Database(":memory:")
    _seed(db, market="0xm1", status="settled", settle_pnl=-10.0)
    rep = bf.build_report(
        db._conn, get_fill_shares=lambda o: bf.fill_shares_from_order(None),
        get_resolution=lambda m: False,
    )
    assert "NOT valid evidence" in bf.format_report(rep)
