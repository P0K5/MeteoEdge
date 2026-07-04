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


def _insert_shadow_direction(db: Database, station: str, side: str, ask: int,
                             target_date: str, direction: str,
                             bracket_low: float = 80.0, bracket_high: float = 82.0) -> int:
    """Insert a shadow trade with an explicit `direction` (issue #610)."""
    ts = f"{target_date}T10:00:00+00:00"
    return db.insert_trade(
        ts=ts,
        station=station,
        ticker=f"0xSHADOW_{direction}_{station}_{ask}",
        bracket_low=bracket_low,
        bracket_high=bracket_high,
        side=side,
        predicted_price=int(ask * 0.9),
        actual_price=ask,
        predicted_edge=10.0,
        mode="shadow",
        capital_before=0.0,
        order_id=None,
        outcome=None,
        pnl=None,
        capital_after=None,
        settled_at=None,
        direction=direction,
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


# ---------------------------------------------------------------------------
# direction='low' rows must never be settled against the daily HIGH (#610)
# ---------------------------------------------------------------------------

class TestSettleShadowSkipsLowDirection:
    """`truth` is the daily HIGH per station -- a direction='low' row settled
    against it produces a near-guaranteed fake result. These rows must be
    skipped entirely, leaving outcome/pnl/settled_at untouched."""

    def test_low_direction_row_not_settled(self, tmp_path):
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        # Low-market bracket (e.g. "lowest temp") that happens to fall inside
        # the daily HIGH range -- if incorrectly settled against `truth`
        # (the daily high), this NO row would spuriously "win".
        tid = _insert_shadow_direction(
            db, "LFPB", "NO", 70, target.isoformat(), direction="low",
            bracket_low=59.0, bracket_high=60.8,
        )

        truth = {"LFPB": 60.0}  # daily HIGH; would make NO "win" if mis-settled
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        assert t["outcome"] is None
        assert t["pnl"] is None
        assert t["settled_at"] is None
        db.close()

    def test_low_direction_row_still_unsettled_and_returned_by_query(self, tmp_path):
        """get_unsettled_shadow_trades() must surface the direction column so
        settle_shadow_trades can actually branch on it."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        _insert_shadow_direction(
            db, "KMIA", "NO", 55, target.isoformat(), direction="low",
            bracket_low=80.0, bracket_high=81.0,
        )

        rows = db.get_unsettled_shadow_trades(target.isoformat())
        assert len(rows) == 1
        assert rows[0]["direction"] == "low"
        db.close()

    def test_high_and_low_rows_settled_independently_same_station_day(self, tmp_path):
        """A direction='high' row settles normally while a direction='low'
        row for the same station/day is skipped."""
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)

        tid_high = _insert_shadow_direction(
            db, "LFPB", "YES", 30, target.isoformat(), direction="high",
            bracket_low=75.0, bracket_high=77.0,
        )
        tid_low = _insert_shadow_direction(
            db, "LFPB", "NO", 70, target.isoformat(), direction="low",
            bracket_low=59.0, bracket_high=60.8,
        )

        truth = {"LFPB": 76.0}  # inside the high bracket -> high YES row wins
        settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t_high = next(r for r in trades if r["id"] == tid_high)
        t_low = next(r for r in trades if r["id"] == tid_low)

        assert t_high["outcome"] == "filled"
        assert abs(t_high["pnl"] - (100 - 30) / 100) < 1e-5

        assert t_low["outcome"] is None
        assert t_low["pnl"] is None
        assert t_low["settled_at"] is None
        db.close()


# ---------------------------------------------------------------------------
# Issue #622 regression tests
# ---------------------------------------------------------------------------

class TestBracketTopEdgeExclusive:
    """C-bucket bracket uses [lo, hi) — top edge is exclusive."""

    def test_actual_at_top_edge_is_no_win(self, tmp_path):
        """METAR says actual == hi → YES does NOT win (hi is exclusive).

        Regression for issue #622: old code used lo <= actual <= hi (inclusive),
        which incorrectly called YES won at the top bracket edge.
        """
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 40
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        # actual equals hi exactly — should NOT be YES won (exclusive upper bound)
        truth = {"KORD": 82.0}
        with patch("src.scripts.settle.fetch_market_final_price", return_value=None):
            settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        assert t["outcome"] == "filled"
        # YES lost (hi exclusive): pnl = -ask / 100
        expected_pnl = -yes_ask / 100
        assert abs(t["pnl"] - expected_pnl) < 1e-5, (
            f"Expected pnl {expected_pnl} (YES lost, top edge exclusive), got {t['pnl']}"
        )
        db.close()

    def test_actual_just_inside_wins(self, tmp_path):
        """actual = hi - epsilon → YES wins (strictly inside bracket)."""
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 40
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        truth = {"KORD": 81.9}
        with patch("src.scripts.settle.fetch_market_final_price", return_value=None):
            settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        expected_pnl = (100 - yes_ask) / 100
        assert abs(t["pnl"] - expected_pnl) < 1e-5
        db.close()


class TestGammaPreferenceInShadow:
    """settle_shadow_trades() prefers Gamma resolution over METAR."""

    def test_gamma_yes_overrides_metar_no(self, tmp_path):
        """Gamma says YES won (~100) but METAR would say NO — Gamma wins."""
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 30
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        # actual=85 is outside bracket → METAR would say YES lost
        truth = {"KORD": 85.0}
        with patch("src.scripts.settle.fetch_market_final_price", return_value=97):
            settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        # Gamma YES price=97 → yes_won=True → YES won
        expected_pnl = (100 - yes_ask) / 100
        assert abs(t["pnl"] - expected_pnl) < 1e-5

    def test_gamma_no_overrides_metar_yes(self, tmp_path):
        """Gamma says NO won (~0) but METAR would say YES — Gamma wins."""
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 30
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        # actual=81 is inside bracket → METAR would say YES won
        truth = {"KORD": 81.0}
        with patch("src.scripts.settle.fetch_market_final_price", return_value=2):
            settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        # Gamma YES price=2 → yes_won=False → YES lost
        expected_pnl = -yes_ask / 100
        assert abs(t["pnl"] - expected_pnl) < 1e-5

    def test_gamma_network_failure_falls_back_to_metar(self, tmp_path):
        """Gamma returns None (network failure) → METAR truth is used."""
        from unittest.mock import patch
        db = _fresh_db(tmp_path)
        target = date(2025, 6, 1)
        yes_ask = 30
        tid = _insert_shadow(db, "KORD", yes_ask, target.isoformat(),
                             bracket_low=80.0, bracket_high=82.0)

        # actual=81 is inside bracket → METAR says YES won
        truth = {"KORD": 81.0}
        with patch("src.scripts.settle.fetch_market_final_price", return_value=None):
            settle_shadow_trades(target, truth, db=db)

        trades = db.get_trades(limit=None)
        t = next(r for r in trades if r["id"] == tid)
        expected_pnl = (100 - yes_ask) / 100
        assert abs(t["pnl"] - expected_pnl) < 1e-5
