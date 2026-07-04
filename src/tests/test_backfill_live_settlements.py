"""Unit tests for src/scripts/backfill_live_settlements.py (issue #609).

Covers:
- Dry-run reports what it would do without touching the real DB.
- Dates/stations with no observed truth are skipped and reported, never guessed.
- Idempotent: rerunning after a successful pass settles nothing further.
- Backfill delegates to the SAME settle_live_trades() used nightly.

Dates are anchored relative to today (not hardcoded).
"""
from datetime import date, datetime, timedelta, timezone

from src.data.db import Database
from src.scripts import backfill_live_settlements as backfill


def _fresh_db(db_path) -> Database:
    return Database(path=str(db_path))


def _insert_live(db: Database, *, order_id: str, station: str, end_date: str,
                  side: str = "NO", price_cents: int = 70, size_eur: float = 5.0,
                  bracket_low: float = 70.0, bracket_high: float = 72.0) -> int:
    return db.insert_trade(
        ts=f"{end_date}T12:00:00+00:00",
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
        outcome="filled",
        capital_before=size_eur,
        end_date=end_date,
    )


def _insert_obs(db: Database, *, station: str, ts: str, temp_f: float) -> None:
    db.insert_observation(
        ts=ts, station=station, temp_f=temp_f, temp_native=temp_f,
        unit="F", source="metar",
    )


class TestSkipsDatesWithoutTruth:
    def test_missing_observations_are_skipped_and_reported(self, tmp_path, capsys):
        db_path = tmp_path / "meteoedge.db"
        db = _fresh_db(db_path)
        target = date.today() - timedelta(days=5)
        _insert_live(db, order_id="ord-1", station="KORD", end_date=target.isoformat())
        db.close()

        result = backfill.run_backfill(db_path, target, target, dry_run=False)

        assert result["exit_code"] == 0
        assert result["settled"] == 0
        assert result["skipped_station_days"] == 1
        out = capsys.readouterr().out
        assert "no trustworthy observed high" in out

        # Row remains unsettled -- we must never guess.
        db2 = Database(path=str(db_path))
        assert len(db2.get_unsettled_live_trades()) == 1
        db2.close()


class TestSettlesWithObservedTruth:
    def test_settles_using_station_local_observations(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        db = _fresh_db(db_path)
        target = date.today() - timedelta(days=5)
        _insert_live(db, order_id="ord-2", station="KORD", end_date=target.isoformat(),
                     side="NO", price_cents=70, size_eur=5.0,
                     bracket_low=70.0, bracket_high=72.0)
        # KORD = America/Chicago; 18:00 UTC is still `target` in local time
        # year-round (13:00-14:00 local).
        _insert_obs(db, station="KORD", ts=f"{target.isoformat()}T18:00:00+00:00", temp_f=60.0)
        db.close()

        result = backfill.run_backfill(db_path, target, target, dry_run=False)

        assert result["exit_code"] == 0
        assert result["settled"] == 1
        assert result["skipped_station_days"] == 0

        db2 = Database(path=str(db_path))
        row = db2.get_trades(limit=None)[0]
        assert row["settled_at"] is not None
        assert row["pnl"] == round((100 - 70) / 100 * (5.0 / 0.70), 4)
        db2.close()


class TestDryRunDoesNotTouchRealDb:
    def test_dry_run_leaves_real_db_unsettled(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        db = _fresh_db(db_path)
        target = date.today() - timedelta(days=5)
        _insert_live(db, order_id="ord-3", station="KORD", end_date=target.isoformat())
        _insert_obs(db, station="KORD", ts=f"{target.isoformat()}T18:00:00+00:00", temp_f=60.0)
        db.close()

        result = backfill.run_backfill(db_path, target, target, dry_run=True)

        assert result["exit_code"] == 0
        assert result["settled"] == 1  # reported as would-be-settled

        # The REAL db file must be untouched.
        db2 = Database(path=str(db_path))
        assert len(db2.get_unsettled_live_trades()) == 1
        db2.close()


class TestIdempotentReruns:
    def test_second_run_settles_nothing_further(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        db = _fresh_db(db_path)
        target = date.today() - timedelta(days=5)
        _insert_live(db, order_id="ord-4", station="KORD", end_date=target.isoformat())
        _insert_obs(db, station="KORD", ts=f"{target.isoformat()}T18:00:00+00:00", temp_f=60.0)
        db.close()

        first = backfill.run_backfill(db_path, target, target, dry_run=False)
        second = backfill.run_backfill(db_path, target, target, dry_run=False)

        assert first["settled"] == 1
        assert second["settled"] == 0


class TestMissingDb:
    def test_missing_db_path_returns_error_exit_code(self, tmp_path):
        result = backfill.run_backfill(tmp_path / "does-not-exist.db",
                                        date.today() - timedelta(days=1),
                                        date.today() - timedelta(days=1))
        assert result["exit_code"] == 1


class TestNoPendingTrades:
    def test_no_unsettled_trades_is_a_clean_no_op(self, tmp_path):
        db_path = tmp_path / "meteoedge.db"
        Database(path=str(db_path)).close()
        target = date.today() - timedelta(days=1)

        result = backfill.run_backfill(db_path, target, target)

        assert result == {"exit_code": 0, "settled": 0, "skipped_station_days": 0}
