"""Tests for TAF disruption injection into scan_markets() — issue #104.

Verifies that:
- scan_markets() accepts an optional db parameter
- when db is None, taf_disruption defaults to False and confidence is unchanged
- when db has a TEMPO/TS window for the city, taf_disruption=True is set
- TAF_DISRUPTION_CONFIDENCE_FACTOR (env var, default 0.85) scales confidence
- taf_disruption=False leaves confidence unchanged
- the Candidate dataclass has taf_disruption field defaulting to False
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.data.db import Database
from src.model.envelope import Bracket, WeatherState
from src.strategy.scanner import Candidate, scan_markets


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_state(station: str = "KMIA") -> WeatherState:
    """Minimal WeatherState that the envelope model can score."""
    now = datetime.now(timezone.utc)
    return WeatherState(
        station=station,
        now_local=now,
        sunset_local=now.replace(hour=23, minute=59),
        current_high_f=82.0,
        current_high_time=now,
        latest_temp_f=80.0,
        latest_temp_time=now,
        forecast_high_f=85.0,
    )


def _make_market(
    question: str = "Will the highest temperature in Miami be 82-84°F on some date?",
    group_title: str = "82-84°F",
    end_date: str | None = None,
    condition_id: str = "0xcond01",
    yes_price: str = "0.80",
    no_price: str = "0.20",
) -> dict:
    """Minimal Polymarket market dict (today's settlement date)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:00+00:00")
    return {
        "conditionId": condition_id,
        "question": question,
        "groupItemTitle": group_title,
        "outcomes": '["Yes","No"]',
        "outcomePrices": f'["{yes_price}","{no_price}"]',
        "clobTokenIds": '["tok_yes","tok_no"]',
        "endDate": end_date or today,
    }


def _insert_taf_window(db: Database, city: str = "Miami", group_type: str = "Temporary Fluctuation", sig_wx: str = "TS") -> None:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    # valid_from is 5 min ahead of now — falls within [peak_start, peak_end] for any
    # market with mins_left > 5, without relying on the market closing hours away.
    db.insert_taf_window({
        "city": city,
        "issued_at": now.isoformat(),
        "valid_from": (now + timedelta(minutes=5)).isoformat(),
        "valid_to": (now + timedelta(hours=3)).isoformat(),
        "group_type": group_type,
        "temp": None,
        "wind_kt": None,
        "sig_wx": sig_wx,
        "raw_text": "",
    })


# ---------------------------------------------------------------------------
# Candidate dataclass
# ---------------------------------------------------------------------------

class TestCandidateDataclass:
    def test_taf_disruption_defaults_to_false(self):
        """Candidate.taf_disruption defaults to False when not specified."""
        from src.model.envelope import Bracket
        b = Bracket(
            ticker="0xtest", low_f=80.0, high_f=84.0,
            yes_ask_cents=80, yes_ask_size=100,
            no_ask_cents=20, no_ask_size=100,
        )
        now = datetime.now(timezone.utc)
        state = _make_state()
        c = Candidate(
            station="KMIA", bracket=b, side="YES",
            edge_cents=5.0, price_cents=80,
            confidence=0.80, p_yes=0.80,
            ev_yes=5.0, ev_no=-10.0,
            minutes_to_settlement=120.0, market={},
        )
        assert c.taf_disruption is False

    def test_taf_disruption_can_be_set_true(self):
        """Candidate.taf_disruption can be explicitly set."""
        from src.model.envelope import Bracket
        b = Bracket(
            ticker="0xtest", low_f=80.0, high_f=84.0,
            yes_ask_cents=80, yes_ask_size=100,
            no_ask_cents=20, no_ask_size=100,
        )
        c = Candidate(
            station="KMIA", bracket=b, side="YES",
            edge_cents=5.0, price_cents=80,
            confidence=0.80, p_yes=0.80,
            ev_yes=5.0, ev_no=-10.0,
            minutes_to_settlement=120.0, market={},
            taf_disruption=True,
        )
        assert c.taf_disruption is True


# ---------------------------------------------------------------------------
# scan_markets — no db (backward-compatible call)
# ---------------------------------------------------------------------------

def _scan_patched(weather, markets, db=None, extra_patches=None):
    """Run scan_markets with permissive config so candidates are generated."""
    kw = dict(
        MIN_EDGE_CENTS=1,
        MAX_EDGE_CENTS=9999,
        ENABLE_YES_TRADES=True,
        MIN_CONFIDENCE_YES=0.0,
        MAX_CONFIDENCE_YES_FOR_NO=1.0,
        MIN_PRICE_CENTS=1,
        MIN_MINUTES_TO_SETTLEMENT=0,
        MIN_FORECAST_BRACKET_MARGIN_F=0.0,
        DISABLED_STATIONS=set(),
    )
    if extra_patches:
        kw.update(extra_patches)
    with patch.multiple("src.strategy.scanner", **kw):
        return scan_markets(weather, markets, db=db)


class TestScanMarketsNoDb:
    def test_no_db_returns_candidates_with_taf_false(self):
        """Without db arg, candidates have taf_disruption=False."""
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market])
        assert len(candidates) > 0, "Expected at least one candidate"
        for c in candidates:
            assert c.taf_disruption is False

    def test_no_db_confidence_unchanged(self):
        """Without db arg, confidence is not scaled by TAF factor."""
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market])
        # confidence should be the raw p_yes (no scaling applied)
        for c in candidates:
            if c.side == "YES":
                assert c.confidence == pytest.approx(c.p_yes, abs=0.01)
            else:
                assert c.confidence == pytest.approx(1 - c.p_yes, abs=0.01)


# ---------------------------------------------------------------------------
# scan_markets — with db, no TAF data
# ---------------------------------------------------------------------------

class TestScanMarketsDbNoTaf:
    def test_no_taf_data_taf_disruption_false(self):
        """DB with no TAF windows → taf_disruption=False on candidate."""
        db = Database(":memory:")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market], db=db)
        assert len(candidates) > 0
        for c in candidates:
            assert c.taf_disruption is False

    def test_fm_window_with_ts_not_disruption(self):
        """FM window (Definite Change) with TS → taf_disruption=False."""
        db = Database(":memory:")
        _insert_taf_window(db, city="Miami", group_type="Definite Change", sig_wx="TS")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market], db=db)
        assert len(candidates) > 0
        for c in candidates:
            assert c.taf_disruption is False


# ---------------------------------------------------------------------------
# scan_markets — with db, TAF disruption present
# ---------------------------------------------------------------------------

class TestScanMarketsDbWithTaf:
    def test_tempo_ts_sets_taf_disruption_true(self):
        """TEMPO window with TS → taf_disruption=True on candidate."""
        db = Database(":memory:")
        _insert_taf_window(db, city="Miami", group_type="Temporary Fluctuation", sig_wx="TS")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market], db=db)
        assert len(candidates) > 0, "Expected at least one candidate"
        assert any(c.taf_disruption is True for c in candidates), \
            "Expected at least one candidate with taf_disruption=True"

    def test_tempo_sh_sets_taf_disruption_true(self):
        """TEMPO window with SH → taf_disruption=True."""
        db = Database(":memory:")
        _insert_taf_window(db, city="Miami", group_type="Temporary Fluctuation", sig_wx="SH")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market], db=db)
        assert len(candidates) > 0
        assert any(c.taf_disruption is True for c in candidates)

    def test_confidence_scaled_by_default_factor(self):
        """TAF disruption with default factor (0.85) scales confidence."""
        db = Database(":memory:")
        _insert_taf_window(db, city="Miami", group_type="Temporary Fluctuation", sig_wx="TS")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        os.environ.pop("TAF_DISRUPTION_CONFIDENCE_FACTOR", None)
        candidates, _ = _scan_patched(weather, [market], db=db)
        disrupted = [c for c in candidates if c.taf_disruption]
        assert len(disrupted) > 0, "Expected disrupted candidates"
        for c in disrupted:
            raw_p = c.p_yes if c.side == "YES" else 1 - c.p_yes
            assert c.confidence == pytest.approx(raw_p * 0.85, abs=0.001)

    def test_confidence_scaled_by_custom_factor(self):
        """TAF_DISRUPTION_CONFIDENCE_FACTOR env var controls the multiplier."""
        db = Database(":memory:")
        _insert_taf_window(db, city="Miami", group_type="Temporary Fluctuation", sig_wx="TS")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        with patch.dict(os.environ, {"TAF_DISRUPTION_CONFIDENCE_FACTOR": "0.70"}):
            candidates, _ = _scan_patched(weather, [market], db=db)
        disrupted = [c for c in candidates if c.taf_disruption]
        assert len(disrupted) > 0, "Expected disrupted candidates"
        for c in disrupted:
            raw_p = c.p_yes if c.side == "YES" else 1 - c.p_yes
            assert c.confidence == pytest.approx(raw_p * 0.70, abs=0.001)

    def test_no_disruption_confidence_unchanged(self):
        """Without TAF disruption, confidence equals raw p_yes/1-p_yes (no scaling)."""
        db = Database(":memory:")
        _insert_taf_window(db, city="Miami", group_type="Definite Change", sig_wx="TS")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market], db=db)
        assert len(candidates) > 0
        for c in candidates:
            assert c.taf_disruption is False
            raw_p = c.p_yes if c.side == "YES" else 1 - c.p_yes
            assert c.confidence == pytest.approx(raw_p, abs=0.001)

    def test_wrong_city_taf_window_not_disruption(self):
        """TAF window for Chicago does not affect Miami candidate."""
        db = Database(":memory:")
        _insert_taf_window(db, city="Chicago", group_type="Temporary Fluctuation", sig_wx="TS")
        weather = {"KMIA": _make_state("KMIA")}
        market = _make_market()
        candidates, _ = _scan_patched(weather, [market], db=db)
        assert len(candidates) > 0
        for c in candidates:
            assert c.taf_disruption is False
