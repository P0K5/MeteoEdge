"""Unit tests for the DB-driven settle_live_trades() rewrite (issue #609).

Covers:
- Settlement works with a rotated/stale/missing live_trades.jsonl present
  (the frozen-file scenario that caused #609) -- settles from the DB anyway.
- Held-to-expiry win/loss PnL formulas, YES and NO sides.
- Sold rows are never double-settled (outcome flips to 'sold' on the SAME row).
- Already-settled rows are skipped on rerun (idempotent).
- Legacy rows without end_date settle via the station-local ts fallback.
- open_positions cleanup after settlement.

Issue #617: the settle.py JSONL enrichment write-back for the dashboard
closed-positions panel (_enrich_jsonl_with_settlements()) was removed once
the panel started reading settled held-to-expiry trades directly from the
trades table (db.get_settled_live_trades(), see src/dashboard/api.py and
src/tests/test_dashboard.py::TestDbSettledPositionsParity) -- there is no
longer a JSONL write-back to test here.

Dates are anchored relative to today (not hardcoded) so these tests do not
rot as "today" moves forward.
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from src.data.db import Database
from src.scripts import settle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path) -> Database:
    return Database(path=str(tmp_path / "settle_live_test.db"))


def _insert_live(
    db: Database,
    *,
    order_id: str,
    station: str = "KORD",
    side: str = "NO",
    price_cents: int = 70,
    size_eur: float = 5.0,
    bracket_low: float = 70.0,
    bracket_high: float = 72.0,
    end_date: "str | None" = None,
    ts: "str | None" = None,
    outcome: "str | None" = "filled",
    settled_at: "str | None" = None,
) -> int:
    """Insert a live trade row matching the shape LiveTrader.place_order() writes."""
    ts = ts or datetime.now(timezone.utc).isoformat()
    return db.insert_trade(
        ts=ts,
        station=station,
        ticker=f"{station}-order-{order_id[:8]}",
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        side=side,
        predicted_price=price_cents,
        actual_price=price_cents,
        predicted_edge=10.0,
        mode="live",
        order_id=order_id,
        outcome=outcome,
        capital_before=size_eur,
        settled_at=settled_at,
        end_date=end_date,
    )


def _no_jsonl(tmp_path: Path, monkeypatch) -> None:
    """Point settle.LIVE_TRADES_JSONL at a path that does not exist."""
    monkeypatch.setattr(settle, "LIVE_TRADES_JSONL", tmp_path / "logs" / "live_trades.jsonl")


def _stale_jsonl(tmp_path: Path, monkeypatch, records: "list[dict] | None" = None) -> Path:
    """Write a plain (non-rotated) live_trades.jsonl -- simulates the file frozen
    since log rotation moved writes to dated files (the actual #609 bug)."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(exist_ok=True)
    jsonl = logs_dir / "live_trades.jsonl"
    lines = [json.dumps(r) for r in (records or [])]
    jsonl.write_text("\n".join(lines) + ("\n" if lines else ""))
    monkeypatch.setattr(settle, "LIVE_TRADES_JSONL", jsonl)
    return jsonl


# ---------------------------------------------------------------------------
# The core #609 regression: settle must work even with a frozen/missing JSONL
# ---------------------------------------------------------------------------

class TestSettlesFromDbRegardlessOfJsonl:
    def test_settles_when_jsonl_missing(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=5)
        _insert_live(db, order_id="ord-1", end_date=target.isoformat(),
                     side="NO", price_cents=70, size_eur=5.0,
                     bracket_low=70.0, bracket_high=72.0)

        # bracket missed -> NO wins
        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["settled_at"] is not None
        assert row["pnl"] == round((100 - 70) / 100 * (5.0 / 0.70), 4)
        db.close()

    def test_settles_when_jsonl_is_stale_and_unrelated(self, tmp_path, monkeypatch):
        """A stale live_trades.jsonl with unrelated/old content must not block
        DB settlement -- this is exactly the #609 failure mode (frozen file)."""
        db = _fresh_db(tmp_path)
        _stale_jsonl(tmp_path, monkeypatch, records=[
            {"order_id": "some-ancient-order", "outcome": "filled",
             "station": "KMIA", "end_date": "2026-06-16"},
        ])
        target = date.today() - timedelta(days=5)
        _insert_live(db, order_id="ord-2", end_date=target.isoformat(),
                     side="YES", price_cents=30, size_eur=5.0,
                     bracket_low=80.0, bracket_high=82.0)

        settle.settle_live_trades(target, {"KORD": 81.0}, db=db)  # YES bracket hit -> won

        row = next(r for r in db.get_trades(limit=None) if r["order_id"] == "ord-2")
        assert row["pnl"] == round((100 - 30) / 100 * (5.0 / 0.30), 4)
        assert row["settled_at"] is not None
        db.close()


# ---------------------------------------------------------------------------
# PnL formulas — held to expiry, YES and NO sides
# ---------------------------------------------------------------------------

class TestHeldToExpiryPnlFormulas:
    def test_no_side_won_bracket_missed(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=3)
        _insert_live(db, order_id="ord-no-won", side="NO", price_cents=70,
                     size_eur=5.0, bracket_low=70.0, bracket_high=72.0,
                     end_date=target.isoformat())

        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)  # outside bracket -> NO wins

        row = db.get_trades(limit=None)[0]
        shares = 5.0 / 0.70
        assert row["pnl"] == round((100 - 70) / 100 * shares, 4)
        db.close()

    def test_no_side_lost_bracket_hit(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=3)
        _insert_live(db, order_id="ord-no-lost", side="NO", price_cents=70,
                     size_eur=5.0, bracket_low=70.0, bracket_high=72.0,
                     end_date=target.isoformat())

        settle.settle_live_trades(target, {"KORD": 71.0}, db=db)  # inside bracket -> NO loses

        row = db.get_trades(limit=None)[0]
        shares = 5.0 / 0.70
        assert row["pnl"] == round(-70 / 100 * shares, 4)
        db.close()

    def test_yes_side_won_bracket_hit(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=3)
        _insert_live(db, order_id="ord-yes-won", side="YES", price_cents=32,
                     size_eur=5.0, bracket_low=80.0, bracket_high=82.0,
                     end_date=target.isoformat())

        settle.settle_live_trades(target, {"KORD": 81.0}, db=db)  # inside bracket -> YES wins

        row = db.get_trades(limit=None)[0]
        shares = 5.0 / 0.32
        assert row["pnl"] == round((100 - 32) / 100 * shares, 4)
        db.close()

    def test_yes_side_lost_bracket_missed(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=3)
        _insert_live(db, order_id="ord-yes-lost", side="YES", price_cents=32,
                     size_eur=5.0, bracket_low=80.0, bracket_high=82.0,
                     end_date=target.isoformat())

        settle.settle_live_trades(target, {"KORD": 85.0}, db=db)  # outside bracket -> YES loses

        row = db.get_trades(limit=None)[0]
        shares = 5.0 / 0.32
        assert row["pnl"] == round(-32 / 100 * shares, 4)
        db.close()


# ---------------------------------------------------------------------------
# Sold rows are never double-settled
# ---------------------------------------------------------------------------

class TestSoldRowsNotDoubleSettled:
    def test_sold_row_excluded_from_unsettled_query(self, tmp_path, monkeypatch):
        """order_manager._record_sell_in_db() flips the SAME row's outcome to
        'sold' and sets settled_at at sell time -- get_unsettled_live_trades()
        (outcome='filled') must not surface it."""
        db = _fresh_db(tmp_path)
        target = date.today() - timedelta(days=2)
        _insert_live(db, order_id="ord-sold", side="NO", price_cents=70,
                     size_eur=5.0, end_date=target.isoformat(),
                     outcome="sold", settled_at=datetime.now(timezone.utc).isoformat())
        # Simulate the sell path having already written a pnl for this row.
        db.update_trade_by_order("ord-sold", pnl=0.42)

        assert db.get_unsettled_live_trades() == []

        _no_jsonl(tmp_path, monkeypatch)
        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["pnl"] == 0.42  # untouched by settle_live_trades
        assert row["outcome"] == "sold"
        db.close()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_rerun_does_not_change_pnl_or_daily_total(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=4)
        _insert_live(db, order_id="ord-idem", side="NO", price_cents=70,
                     size_eur=5.0, end_date=target.isoformat())

        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)
        row1 = db.get_trades(limit=None)[0]
        pnl1, settled_at1 = row1["pnl"], row1["settled_at"]
        daily1 = db.get_daily_pnl(target.isoformat())

        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)
        row2 = db.get_trades(limit=None)[0]

        assert row2["pnl"] == pnl1
        assert row2["settled_at"] == settled_at1
        assert db.get_daily_pnl(target.isoformat()) == daily1
        db.close()


# ---------------------------------------------------------------------------
# Legacy rows without end_date fall back to station-local ts date
# ---------------------------------------------------------------------------

class TestLegacyEndDateFallback:
    def test_legacy_row_settles_via_station_local_ts(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=10)
        # KORD is America/Chicago (UTC-5/-6) -- pick a UTC ts late enough in
        # the day that it's still `target` in Chicago local time.
        ts = f"{target.isoformat()}T22:00:00+00:00"
        _insert_live(db, order_id="ord-legacy", side="NO", price_cents=70,
                     size_eur=5.0, end_date=None, ts=ts)

        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["settled_at"] is not None
        assert row["pnl"] == round((100 - 70) / 100 * (5.0 / 0.70), 4)
        db.close()

    def test_legacy_row_not_settled_for_wrong_date(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=10)
        wrong_target = target - timedelta(days=1)
        ts = f"{target.isoformat()}T22:00:00+00:00"
        _insert_live(db, order_id="ord-legacy-2", side="NO", price_cents=70,
                     size_eur=5.0, end_date=None, ts=ts)

        settle.settle_live_trades(wrong_target, {"KORD": 60.0}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["settled_at"] is None
        assert row["pnl"] is None
        db.close()


# ---------------------------------------------------------------------------
# Gamma-authoritative settlement for real 0x markets (issue #644)
# ---------------------------------------------------------------------------

class TestGammaAuthoritativeSettlement:
    """Rows with a real 0x ticker settle ONLY from the definitive Gamma
    resolution; unresolved markets stay pending and are retried via the
    SETTLE_LOOKBACK_DAYS window on later runs."""

    def _insert_0x(self, db, *, order_id: str, end_date: str, side: str = "NO",
                   price_cents: int = 70, size_eur: float = 5.0) -> int:
        return db.insert_trade(
            ts=datetime.now(timezone.utc).isoformat(),
            station="KORD",
            ticker=f"0x{order_id.encode().hex()}",
            bracket_low=70.0, bracket_high=72.0,
            side=side, predicted_price=price_cents, actual_price=price_cents,
            predicted_edge=10.0, mode="live", order_id=order_id,
            outcome="filled", capital_before=size_eur, end_date=end_date,
        )

    def test_gamma_no_won_settles_win(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=1)
        self._insert_0x(db, order_id="ord-g1", end_date=target.isoformat())

        with patch("src.scripts.settle.fetch_market_resolution", return_value=False):
            settle.settle_live_trades(target, {}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["pnl"] == round((100 - 70) / 100 * (5.0 / 0.70), 4)
        db.close()

    def test_gamma_yes_won_settles_no_side_loss_despite_metar_win(self, tmp_path, monkeypatch):
        """The audit's headline failure: METAR truth says the bracket was
        missed (NO won) but the market resolved YES — the market must win."""
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=1)
        self._insert_0x(db, order_id="ord-g2", end_date=target.isoformat())

        # truth says 60.0 (outside 70-72 -> NO would "win" under METAR)
        with patch("src.scripts.settle.fetch_market_resolution", return_value=True):
            settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["pnl"] == round(-70 / 100 * (5.0 / 0.70), 4)  # full-stake loss
        db.close()

    def test_unresolved_market_stays_pending_never_metar(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=1)
        self._insert_0x(db, order_id="ord-g3", end_date=target.isoformat())

        # METAR truth available, but the market has not resolved -> pending
        with patch("src.scripts.settle.fetch_market_resolution", return_value=None):
            settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["pnl"] is None
        assert row["settled_at"] is None
        db.close()

    def test_pending_row_retried_and_credited_to_own_date(self, tmp_path, monkeypatch):
        """A later run picks up the overdue row via the lookback window and
        credits its PnL to the row's OWN trade date in risk_state."""
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        trade_day = date.today() - timedelta(days=4)
        self._insert_0x(db, order_id="ord-g4", end_date=trade_day.isoformat())

        with patch("src.scripts.settle.fetch_market_resolution", return_value=None):
            settle.settle_live_trades(trade_day, {}, db=db)
        assert db.get_trades(limit=None)[0]["settled_at"] is None

        later_target = date.today() - timedelta(days=1)
        with patch("src.scripts.settle.fetch_market_resolution", return_value=False):
            settle.settle_live_trades(later_target, {}, db=db)

        row = db.get_trades(limit=None)[0]
        expected = round((100 - 70) / 100 * (5.0 / 0.70), 4)
        assert row["pnl"] == expected
        assert db.get_daily_pnl(trade_day.isoformat()) == expected
        assert db.get_daily_pnl(later_target.isoformat()) == 0.0
        db.close()

    def test_row_older_than_lookback_not_touched(self, tmp_path, monkeypatch):
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        old_day = date.today() - timedelta(days=settle.SETTLE_LOOKBACK_DAYS + 5)
        self._insert_0x(db, order_id="ord-g5", end_date=old_day.isoformat())

        with patch("src.scripts.settle.fetch_market_resolution", return_value=False):
            settle.settle_live_trades(date.today() - timedelta(days=1), {}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["settled_at"] is None
        db.close()


# ---------------------------------------------------------------------------
# open_positions cleanup
# ---------------------------------------------------------------------------

class TestOpenPositionsCleanup:
    def test_open_position_removed_after_settlement(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=1)
        trade_id = _insert_live(db, order_id="ord-open", side="NO", price_cents=70,
                                 size_eur=5.0, end_date=target.isoformat())
        db.open_position(
            trade_id=trade_id, station="KORD", ticker="KORD-order-ord-open",
            token_id="tok-1", side="NO", order_id="ord-open",
            entry_price=70, shares=7.14,
            entry_ts=datetime.now(timezone.utc).isoformat(),
        )
        assert len(db.get_open_positions()) == 1

        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        assert db.get_open_positions() == []
        db.close()


# ---------------------------------------------------------------------------
# Issue #617: the dashboard's closed-positions panel now reads settled
# held-to-expiry trades directly from the trades table
# (db.get_settled_live_trades()) instead of a JSONL write-back -- the old
# _enrich_jsonl_with_settlements() write-back tested here was removed.
# ---------------------------------------------------------------------------

class TestDbSettledLiveTrades:
    def test_settled_row_selectable_via_get_settled_live_trades(self, tmp_path, monkeypatch):
        db = _fresh_db(tmp_path)
        _no_jsonl(tmp_path, monkeypatch)
        target = date.today() - timedelta(days=1)
        _insert_live(db, order_id="ord-enrich", side="NO", price_cents=70,
                     size_eur=5.0, end_date=target.isoformat())

        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        settled = db.get_settled_live_trades()
        assert len(settled) == 1
        assert settled[0]["order_id"] == "ord-enrich"
        assert settled[0]["pnl"] == round((100 - 70) / 100 * (5.0 / 0.70), 4)
        assert settled[0]["settled_at"] is not None
        db.close()

    def test_settlement_survives_unreadable_jsonl_directory(self, tmp_path, monkeypatch):
        """Even if the JSONL directory cannot be read (used only by
        _write_db_settlements, the `settlements` table writer), DB settlement
        of the trades table must still succeed -- JSONL is never the source
        of truth for live trades."""
        db = _fresh_db(tmp_path)
        target = date.today() - timedelta(days=1)
        _insert_live(db, order_id="ord-robust", side="NO", price_cents=70,
                     size_eur=5.0, end_date=target.isoformat())

        # Point at a path whose parent doesn't exist -- read should just no-op.
        monkeypatch.setattr(settle, "LIVE_TRADES_JSONL",
                             Path("/nonexistent-dir-xyz/live_trades.jsonl"))

        settle.settle_live_trades(target, {"KORD": 60.0}, db=db)

        row = db.get_trades(limit=None)[0]
        assert row["settled_at"] is not None
        db.close()
