"""Tests for src/data/taf_disruption.py — check_taf_disruption function."""
from __future__ import annotations

import pytest

from src.data.db import Database
from src.data.taf_disruption import check_taf_disruption


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _db() -> Database:
    db = Database(":memory:")
    return db


def _insert_window(db: Database, **kwargs) -> None:
    defaults = {
        "city": "Tokyo",
        "issued_at": "2026-06-07T17:00:00+00:00",
        "valid_from": "2026-06-07T18:00:00+00:00",
        "valid_to": "2026-06-07T22:00:00+00:00",
        "group_type": "Temporary Fluctuation",
        "temp": None,
        "wind_kt": None,
        "sig_wx": "TS",
        "raw_text": "",
    }
    defaults.update(kwargs)
    db.insert_taf_window(defaults)


# Peak window used across most tests
_PEAK_START = "2026-06-07T18:00:00+00:00"
_PEAK_END   = "2026-06-07T22:00:00+00:00"


# ---------------------------------------------------------------------------
# True cases
# ---------------------------------------------------------------------------

class TestTrueCases:
    def test_tempo_ts_overlapping_peak_returns_true(self):
        """TEMPO window with TS overlapping peak → True."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx="TS")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is True

    def test_tempo_sh_returns_true(self):
        """TEMPO window with SH overlapping peak → True."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx="SH")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is True

    def test_prob30_fg_overlapping_peak_returns_true(self):
        """PROB30 TEMPO window (group_type='Temporary Fluctuation') with FG → True."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx="FG BR")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is True

    def test_multiple_codes_any_match_returns_true(self):
        """sig_wx with multiple codes including TS → True."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx="TS SH RA")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is True


# ---------------------------------------------------------------------------
# False cases — wrong group type
# ---------------------------------------------------------------------------

class TestFalseCasesWrongGroupType:
    def test_fm_window_with_ts_returns_false(self):
        """FM window (Definite Change) with TS → False (wrong group type)."""
        db = _db()
        _insert_window(db, group_type="Definite Change", sig_wx="TS")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False

    def test_becmg_window_returns_false(self):
        """BECMG window (Gradual Transition) with TS → False."""
        db = _db()
        _insert_window(db, group_type="Gradual Transition", sig_wx="TS")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False

    def test_base_period_returns_false(self):
        """Base Period window with TS → False."""
        db = _db()
        _insert_window(db, group_type="Base Period", sig_wx="TS SH")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False


# ---------------------------------------------------------------------------
# False cases — sig_wx mismatch
# ---------------------------------------------------------------------------

class TestFalseCasesSigWxMismatch:
    def test_tempo_ra_only_returns_false(self):
        """TEMPO window with only RA (not TS/SH/FG) → False."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx="RA")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False

    def test_tempo_shra_no_disruption_codes_returns_false(self):
        """SHRA is not in the disruption code set — returns False."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx="SHRA BR")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False

    def test_tempo_no_sig_wx_returns_false(self):
        """TEMPO window with sig_wx=None → False."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx=None)
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False

    def test_tempo_empty_sig_wx_returns_false(self):
        """TEMPO window with sig_wx='' → False."""
        db = _db()
        _insert_window(db, group_type="Temporary Fluctuation", sig_wx="")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False


# ---------------------------------------------------------------------------
# False cases — no data
# ---------------------------------------------------------------------------

class TestFalseCasesNoData:
    def test_no_taf_data_returns_false(self):
        """Empty taf_windows → False (fail-open)."""
        db = _db()
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False

    def test_wrong_city_returns_false(self):
        """Window exists for different city → False."""
        db = _db()
        _insert_window(db, city="Seoul", group_type="Temporary Fluctuation", sig_wx="TS")
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False

    def test_window_outside_peak_returns_false(self):
        """Window valid_from is outside [peak_start, peak_end] → False."""
        db = _db()
        _insert_window(
            db,
            valid_from="2026-06-07T23:00:00+00:00",  # after peak_end
            group_type="Temporary Fluctuation",
            sig_wx="TS",
        )
        assert check_taf_disruption("Tokyo", db, _PEAK_START, _PEAK_END) is False
