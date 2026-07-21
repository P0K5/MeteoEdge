"""Tests for GET /api/analysis/{station} (issue #757).

Repointed from the parallel get_ensemble_distribution()/get_bracket_analysis()
recompute to the persisted scan_decisions table (#756) -- the numbers the
scanner actually traded on, plus the per-bracket gate verdict.

Covers:
    - 200 with seeded scan_decisions rows: full per-bracket payload incl.
      gate_verdict, side, the gate-tooltip fields, and poll_ts
    - 404 for an unknown station
    - 200 with an empty bracket list when there is no recent scan
    - Today vs D+1 selection partitions correctly on is_next_day
    - poll_interval_seconds is present, sourced from live config
    - forecast_inputs is demoted (served from the persisted snapshot), not a
      live recompute, and bias_corrected is gone entirely
    - 422 for a malformed date string
"""
from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

import src.dashboard.api as dash_api
from src.dashboard.api import app
from src.data.db import Database

STATION = "KORD"


def _mem_db() -> Database:
    return Database(":memory:")


@pytest.fixture
def client_with_db():
    """TestClient with a fresh in-memory Database injected as the module-level _db."""
    db = _mem_db()
    original_db = dash_api._db
    dash_api._db = db
    yield TestClient(app), db
    dash_api._db = original_db


def _seed_row(db: Database, **overrides) -> None:
    defaults = dict(
        ts="2026-07-21T14:32:05+00:00",
        station=STATION,
        ticker="0xabc",
        date="2026-07-21",
        bracket_low=68.0,
        bracket_high=70.0,
        gate_verdict="below_min_edge",
        poll_ts="2026-07-21T14:32:05+00:00",
        side=None,
        yes_ask=58,
        no_ask=45,
        current_high=68.0,
        latest_temp=65.0,
        forecast_high=71.0,
        p_yes=0.41,
        raw_p_yes=0.39,
        capped_p_yes=0.41,
        ev_yes=-3.2,
        ev_no=1.1,
        minutes_to_settlement=210.0,
        emos_mode="emos_shadow",
        is_next_day=0,
        gate_actual=3.1,
        gate_threshold=15.0,
        gate_unit="cents",
        gate_detail=None,
        ensemble_mean=69.5,
        ensemble_members=6,
        ensemble_range_low=66.0,
        ensemble_range_high=73.0,
    )
    defaults.update(overrides)
    db.upsert_scan_decision(**defaults)


# ---------------------------------------------------------------------------
# 200 — happy path, seeded scan_decisions rows
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint200:
    def test_status_code(self, client_with_db):
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.status_code == 200

    def test_top_level_schema(self, client_with_db):
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        data = r.json()
        for key in ("station", "date", "is_next_day", "poll_ts",
                    "poll_interval_seconds", "forecast_inputs", "brackets"):
            assert key in data, f"Missing top-level key: {key}"

    def test_bias_corrected_dropped(self, client_with_db):
        """The Bias-Corrected field is dropped entirely (issue #757 AC)."""
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert "bias_corrected" not in r.json()

    def test_distribution_dropped(self, client_with_db):
        """The raw per-degree distribution (old KPI chart) is not part of the
        locked serve-contract; only the summarised forecast_inputs block is."""
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert "distribution" not in r.json()

    def test_station_uppercased(self, client_with_db):
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION.lower()}?date=2026-07-21")
        assert r.json()["station"] == STATION

    def test_date_echoed(self, client_with_db):
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.json()["date"] == "2026-07-21"

    def test_poll_ts_returned(self, client_with_db):
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.json()["poll_ts"] == "2026-07-21T14:32:05+00:00"

    def test_poll_interval_seconds_present_and_int(self, client_with_db):
        """Sourced from src.config.POLL_INTERVAL_SECONDS via get_live_config()
        (live-read, same pattern as GET /api/config) -- frontend uses 2x this
        for the stale-scan pill."""
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert isinstance(r.json()["poll_interval_seconds"], int)
        assert r.json()["poll_interval_seconds"] > 0

    def test_bracket_schema_full_contract(self, client_with_db):
        """Every locked serve-contract field is present on each bracket row."""
        client, db = client_with_db
        _seed_row(db)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        bracket = r.json()["brackets"][0]
        for field in (
            "range", "bracket_low", "bracket_high",
            "market_yes_ask", "market_no_ask",
            "p_yes", "raw_p_yes", "ev_yes", "ev_no", "emos_mode",
            "forecast_high", "current_high", "minutes_to_settlement",
            "gate_verdict", "side",
            "gate_actual", "gate_threshold", "gate_unit", "gate_detail",
            "poll_ts",
        ):
            assert field in bracket, f"Missing bracket field: {field}"

    def test_bracket_values_pass_through(self, client_with_db):
        client, db = client_with_db
        _seed_row(db, side="NO", gate_verdict="entry_guard",
                   gate_detail="duplicate-entry guard: open position already exists")
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        bracket = r.json()["brackets"][0]
        assert bracket["market_yes_ask"] == 58
        assert bracket["market_no_ask"] == 45
        assert bracket["p_yes"] == pytest.approx(0.41)
        assert bracket["raw_p_yes"] == pytest.approx(0.39)
        assert bracket["ev_yes"] == pytest.approx(-3.2)
        assert bracket["ev_no"] == pytest.approx(1.1)
        assert bracket["emos_mode"] == "emos_shadow"
        assert bracket["forecast_high"] == pytest.approx(71.0)
        assert bracket["current_high"] == pytest.approx(68.0)
        assert bracket["minutes_to_settlement"] == pytest.approx(210.0)
        assert bracket["side"] == "NO"
        assert bracket["gate_verdict"] == "entry_guard"
        assert bracket["gate_detail"] == "duplicate-entry guard: open position already exists"
        assert bracket["poll_ts"] == "2026-07-21T14:32:05+00:00"

    def test_gate_tooltip_fields_pass_through(self, client_with_db):
        client, db = client_with_db
        _seed_row(db, gate_verdict="below_min_edge", gate_actual=3.1,
                   gate_threshold=15.0, gate_unit="cents")
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        bracket = r.json()["brackets"][0]
        assert bracket["gate_actual"] == pytest.approx(3.1)
        assert bracket["gate_threshold"] == pytest.approx(15.0)
        assert bracket["gate_unit"] == "cents"

    def test_range_label_formatted(self, client_with_db):
        client, db = client_with_db
        _seed_row(db, bracket_low=68.0, bracket_high=70.0)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.json()["brackets"][0]["range"] == "68–70°F"

    def test_multiple_brackets_ordered_ascending(self, client_with_db):
        client, db = client_with_db
        _seed_row(db, ticker="0xhigh", bracket_low=74.0, bracket_high=76.0)
        _seed_row(db, ticker="0xlow", bracket_low=68.0, bracket_high=70.0)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        lows = [b["bracket_low"] for b in r.json()["brackets"]]
        assert lows == sorted(lows)

    def test_forecast_inputs_demoted_from_persisted_snapshot(self, client_with_db):
        """forecast_inputs is served from the scan_decisions row's own
        ensemble_* columns (persisted by the scanner at poll time), never a
        live get_ensemble_distribution() recompute -- the demote-not-retire
        decision for issue #757."""
        client, db = client_with_db
        _seed_row(db, ensemble_mean=69.5, ensemble_members=6,
                   ensemble_range_low=66.0, ensemble_range_high=73.0)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        fi = r.json()["forecast_inputs"]
        assert fi is not None
        assert fi["ensemble_mean"] == pytest.approx(69.5)
        assert fi["ensemble_members"] == 6
        assert fi["ensemble_range_low"] == pytest.approx(66.0)
        assert fi["ensemble_range_high"] == pytest.approx(73.0)

    def test_forecast_inputs_none_when_no_ensemble_data(self, client_with_db):
        client, db = client_with_db
        _seed_row(db, ensemble_mean=None, ensemble_members=None,
                   ensemble_range_low=None, ensemble_range_high=None)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.json()["forecast_inputs"] is None


# ---------------------------------------------------------------------------
# 404 — unknown station
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint404:
    def test_unknown_station_returns_404(self, client_with_db):
        client, _db = client_with_db
        r = client.get("/api/analysis/ZZZZ?date=2026-07-21")
        assert r.status_code == 404

    def test_unknown_station_detail(self, client_with_db):
        client, _db = client_with_db
        r = client.get("/api/analysis/ZZZZ?date=2026-07-21")
        assert "ZZZZ" in r.json().get("detail", "")


# ---------------------------------------------------------------------------
# 200 — no recent scan (empty)
# ---------------------------------------------------------------------------

class TestAnalysisEndpointEmpty:
    def test_no_scan_returns_200_empty(self, client_with_db):
        client, _db = client_with_db
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.status_code == 200
        data = r.json()
        assert data["brackets"] == []

    def test_no_scan_poll_ts_none(self, client_with_db):
        client, _db = client_with_db
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.json()["poll_ts"] is None

    def test_no_scan_forecast_inputs_none(self, client_with_db):
        client, _db = client_with_db
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.json()["forecast_inputs"] is None

    def test_data_for_other_date_does_not_leak(self, client_with_db):
        """A scan exists for a different date -- querying today's date must
        still come back empty, not show yesterday's stale rows."""
        client, db = client_with_db
        _seed_row(db, date="2026-07-20", poll_ts="2026-07-20T10:00:00+00:00")
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21")
        assert r.json()["brackets"] == []


# ---------------------------------------------------------------------------
# Today vs D+1 partition (is_next_day)
# ---------------------------------------------------------------------------

class TestAnalysisEndpointDayPartition:
    def test_today_returns_same_day_rows_only(self, client_with_db):
        client, db = client_with_db
        _seed_row(db, ticker="0xtoday", date="2026-07-21", is_next_day=0)
        _seed_row(db, ticker="0xnextday", date="2026-07-22", is_next_day=1)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21&next_day=false")
        data = r.json()
        assert data["is_next_day"] is False
        assert len(data["brackets"]) == 1
        assert data["brackets"][0]["gate_verdict"] == "below_min_edge"

    def test_next_day_returns_next_day_rows_only(self, client_with_db):
        client, db = client_with_db
        _seed_row(db, ticker="0xtoday", date="2026-07-21", is_next_day=0)
        _seed_row(db, ticker="0xnextday", date="2026-07-22", is_next_day=1,
                   gate_verdict="next_day_shadow")
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-22&next_day=true")
        data = r.json()
        assert data["is_next_day"] is True
        assert len(data["brackets"]) == 1
        assert data["brackets"][0]["gate_verdict"] == "next_day_shadow"

    def test_next_day_without_explicit_date_defaults_to_tomorrow(self, client_with_db):
        client, db = client_with_db
        today = datetime.datetime.now(datetime.timezone.utc).date()
        tomorrow = today + datetime.timedelta(days=1)
        _seed_row(db, ticker="0xnextday", date=tomorrow.isoformat(), is_next_day=1,
                   gate_verdict="next_day_shadow")
        r = client.get(f"/api/analysis/{STATION}?next_day=true")
        data = r.json()
        assert data["date"] == tomorrow.isoformat()
        assert len(data["brackets"]) == 1

    def test_mismatched_is_next_day_for_date_filters_to_empty(self, client_with_db):
        """A row's is_next_day column disagrees with the requested selector
        (defensive belt-and-braces filter, see the endpoint docstring)."""
        client, db = client_with_db
        _seed_row(db, ticker="0xweird", date="2026-07-21", is_next_day=1)
        r = client.get(f"/api/analysis/{STATION}?date=2026-07-21&next_day=false")
        assert r.json()["brackets"] == []


# ---------------------------------------------------------------------------
# 422 — malformed date
# ---------------------------------------------------------------------------

class TestAnalysisEndpoint422:
    def test_malformed_date_returns_422(self, client_with_db):
        client, _db = client_with_db
        r = client.get(f"/api/analysis/{STATION}?date=not-a-date")
        assert r.status_code == 422

    def test_partial_date_returns_422(self, client_with_db):
        client, _db = client_with_db
        r = client.get(f"/api/analysis/{STATION}?date=2026-13-99")
        assert r.status_code == 422

    def test_wrong_format_returns_422(self, client_with_db):
        client, _db = client_with_db
        r = client.get(f"/api/analysis/{STATION}?date=06/29/2026")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# date defaults to today (UTC) when omitted
# ---------------------------------------------------------------------------

class TestAnalysisEndpointDateDefault:
    def test_omit_date_defaults_to_today(self, client_with_db):
        client, db = client_with_db
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        _seed_row(db, date=today)
        r = client.get(f"/api/analysis/{STATION}")
        assert r.status_code == 200
        assert r.json()["date"] == today
        assert len(r.json()["brackets"]) == 1
