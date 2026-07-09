"""Unit tests for per-side station shadow overrides (issue #271).

Covers:
  - DB migration: yes_enabled and no_enabled columns added to station_overrides
  - DB migration: legacy enabled=0 → yes_enabled=0, no_enabled=0 (back-compat)
  - DB: get_station_override / set_station_override round-trip with dict return
  - Scanner: per-side shadow logic for all four YES/NO combinations
  - Scanner: yes_enabled=False in station_overrides forces YES shadow
  - Config: SHADOW_STATIONS_YES / SHADOW_STATIONS_NO env var seeding
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.data.db import Database
from src.strategy.scanner import Candidate
from src.model.envelope import Bracket, WeatherState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db() -> Database:
    return Database(":memory:")


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _make_weather_state(station: str = "KORD") -> WeatherState:
    now = datetime.now(timezone.utc)
    return WeatherState(
        station=station,
        now_local=now,
        sunset_local=now,
        current_high_f=70.0,
        current_high_time=now,
        latest_temp_f=68.0,
        latest_temp_time=now,
        forecast_high_f=82.0,
    )


def _make_bracket_yes(yes_ask: int = 72, no_ask: int = 30) -> Bracket:
    """YES-eligible bracket: p_yes=0.90, ev_yes=17¢ (MIN=15, MAX=20)."""
    return Bracket(
        ticker="0xTEST",
        low_f=81.0,
        high_f=83.0,
        yes_ask_cents=yes_ask,
        yes_ask_size=500,
        no_ask_cents=no_ask,
        no_ask_size=500,
    )


def _make_bracket_no(yes_ask: int = 24, no_ask: int = 78) -> Bracket:
    """NO-eligible bracket: p_yes=0.05, no_ask=78¢.

    ev_no = (1-0.05)*100 - 78 - 1 = 16¢ (MIN=15, MAX=20 → in range).
    no_ask=78 >= MIN_PRICE_CENTS=60.
    p_yes=0.05 <= MAX_CONFIDENCE_YES_FOR_NO=0.05.
    """
    return Bracket(
        ticker="0xTESTNO",
        low_f=81.0,
        high_f=83.0,
        yes_ask_cents=yes_ask,
        yes_ask_size=500,
        no_ask_cents=no_ask,
        no_ask_size=500,
    )


def _make_market(bracket: Bracket) -> dict:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00Z")
    return {
        "question": "Will the highest temperature in Chicago be 81-83°F on test date?",
        "groupItemTitle": "81-83°F",
        "conditionId": "0xCONDITION",
        "endDate": today_str,
        "yesTokenId": "0xYES",
        "noTokenId": "0xNO",
    }


def _run_yes_scan(weather, market, *,
                  shadow_stations=None, shadow_stations_yes=None,
                  shadow_stations_no=None, db=None):
    """Run scan_markets with controlled settings; bracket yields YES candidate."""
    from src.strategy import scanner as _scanner_mod
    bracket = _make_bracket_yes()
    patches = [
        patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch.object(_scanner_mod, "SHADOW_STATIONS",
                     shadow_stations if shadow_stations is not None else set()),
        patch.object(_scanner_mod, "SHADOW_STATIONS_YES",
                     shadow_stations_yes if shadow_stations_yes is not None else set()),
        patch.object(_scanner_mod, "SHADOW_STATIONS_NO",
                     shadow_stations_no if shadow_stations_no is not None else set()),
        patch.object(_scanner_mod, "DISABLED_STATIONS",
                     shadow_stations if shadow_stations is not None else set()),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
        patch("src.strategy.scanner.emos_serving_mu", side_effect=lambda *a, **kw: None),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
        patch("src.strategy.scanner.true_probability_yes", return_value=0.90),
        patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0),
    ]
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        from src.strategy.scanner import scan_markets
        candidates, _ = scan_markets(weather, [market], db=db)
    return candidates


def _run_no_scan(weather, market, *, shadow_stations=None, shadow_stations_no=None,
                 shadow_stations_yes=None, db=None):
    """Run scan_markets with controlled settings; bracket yields NO candidate."""
    from src.strategy import scanner as _scanner_mod
    bracket = _make_bracket_no()
    patches = [
        patch.object(_scanner_mod, "parse_bracket_from_market", return_value=bracket),
        patch.object(_scanner_mod, "ENABLE_CLOB_ENRICHMENT", False),
        patch.object(_scanner_mod, "SHADOW_STATIONS",
                     shadow_stations if shadow_stations is not None else set()),
        patch.object(_scanner_mod, "SHADOW_STATIONS_YES",
                     shadow_stations_yes if shadow_stations_yes is not None else set()),
        patch.object(_scanner_mod, "SHADOW_STATIONS_NO",
                     shadow_stations_no if shadow_stations_no is not None else set()),
        patch.object(_scanner_mod, "DISABLED_STATIONS",
                     shadow_stations if shadow_stations is not None else set()),
        patch("src.strategy.scanner.get_orderbook", return_value={"asks": [], "bids": []}),
        patch("src.strategy.scanner.check_taf_disruption", return_value=False),
        patch("src.strategy.scanner.get_city_mode", return_value="legacy"),
        patch("src.strategy.scanner.emos_serving_mu", side_effect=lambda *a, **kw: None),
        patch("src.strategy.scanner._check_ready_for_promotion", return_value=False),
        # p_yes=0.05 → NO-side eligible (MAX_CONFIDENCE_YES_FOR_NO=0.05)
        patch("src.strategy.scanner.true_probability_yes", return_value=0.05),
        patch("src.strategy.scanner.estimate_fee_cents", return_value=1.0),
        # Disable margin gate so NO candidate is created
        patch.object(_scanner_mod, "MIN_FORECAST_BRACKET_MARGIN_F", -999.0),
    ]
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        from src.strategy.scanner import scan_markets
        candidates, _ = scan_markets(weather, [market], db=db)
    return candidates


# ---------------------------------------------------------------------------
# DB migration tests
# ---------------------------------------------------------------------------

class TestMigrationAddsColumns:
    def test_fresh_db_has_yes_no_enabled_columns(self):
        """Fresh DB must have yes_enabled and no_enabled in station_overrides."""
        db = _mem_db()
        cur = db._conn.execute("PRAGMA table_info(station_overrides)")
        columns = {row[1] for row in cur.fetchall()}
        assert "yes_enabled" in columns
        assert "no_enabled" in columns

    def test_migration_adds_yes_no_to_existing_db(self):
        """Migration must add yes_enabled/no_enabled to a legacy DB that lacks them."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            # Create a legacy DB with only the old columns
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE station_overrides (
                    station TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()

            # Open through Database() — migration must add the new columns
            db = Database(path)
            cur = db._conn.execute("PRAGMA table_info(station_overrides)")
            columns = {row[1] for row in cur.fetchall()}
            assert "yes_enabled" in columns, "yes_enabled column missing after migration"
            assert "no_enabled" in columns, "no_enabled column missing after migration"
            db.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_migration_idempotent(self):
        """Running Database() twice on the same file must not raise errors."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            db1 = Database(path)
            db1.close()
            db2 = Database(path)
            cur = db2._conn.execute("PRAGMA table_info(station_overrides)")
            columns = {row[1] for row in cur.fetchall()}
            assert "yes_enabled" in columns
            assert "no_enabled" in columns
            db2.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass


class TestLegacyEnabledFalseMigration:
    def test_legacy_enabled_false_maps_to_both_shadow(self):
        """A legacy row with enabled=0 must get yes_enabled=0, no_enabled=0 after migration."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            # Create legacy DB and insert a disabled row
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE station_overrides (
                    station TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO station_overrides(station, enabled, updated_at) VALUES(?,?,?)",
                ("RKSI", 0, "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
            conn.close()

            db = Database(path)
            cur = db._conn.execute(
                "SELECT yes_enabled, no_enabled FROM station_overrides WHERE station=?",
                ("RKSI",),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 0, f"yes_enabled should be 0, got {row[0]}"
            assert row[1] == 0, f"no_enabled should be 0, got {row[1]}"
            db.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass

    def test_legacy_enabled_true_not_affected_by_backcompat_migration(self):
        """A legacy row with enabled=1 must keep yes_enabled=1, no_enabled=1."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            path = tf.name
        try:
            conn = sqlite3.connect(path)
            conn.execute("""
                CREATE TABLE station_overrides (
                    station TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT INTO station_overrides(station, enabled, updated_at) VALUES(?,?,?)",
                ("KORD", 1, "2026-01-01T00:00:00+00:00"),
            )
            conn.commit()
            conn.close()

            db = Database(path)
            cur = db._conn.execute(
                "SELECT yes_enabled, no_enabled FROM station_overrides WHERE station=?",
                ("KORD",),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == 1, f"yes_enabled should be 1, got {row[0]}"
            assert row[1] == 1, f"no_enabled should be 1, got {row[1]}"
            db.close()
        finally:
            os.unlink(path)
            for ext in ("-wal", "-shm"):
                try:
                    os.unlink(path + ext)
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# DB helper method tests
# ---------------------------------------------------------------------------

class TestGetSetStationOverrideDict:
    def test_get_returns_none_when_no_row(self):
        db = _mem_db()
        assert db.get_station_override("KORD") is None

    def test_set_get_both_enabled(self):
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        assert db.get_station_override("KORD") == {"yes_enabled": True, "no_enabled": True, "low_no_enabled": False}

    def test_set_get_yes_shadow(self):
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=False, no_enabled=True)
        assert db.get_station_override("KORD") == {"yes_enabled": False, "no_enabled": True, "low_no_enabled": False}

    def test_set_get_no_shadow(self):
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=False)
        assert db.get_station_override("KORD") == {"yes_enabled": True, "no_enabled": False, "low_no_enabled": False}

    def test_set_get_both_shadow(self):
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=False, no_enabled=False)
        assert db.get_station_override("KORD") == {"yes_enabled": False, "no_enabled": False, "low_no_enabled": False}

    def test_upsert_overwrites(self):
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        db.set_station_override("KORD", yes_enabled=False, no_enabled=True)
        assert db.get_station_override("KORD") == {"yes_enabled": False, "no_enabled": True, "low_no_enabled": False}

    def test_get_all_returns_dict_of_dicts(self):
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=False)
        db.set_station_override("KMIA", yes_enabled=False, no_enabled=False)
        result = db.get_all_station_overrides()
        assert result == {
            "KORD": {"yes_enabled": True, "no_enabled": False, "low_no_enabled": False},
            "KMIA": {"yes_enabled": False, "no_enabled": False, "low_no_enabled": False},
        }


# ---------------------------------------------------------------------------
# Scanner per-side shadow tests
# ---------------------------------------------------------------------------

class TestScannerPerSideShadow:
    def setup_method(self):
        self.weather = {"KORD": _make_weather_state("KORD")}
        self.yes_market = _make_market(_make_bracket_yes())
        self.no_market = _make_market(_make_bracket_no())

    def test_scanner_live_when_both_enabled(self):
        """With both sides enabled in DB, YES candidate is not shadow."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        candidates = _run_yes_scan(
            self.weather, self.yes_market,
            db=db,
        )
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1
        assert yes_cands[0].shadow is False

    def test_scanner_shadow_yes_when_yes_enabled_false(self):
        """DB yes_enabled=False → YES candidate has shadow=True."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=False, no_enabled=True)
        candidates = _run_yes_scan(
            self.weather, self.yes_market,
            db=db,
        )
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1
        assert yes_cands[0].shadow is True

    def test_scanner_shadow_no_when_no_enabled_false(self):
        """DB no_enabled=False → NO candidate has shadow=True."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=False)
        candidates = _run_no_scan(
            self.weather, self.no_market,
            db=db,
        )
        no_cands = [c for c in candidates if c.side == "NO"]
        assert len(no_cands) == 1
        assert no_cands[0].shadow is True

    def test_scanner_live_no_when_no_enabled_true(self):
        """DB no_enabled=True → NO candidate is not shadow."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        candidates = _run_no_scan(
            self.weather, self.no_market,
            db=db,
        )
        no_cands = [c for c in candidates if c.side == "NO"]
        assert len(no_cands) == 1
        assert no_cands[0].shadow is False

    def test_yes_enabled_true_in_db_produces_live_candidate(self):
        """DB yes_enabled=True → YES candidate is live (shadow=False), station override is sole control."""
        db = _mem_db()
        db.set_station_override("KORD", yes_enabled=True, no_enabled=True)
        candidates = _run_yes_scan(
            self.weather, self.yes_market,
            db=db,
        )
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1
        assert yes_cands[0].shadow is False

    def test_scanner_no_db_falls_back_to_env_all_live(self):
        """With no DB and empty SHADOW_STATIONS, both sides are live."""
        candidates = _run_yes_scan(
            self.weather, self.yes_market,
            shadow_stations=set(),
            shadow_stations_yes=set(),
            shadow_stations_no=set(),
            db=None,
        )
        yes_cands = [c for c in candidates if c.side == "YES"]
        assert len(yes_cands) == 1
        assert yes_cands[0].shadow is False


# ---------------------------------------------------------------------------
# Scanner env var seeding tests (no DB)
# ---------------------------------------------------------------------------

class TestShadowStationsEnvVars:
    def setup_method(self):
        self.weather = {"KORD": _make_weather_state("KORD")}
        self.yes_market = _make_market(_make_bracket_yes())
        self.no_market = _make_market(_make_bracket_no())

    def test_shadow_stations_yes_env_shadows_yes_only(self):
        """SHADOW_STATIONS_YES=KORD shadows YES; NO side is still live."""
        # First check YES side is shadowed
        yes_candidates = _run_yes_scan(
            self.weather, self.yes_market,

            shadow_stations_yes={"KORD"},
            shadow_stations_no=set(),
            db=None,
        )
        yes_cands = [c for c in yes_candidates if c.side == "YES"]
        assert len(yes_cands) == 1
        assert yes_cands[0].shadow is True

        # Then check NO side is NOT shadowed
        no_candidates = _run_no_scan(
            self.weather, self.no_market,
            shadow_stations_no=set(),
            shadow_stations_yes={"KORD"},
            db=None,
        )
        no_cands = [c for c in no_candidates if c.side == "NO"]
        assert len(no_cands) == 1
        assert no_cands[0].shadow is False

    def test_shadow_stations_no_env_shadows_no_only(self):
        """SHADOW_STATIONS_NO=KORD shadows NO; YES side is still live."""
        # YES side is live
        yes_candidates = _run_yes_scan(
            self.weather, self.yes_market,

            shadow_stations_yes=set(),
            shadow_stations_no={"KORD"},
            db=None,
        )
        yes_cands = [c for c in yes_candidates if c.side == "YES"]
        assert len(yes_cands) == 1
        assert yes_cands[0].shadow is False

        # NO side is shadowed
        no_candidates = _run_no_scan(
            self.weather, self.no_market,
            shadow_stations_no={"KORD"},
            shadow_stations_yes=set(),
            db=None,
        )
        no_cands = [c for c in no_candidates if c.side == "NO"]
        assert len(no_cands) == 1
        assert no_cands[0].shadow is True

    def test_shadow_stations_both_sides_shadows_both(self):
        """SHADOW_STATIONS=KORD shadows both YES and NO."""
        yes_candidates = _run_yes_scan(
            self.weather, self.yes_market,

            shadow_stations={"KORD"},
            db=None,
        )
        yes_cands = [c for c in yes_candidates if c.side == "YES"]
        assert len(yes_cands) == 1
        assert yes_cands[0].shadow is True

        no_candidates = _run_no_scan(
            self.weather, self.no_market,
            shadow_stations={"KORD"},
            db=None,
        )
        no_cands = [c for c in no_candidates if c.side == "NO"]
        assert len(no_cands) == 1
        assert no_cands[0].shadow is True
