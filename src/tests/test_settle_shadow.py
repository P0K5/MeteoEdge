"""Unit tests for settle_shadow_trades() in src/scripts/settle.py.

Covers:
- Shadow YES trade that WON: pnl = (100 - yes_ask) / 100
- Shadow YES trade that LOST: pnl = -(yes_ask) / 100
- Shadow NO trade that WON (bracket missed): pnl = (100 - no_ask) / 100
- Shadow NO trade that LOST (bracket hit): pnl = -(no_ask) / 100
- YES and NO sides settled independently for the same station/date
- Settlement is idempotent (already-settled rows skipped)
- Rows with no truth for station are skipped
- settle_shadow_trades is a no-op when db=None
"""
from datetime import date
from pathlib import Path

from src.data.db import Database
from src.scripts.settle import settle_shadow_trades


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_db(tmp_path: Path) -> Database:
    return Database(path=str(tmp_path / "settle_test.db"))


def _insert_shadow(db: Database, station: str, yes_ask: int, target_date: str,
                   bracket_low: float = 80.0, bracket_high: float = 82.0) -> int:
    """Insert a shadow YES trade for the given station and date."""
    ts = f"{target_date}T10:00:00+00:00"
    return db.insert_trade(
        ts=ts,
        station=station,
        ticker=f"0xSHADOW_{station}_{yes_ask}",
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        side="YES",
        predicted_price=int(yes_ask * 0.9),
        actual_price=yes_ask,
        predicted_edge=10.0,
        mode="shadow",
        capital_before=0.0,
        order_id=None,
        outcome=None,
        pnl=None,
        capital_after=None,
        settled_at=None,
    )


def _insert_shadow_no(db: Database, station: str, no_ask: int, target_date: str,
                      bracket_low: float = 80.0, bracket_high: float = 82.0) -> int:
    """Insert a shadow NO trade for the given station and date."""
    ts = f"{target_date}T10:00:00+00:00"
    return db.insert_trade(
        ts=ts,
        station=station,
        ticker=f"0xSHADOW_NO_{station}_{no_ask}",
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        side="NO",
        predicted_price=int(no_ask * 0.9),
        actual_price=no_ask,
        predicted_edge=10.0,
        mode="shadow",
        capital_before=0.0,
        order_id=None,
        outcome=None,
        pnl=None,
        capital_after=None,
        settled_at=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSettleShadowTradesWon:
    """YES bracket was hit — trade won."""

    def test_pnl_formula_yes_won(self, tmp_path):
        """pnl = (100 - yes_ask) / 100 when the YES bracket is hit."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 32  # 32¢
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        # actual_high=81.0 is within [80, 82] → YES won
        truth = {"KORD": 81.0}
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        expected_pnl = (100 - yes_ask) / 100  # 0.68
        assert abs(t["pnl"] - expected_pnl) < 1e-5
        assert t["outcome"] == "filled"
        assert t["settled_at"] is not None
        db.close()

    def test_capital_after_equals_pnl_when_capital_before_zero(self, tmp_path):
        """capital_after = 0 + pnl (capital_before is 0 for shadow rows)."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 40
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)
        settle_shadow_trades(target, {"KORD": 81.0}, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        expected = (100 - yes_ask) / 100
        assert abs(t["capital_after"] - expected) < 1e-5
        db.close()


class TestSettleShadowTradesLost:
    """YES bracket was NOT hit — trade lost."""

    def test_pnl_formula_yes_lost(self, tmp_path):
        """pnl = -(yes_ask) / 100 when the YES bracket is not hit."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 32  # 32¢
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        # actual_high=85.0 is outside [80, 82] → YES lost
        truth = {"KORD": 85.0}
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        expected_pnl = -yes_ask / 100  # -0.32
        assert abs(t["pnl"] - expected_pnl) < 1e-5
        assert t["outcome"] == "filled"
        db.close()


class TestSettleShadowIdempotency:
    def test_already_settled_rows_are_skipped(self, tmp_path):
        """Running settle_shadow_trades twice does not change pnl on rows already settled."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 32
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        truth = {"KORD": 81.0}
        # First run
        settle_shadow_trades(target, truth, db=db)
        trades_after_first = db.get_trades(limit=None)
        t1 = next(r for r in trades_after_first if r["id"] == tid)
        pnl_first = t1["pnl"]
        settled_at_first = t1["settled_at"]

        # Second run — should be a no-op (settled_at IS NOT NULL)
        settle_shadow_trades(target, truth, db=db)
        trades_after_second = db.get_trades(limit=None)
        t2 = next(r for r in trades_after_second if r["id"] == tid)
        assert abs(t2["pnl"] - pnl_first) < 1e-10
        assert t2["settled_at"] == settled_at_first  # unchanged
        db.close()


class TestSettleShadowEdgeCases:
    def test_no_op_when_db_none(self, tmp_path):
        """settle_shadow_trades with db=None returns without error."""
        # Should not raise
        settle_shadow_trades(date(2025, 6, 1), {"KORD": 81.0}, db=None)

    def test_skips_station_not_in_truth(self, tmp_path):
        """Rows for stations not in truth dict are skipped (outcome remains NULL)."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        tid = _insert_shadow(db, "KMIA", 30, target.isoformat())

        # Truth has KORD but not KMIA
        settle_shadow_trades(target, {"KORD": 81.0}, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        assert t["outcome"] is None   # not settled
        assert t["settled_at"] is None
        db.close()

    def test_multiple_stations_settled_independently(self, tmp_path):
        """Multiple stations are each settled from their own truth value."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)

        # KORD bracket [80-82], actual=81 → YES won
        tid_kord = _insert_shadow(db, "KORD", 30, target.isoformat(),
                                  bracket_low=80.0, bracket_high=82.0)
        # KMIA bracket [88-90], actual=85 → YES lost
        tid_kmia = _insert_shadow(db, "KMIA", 35, target.isoformat(),
                                  bracket_low=88.0, bracket_high=90.0)

        truth = {"KORD": 81.0, "KMIA": 85.0}
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t_kord = next(r for r in trades if r["id"] == tid_kord)
        t_kmia = next(r for r in trades if r["id"] == tid_kmia)

        assert abs(t_kord["pnl"] - (100 - 30) / 100) < 1e-5   # won
        assert abs(t_kmia["pnl"] - (-35 / 100)) < 1e-5         # lost
        db.close()


# ---------------------------------------------------------------------------
# NO-side tests
# ---------------------------------------------------------------------------

class TestSettleShadowNoSideWon:
    """NO bracket wins when the YES bracket is MISSED."""

    def test_pnl_formula_no_won(self, tmp_path):
        """pnl = (100 - no_ask) / 100 when the YES bracket is missed (NO won)."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        no_ask = 68  # 68¢
        tid = _insert_shadow_no(db, "KORD", no_ask, target.isoformat(),
                                bracket_low=80.0, bracket_high=82.0)

        # actual_high=85.0 is outside [80, 82] → YES missed → NO won
        truth = {"KORD": 85.0}
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        expected_pnl = (100 - no_ask) / 100  # 0.32
        assert abs(t["pnl"] - expected_pnl) < 1e-5
        assert t["outcome"] == "filled"
        assert t["settled_at"] is not None
        db.close()


class TestSettleShadowNoSideLost:
    """NO bracket loses when the YES bracket is HIT."""

    def test_pnl_formula_no_lost(self, tmp_path):
        """pnl = -(no_ask) / 100 when the YES bracket is hit (NO lost)."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        no_ask = 68  # 68¢
        tid = _insert_shadow_no(db, "KORD", no_ask, target.isoformat(),
                                bracket_low=80.0, bracket_high=82.0)

        # actual_high=81.0 is within [80, 82] → YES hit → NO lost
        truth = {"KORD": 81.0}
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        expected_pnl = -no_ask / 100  # -0.68
        assert abs(t["pnl"] - expected_pnl) < 1e-5
        assert t["outcome"] == "filled"
        db.close()


class TestSettleShadowMultipleSides:
    """YES and NO shadow rows for the same station/date settle independently."""

    def test_multiple_sides_same_station(self, tmp_path):
        """YES won and NO lost simultaneously when bracket is hit."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)

        # YES side: ask=32¢, bracket [80-82], actual=81 → YES won → pnl = (100-32)/100 = 0.68
        yes_ask = 32
        tid_yes = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                                 bracket_low=80.0, bracket_high=82.0)

        # NO side: ask=68¢, bracket [80-82], actual=81 → YES hit → NO lost → pnl = -68/100 = -0.68
        no_ask = 68
        tid_no = _insert_shadow_no(db, "KORD", no_ask, target.isoformat(),
                                   bracket_low=80.0, bracket_high=82.0)

        truth = {"KORD": 81.0}
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t_yes = next(r for r in trades if r["id"] == tid_yes)
        t_no = next(r for r in trades if r["id"] == tid_no)

        assert abs(t_yes["pnl"] - (100 - yes_ask) / 100) < 1e-5  # YES won: 0.68
        assert abs(t_no["pnl"] - (-no_ask / 100)) < 1e-5          # NO lost: -0.68
        assert t_yes["outcome"] == "filled"
        assert t_no["outcome"] == "filled"
        db.close()
