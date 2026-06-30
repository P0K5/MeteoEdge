"""Integration tests for GET /api/analysis/{station} (issue #523).

These tests exercise the full stack from the FastAPI router through the service
layer (get_ensemble_distribution, get_bracket_analysis) down to a real SQLite
database seeded with controlled fixture data.

What makes these integration tests (vs the unit tests in src/tests/test_analysis_endpoint.py):
- The database is a real SQLite instance (Database(":memory:")) with seeded rows.
- get_ensemble_distribution() is NOT mocked — it queries the real DB.
- get_bracket_analysis()'s bracket math runs for real; only the external
  Polymarket HTTP call (get_weather_markets) is mocked to avoid network access.

Stations used:
    KORD  — fully populated (5 ensemble members, 3 Polymarket brackets)
    KMIA  — missing ensemble data (no model_forecast_log rows)
    ZZZZ  — not in _KNOWN_METARS (triggers 404 at the router level)

Run with:
    pytest -m integration tests/integration/
"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# One known station with full data, one known station without ensemble data.
_POPULATED_STATION = "KORD"
_EMPTY_STATION = "KMIA"
_UNKNOWN_STATION = "ZZZZ"

# Target date used across all 200-path tests.
_TEST_DATE = "2026-06-30"
_TEST_DATE_OBJ = datetime.date(2026, 6, 30)

# Five ensemble members for KORD on _TEST_DATE — covers models in the active stack
# ("nws", "open_meteo") and extras so member_count=5.
_KORD_MEMBERS = [
    ("nws", 55.0),
    ("open_meteo", 56.0),
    ("gfs", 57.0),
    ("hrrr", 54.0),
    ("ecmwf", 58.0),
]

# Three Polymarket bracket dicts for KORD covering the temperature range of the
# seeded ensemble.  Encoded as outcomePrices JSON strings to match Polymarket's
# wire format.  parse_bracket_from_market and is_highest_temp_market are called
# for real, so the title must look like a real Polymarket market title.
def _make_market(conditionId: str, group_title: str, question: str,
                 yes_price: str, end_date: str) -> dict:
    """Build a minimal Polymarket market dict for integration fixtures.

    - ``conditionId`` is required by parse_bracket_from_market.
    - ``question`` must contain "highest temperature in chicago" for KORD to match
      is_highest_temp_market() (which does a case-insensitive substring search
      against POLYMARKET_CITY_TO_STATION keys derived from STATIONS city names).
    - ``groupItemTitle`` carries the bracket label parsed by parse_bracket_from_market.
    """
    return {
        "conditionId": conditionId,
        "endDate": end_date,
        "groupItemTitle": group_title,
        "question": question,
        "outcomePrices": json.dumps([yes_price, str(round(1.0 - float(yes_price), 2))]),
        "outcomes": json.dumps(["Yes", "No"]),
    }


# "highest temperature in chicago" triggers is_highest_temp_market → KORD.
# Bracket labels use the _LABEL_RANGE_DASH regex: "54–56°F" (en-dash + unit).
_KORD_MARKETS = [
    _make_market(
        "kord-54-56",
        "54-56°F",
        f"Will the highest temperature in Chicago be 54-56°F on {_TEST_DATE}?",
        "0.30",
        f"{_TEST_DATE}T23:59:00Z",
    ),
    _make_market(
        "kord-56-58",
        "56-58°F",
        f"Will the highest temperature in Chicago be 56-58°F on {_TEST_DATE}?",
        "0.45",
        f"{_TEST_DATE}T23:59:00Z",
    ),
    _make_market(
        "kord-58-60",
        "58-60°F",
        f"Will the highest temperature in Chicago be 58-60°F on {_TEST_DATE}?",
        "0.15",
        f"{_TEST_DATE}T23:59:00Z",
    ),
]


def _seed_db(db) -> None:
    """Insert fixture rows into *db* for the integration test suite.

    Inserts:
    - 5 model_forecast_log rows for KORD on _TEST_DATE (one per model).
    - No rows for KMIA (simulates missing ensemble data → 503).
    - An emos_calibration row for Chicago so bias_corrected != None.
    - A bot_config row fixing FORECAST_STACK to "baseline".
    """
    now = "2026-06-30T00:00:00"

    # Ensemble members for KORD
    for model, forecast_high_f in _KORD_MEMBERS:
        db._conn.execute(
            "INSERT INTO model_forecast_log "
            "(station, model, date, forecast_high_f, logged_at, lead_hours) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_POPULATED_STATION, model, _TEST_DATE, forecast_high_f, now, 24),
        )

    # EMOS calibration for Chicago.
    # Using identity coefficients (a=0, b=1, c=0, d=1) so bias_corrected == ensemble_mean.
    # model_mode must match the query in get_all_emos_calibration / _emos_bias_correct:
    # it looks for forecast_source='nws_open_meteo'; model_mode is stored but not
    # used for the lookup, so any non-empty value is fine.
    db._conn.execute(
        "INSERT OR IGNORE INTO emos_calibration "
        "(city, model_mode, forecast_source, a, b, c, d, trained_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("Chicago", "standard", "nws_open_meteo", 0.0, 1.0, 0.0, 1.0, now),
    )

    # Fix FORECAST_STACK so tests are deterministic regardless of bot_config state
    db._conn.execute(
        "INSERT OR REPLACE INTO bot_config(key, value, updated_at) VALUES (?, ?, ?)",
        ("FORECAST_STACK", "baseline", now),
    )

    db._conn.commit()


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
# Helper — a stable mock for get_weather_markets so Polymarket is never called.
# Used by every test that hits a 200 path.
# ---------------------------------------------------------------------------

def _mock_markets():
    """Return the pre-built market list for KORD."""
    return list(_KORD_MARKETS)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntegration200FullResponse:
    """Happy-path: KORD with full ensemble + bracket data."""

    def test_integration_200_full_response(self, client):
        """Status 200 and all top-level schema fields are present and typed correctly."""
        with patch("src.model.bracket_analysis.get_weather_markets", side_effect=_mock_markets):
            r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200, r.text
        data = r.json()

        # Required top-level keys
        required_keys = {
            "station", "date", "ensemble_mean", "bias_corrected",
            "member_count", "range", "distribution", "brackets",
        }
        missing = required_keys - set(data.keys())
        assert not missing, f"Missing top-level keys: {missing}"

        # Type checks
        assert data["station"] == _POPULATED_STATION
        assert data["date"] == _TEST_DATE
        assert isinstance(data["ensemble_mean"], float)
        assert isinstance(data["bias_corrected"], float)
        assert isinstance(data["member_count"], int) and data["member_count"] > 0
        assert isinstance(data["range"], list) and len(data["range"]) == 2
        assert isinstance(data["distribution"], dict) and len(data["distribution"]) > 0
        assert isinstance(data["brackets"], list) and len(data["brackets"]) > 0

        # Each bracket must have all required fields
        for bracket in data["brackets"]:
            for field in ("range", "bracket_low", "bracket_high",
                          "polymarket_prob", "model_prob", "edge"):
                assert field in bracket, f"Bracket missing field: {field}"

    def test_integration_200_member_count_matches_seeded_rows(self, client):
        """member_count must equal the number of distinct models seeded for KORD."""
        with patch("src.model.bracket_analysis.get_weather_markets", side_effect=_mock_markets):
            r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200
        assert r.json()["member_count"] == len(_KORD_MEMBERS)


@pytest.mark.integration
class TestIntegration200DistributionSumsToMemberCount:
    """distribution values must sum to member_count."""

    def test_integration_200_distribution_sums_to_member_count(self, client):
        with patch("src.model.bracket_analysis.get_weather_markets", side_effect=_mock_markets):
            r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200
        data = r.json()
        dist_sum = sum(data["distribution"].values())
        assert dist_sum == data["member_count"], (
            f"sum(distribution.values())={dist_sum} != member_count={data['member_count']}"
        )


@pytest.mark.integration
class TestIntegration200EdgeMath:
    """For each bracket with both model_prob and polymarket_prob, verify edge math."""

    def test_integration_200_edge_math(self, client):
        with patch("src.model.bracket_analysis.get_weather_markets", side_effect=_mock_markets):
            r = client.get(f"/api/analysis/{_POPULATED_STATION}?date={_TEST_DATE}")

        assert r.status_code == 200
        brackets = r.json()["brackets"]

        computable = [
            b for b in brackets
            if b["model_prob"] is not None and b["polymarket_prob"] is not None
        ]
        assert computable, "Expected at least one bracket with both model_prob and polymarket_prob"

        for b in computable:
            expected_edge = b["model_prob"] - b["polymarket_prob"]
            assert abs(b["edge"] - expected_edge) < 0.01, (
                f"edge mismatch for bracket {b['range']}: "
                f"got {b['edge']}, expected {expected_edge}"
            )


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
class TestIntegration503NoEnsembleData:
    """A station with no model_forecast_log rows must return 503."""

    def test_integration_503_no_ensemble_data(self, client):
        """KMIA has no seeded ensemble rows — the service returns None → 503."""
        with patch("src.model.bracket_analysis.get_weather_markets", side_effect=_mock_markets):
            r = client.get(f"/api/analysis/{_EMPTY_STATION}?date={_TEST_DATE}")

        assert r.status_code == 503, r.text

    def test_integration_503_error_body(self, client):
        """503 detail must include error='ensemble data unavailable' and station/date."""
        with patch("src.model.bracket_analysis.get_weather_markets", side_effect=_mock_markets):
            r = client.get(f"/api/analysis/{_EMPTY_STATION}?date={_TEST_DATE}")

        detail = r.json().get("detail", {})
        assert detail.get("error") == "ensemble data unavailable"
        assert detail.get("station") == _EMPTY_STATION
        assert detail.get("date") == _TEST_DATE


@pytest.mark.integration
class TestIntegrationDateDefaultsToToday:
    """Omitting the date param must default to today (UTC)."""

    def test_integration_date_defaults_to_today(self, client):
        today_iso = datetime.datetime.now(datetime.timezone.utc).date().isoformat()

        # Seed today's data so the endpoint can return 200 (not 503).
        from src.data.db import Database
        import src.dashboard.api as _api

        db = _api._db
        try:
            db._conn.execute(
                "INSERT OR IGNORE INTO model_forecast_log "
                "(station, model, date, forecast_high_f, logged_at, lead_hours) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_POPULATED_STATION, "nws", today_iso, 55.0,
                 "2026-06-30T00:00:00", 24),
            )
            db._conn.commit()
        except Exception:
            pass  # If today == _TEST_DATE the row already exists; that's fine.

        with patch("src.model.bracket_analysis.get_weather_markets", return_value=[]):
            r = client.get(f"/api/analysis/{_POPULATED_STATION}")

        # May be 200 (today's data exists) or 503 (no data for today).
        # Either way, the date in a 200 response must be today.
        if r.status_code == 200:
            assert r.json()["date"] == today_iso
        else:
            # 503 detail must also echo today's date
            assert r.status_code == 503
            detail = r.json().get("detail", {})
            assert detail.get("date") == today_iso
