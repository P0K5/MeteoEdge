"""Integration tests for GET /api/analysis/{station} (issue #757).

These tests exercise the full stack from the FastAPI router down to a real
SQLite database (Database backed by a temp file, not mocked) seeded with
controlled scan_decisions fixture rows -- the same table the scanner/run.py
poll loop writes to (#756).

What makes these integration tests (vs the unit tests in
src/tests/test_analysis_endpoint.py):
- The database is a real SQLite instance (temp-file Database()) shared across
  connections via TestClient, not an in-memory swap-in.
- Database.get_scan_decisions() is NOT mocked -- it queries the real DB.

As of #757 this endpoint no longer calls get_ensemble_distribution() /
get_bracket_analysis() / the Polymarket API at all -- it is a pure read of
the persisted scan_decisions table -- so this suite no longer needs to mock
get_weather_markets() or seed model_forecast_log/emos_calibration rows.

Stations used:
    KORD  — fully populated (3 seeded brackets + an ensemble summary)
    KMIA  — no recent scan (no scan_decisions rows for the test date)
    ZZZZ  — not in _KNOWN_METARS (triggers 404 at the router level)

Run with:
    pytest -m integration tests/integration/
"""
from __future__ import annotations

import datetime
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_POPULATED_STATION = "KORD"
_EMPTY_STATION = "KMIA"
_UNKNOWN_STATION = "ZZZZ"

# Target date used across all 200-path tests.
_TEST_DATE = "2026-06-30"

# Ensemble summary the scanner would have attached to every bracket snapshot
# for this poll (issue #756's _ensemble_summary()) -- same value repeated on
# every row, demoted into the response's forecast_inputs block.
_ENSEMBLE_MEAN = 56.0
_ENSEMBLE_RANGE_LOW = 54.0
_ENSEMBLE_RANGE_HIGH = 58.0
_ENSEMBLE_MEMBERS = 5

_POLL_TS = "2026-06-30T14:32:05+00:00"

# Three per-bracket scan_decisions rows for KORD on _TEST_DATE, covering a
# below-min-edge rejection, a live trade, and a below-min-price rejection --
# realistic gate_verdict diversity, ascending bracket_low order.
_KORD_BRACKETS = [
    dict(
        ticker="kord-54-56", bracket_low=54.0, bracket_high=56.0,
        yes_ask=30, no_ask=72, p_yes=0.28, raw_p_yes=0.30,
        ev_yes=-3.5, ev_no=5.2, gate_verdict="below_min_edge",
        side=None, gate_actual=5.2, gate_threshold=15.0, gate_unit="cents",
    ),
    dict(
        ticker="kord-56-58", bracket_low=56.0, bracket_high=58.0,
        yes_ask=45, no_ask=57, p_yes=0.52, raw_p_yes=0.50,
        ev_yes=8.9, ev_no=-2.1, gate_verdict="traded_live",
        side="YES", gate_actual=None, gate_threshold=None, gate_unit=None,
        execution_mode="live",
    ),
    dict(
        ticker="kord-58-60", bracket_low=58.0, bracket_high=60.0,
        yes_ask=15, no_ask=87, p_yes=0.12, raw_p_yes=0.10,
        ev_yes=-4.0, ev_no=1.8, gate_verdict="below_min_price",
        side=None, gate_actual=15.0, gate_threshold=20.0, gate_unit="cents",
    ),
]


def _seed_db(db) -> None:
    """Insert scan_decisions fixture rows into *db* for the integration suite.

    Inserts three evaluated brackets for KORD on _TEST_DATE (real Database
    round-trip through Database.upsert_scan_decision -> SQLite -> real
    Database.get_scan_decisions), each carrying the same ensemble summary a
    single poll would attach to every bracket. No rows are inserted for KMIA
    -- it simulates a station with no recent scan (200, empty brackets).
    """
    for b in _KORD_BRACKETS:
        db.upsert_scan_decision(
            ts=_POLL_TS,
            station=_POPULATED_STATION,
            date=_TEST_DATE,
            poll_ts=_POLL_TS,
            current_high=55.0,
            latest_temp=53.0,
            forecast_high=56.0,
            minutes_to_settlement=210.0,
            emos_mode="emos_shadow",
            is_next_day=0,
            gate_detail=None,
            ensemble_mean=_ENSEMBLE_MEAN,
            ensemble_members=_ENSEMBLE_MEMBERS,
            ensemble_range_low=_ENSEMBLE_RANGE_LOW,
            ensemble_range_high=_ENSEMBLE_RANGE_HIGH,
            **b,
        )


@pytest.fixture(scope="module")
def integration_db():
    """Return a real Database() backed by a temp file, seeded with fixture data.

    Uses a temp-file (not :memory:) so multiple connections within TestClient
    share the same rows — sqlite's in-memory DBs are connection-scoped.
    """
    from src.data.db import Database

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    _seed_db(db)
    yield db
    db._conn.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture(scope="module")
def client(integration_db):
    """TestClient wired to the seeded integration_db via set_db()."""
    # Import lazily so conftest.py's DB_PATH has already been set.
    from src.dashboard.api import app, set_db

    set_db(integration_db)
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    # Restore original module-level _db (conftest._restore_api_db handles per-test,
    # but we're module-scoped so do it explicitly here too).
    import src.dashboard.api as _api
    _api._db = integration_db  # keep pointing at our DB until module teardown


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntegration200FullResponse:
    """Happy-path: KORD with three seeded scan_decisions brackets."""

    def test_integration_200_full_response(self, client):
        """Status 200 and all top-level schema fields are present and typed correctly."""
        r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200, r.text
        data = r.json()

        # Required top-level keys (issue #757 contract -- ensemble_mean/range/
        # member_count now live under forecast_inputs; bias_corrected and the
        # raw distribution histogram are dropped entirely).
        required_keys = {
            "station", "date", "is_next_day", "poll_ts",
            "poll_interval_seconds", "forecast_inputs", "brackets",
        }
        missing = required_keys - set(data.keys())
        assert not missing, f"Missing top-level keys: {missing}"
        for dropped in ("ensemble_mean", "bias_corrected", "member_count", "range", "distribution"):
            assert dropped not in data, f"{dropped!r} should no longer be a top-level key"

        # Type checks
        assert data["station"] == _POPULATED_STATION
        assert data["date"] == _TEST_DATE
        assert data["is_next_day"] is False
        assert data["poll_ts"] == _POLL_TS
        assert isinstance(data["poll_interval_seconds"], int) and data["poll_interval_seconds"] > 0
        assert isinstance(data["brackets"], list) and len(data["brackets"]) == len(_KORD_BRACKETS)

        fi = data["forecast_inputs"]
        assert fi is not None
        assert fi["ensemble_mean"] == pytest.approx(_ENSEMBLE_MEAN)
        assert fi["ensemble_range_low"] == pytest.approx(_ENSEMBLE_RANGE_LOW)
        assert fi["ensemble_range_high"] == pytest.approx(_ENSEMBLE_RANGE_HIGH)
        assert fi["ensemble_members"] == _ENSEMBLE_MEMBERS

        # Each bracket must have every locked serve-contract field
        for bracket in data["brackets"]:
            for field in (
                "range", "bracket_low", "bracket_high",
                "market_yes_ask", "market_no_ask", "p_yes", "raw_p_yes",
                "ev_yes", "ev_no", "emos_mode", "forecast_high", "current_high",
                "minutes_to_settlement", "gate_verdict", "side",
                "gate_actual", "gate_threshold", "gate_unit", "gate_detail",
                "execution_mode", "poll_ts",
            ):
                assert field in bracket, f"Bracket missing field: {field}"

    def test_integration_200_member_count_matches_seeded_rows(self, client):
        """forecast_inputs.ensemble_members must equal the seeded member count."""
        r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200
        assert r.json()["forecast_inputs"]["ensemble_members"] == _ENSEMBLE_MEMBERS

    def test_integration_200_brackets_ascending(self, client):
        """Brackets come back ordered ascending by bracket_low (Database.get_scan_decisions)."""
        r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        lows = [b["bracket_low"] for b in r.json()["brackets"]]
        assert lows == sorted(lows)
        assert lows == [b["bracket_low"] for b in _KORD_BRACKETS]


@pytest.mark.integration
class TestIntegration200BracketFieldsPassThrough:
    """Every per-bracket numeric field must round-trip exactly through the real
    SQLite Database (upsert -> get_scan_decisions) and FastAPI/pydantic
    serialization -- this endpoint no longer computes an edge value itself,
    it passes through the numbers the scanner already computed."""

    def test_integration_200_edge_math(self, client):
        r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200
        brackets = {b["bracket_low"]: b for b in r.json()["brackets"]}
        assert len(brackets) == len(_KORD_BRACKETS)

        for seeded in _KORD_BRACKETS:
            b = brackets[seeded["bracket_low"]]
            assert b["market_yes_ask"] == seeded["yes_ask"]
            assert b["market_no_ask"] == seeded["no_ask"]
            assert b["p_yes"] == pytest.approx(seeded["p_yes"])
            assert b["raw_p_yes"] == pytest.approx(seeded["raw_p_yes"])
            assert b["ev_yes"] == pytest.approx(seeded["ev_yes"])
            assert b["ev_no"] == pytest.approx(seeded["ev_no"])
            assert b["gate_verdict"] == seeded["gate_verdict"]
            assert b["side"] == seeded["side"]
            assert b["execution_mode"] == seeded.get("execution_mode", "paper")


@pytest.mark.integration
class TestIntegration404UnknownStation:
    """Requests for a station not in _KNOWN_METARS must return 404."""

    def test_integration_404_unknown_station(self, client):
        r = client.get(f"/api/analysis/{_UNKNOWN_STATION}?date={_TEST_DATE}")
        assert r.status_code == 404, r.text

    def test_integration_404_detail_contains_station(self, client):
        r = client.get(f"/api/analysis/{_UNKNOWN_STATION}?date={_TEST_DATE}")
        detail = r.json().get("detail", "")
        assert _UNKNOWN_STATION in detail


@pytest.mark.integration
class TestIntegration422BadDate:
    """Malformed date strings must yield 422."""

    def test_integration_422_bad_date(self, client):
        r = client.get(f"/api/analysis/{_POPULATED_STATION}?date=not-a-date")
        assert r.status_code == 422, r.text

    def test_integration_422_out_of_range_date(self, client):
        r = client.get(f"/api/analysis/{_POPULATED_STATION}?date=2026-13-99")
        assert r.status_code == 422, r.text

    def test_integration_422_wrong_format(self, client):
        r = client.get(f"/api/analysis/{_POPULATED_STATION}?date=06/30/2026")
        assert r.status_code == 422, r.text


@pytest.mark.integration
class TestIntegration200NoRecentScan:
    """A station with no scan_decisions rows for the date must return 200
    with an empty bracket list (and poll_ts: null) -- issue #757's own AC.
    This replaces the pre-#757 503 "ensemble data unavailable" behavior:
    an empty scan is a normal, expected state (station outside active
    hours, no poll yet today), not a service failure."""

    def test_integration_200_no_recent_scan(self, client):
        """KMIA has no seeded scan_decisions rows — 200 with an empty list, not 503."""
        r = client.get(f"/api/analysis/{_EMPTY_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200, r.text
        data = r.json()
        assert data["brackets"] == []
        assert data["poll_ts"] is None
        assert data["forecast_inputs"] is None

    def test_integration_200_no_recent_scan_echoes_station_and_date(self, client):
        r = client.get(f"/api/analysis/{_EMPTY_STATION}?date={_TEST_DATE}")

        data = r.json()
        assert data["station"] == _EMPTY_STATION
        assert data["date"] == _TEST_DATE


@pytest.mark.integration
class TestIntegrationDateDefaultsToToday:
    """Omitting the date param must default to today (UTC), resolved through
    a real Database.get_scan_decisions() round-trip."""

    def test_integration_date_defaults_to_today(self, client):
        today_iso = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

        # Seed today's data through the real DB so the round-trip is exercised
        # end to end, not just the date-resolution branch.
        import src.dashboard.api as _api

        db = _api._db
        db.upsert_scan_decision(
            ts=f"{today_iso}T00:00:00+00:00", station=_POPULATED_STATION,
            ticker="kord-today", date=today_iso, bracket_low=54.0, bracket_high=56.0,
            gate_verdict="below_min_edge", poll_ts=f"{today_iso}T00:00:00+00:00",
            yes_ask=30, no_ask=72, p_yes=0.28,
        )

        r = client.get(f"/api/analysis/{_POPULATED_STATION}")

        assert r.status_code == 200, r.text
        assert r.json()["date"] == today_iso
        assert len(r.json()["brackets"]) >= 1
