"""Tests for src/data/taf_parser.py — TafParser class.

Covers:
- Multi-group TAF (FM + TEMPO + BECMG + PROB30 TEMPO)
- CAVOK handling: sig_wx=None, temp=None
- Missing temperature: temp=None without crash
- JSON-wrapped input (aviationweather.gov format)
- Empty / unrecognisable input
- ISO 8601 UTC format for issued_at, valid_from, valid_to
- All group_type labels
- Overlapping TEMPO and PROB groups both returned
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from src.data.taf_parser import TafParser

# ---------------------------------------------------------------------------
# Sample TAF strings
# ---------------------------------------------------------------------------

FULL_TAF = """\
TAF
RJTT 071700Z 0718/0824 12010KT 9999 FEW020
  TEMPO 0718/0722 4000 TSRA FEW010 BKN020CB
  FM072200 15015KT 9999 SCT030
  BECMG 0800/0802 VRB03KT 9999 FEW030
  PROB30 TEMPO 0806/0812 2000 FG
"""

CAVOK_TAF = "TAF WSSS 071200Z 0712/0818 VRB03KT CAVOK"

NO_TEMP_TAF = """\
TAF
RKSI 071700Z 0718/0824 25010KT 9999 SCT030
  TEMPO 0720/0722 3000 SHRA
"""

PROB40_TAF = """\
TAF
RJTT 071700Z 0718/0824 12010KT 9999 FEW020
  PROB40 0800/0806 1000 TSRA
"""

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def parser() -> TafParser:
    return TafParser()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

def _is_iso_utc(s: str) -> bool:
    return bool(_ISO_RE.match(s))


# ---------------------------------------------------------------------------
# Multi-group TAF
# ---------------------------------------------------------------------------

class TestFullTaf:
    def test_returns_five_windows(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        # Base + TEMPO + FM + BECMG + PROB30 TEMPO = 5
        assert len(windows) == 5

    def test_all_windows_have_required_keys(self, parser):
        required = {"city", "issued_at", "valid_from", "valid_to", "group_type",
                    "temp", "wind_kt", "sig_wx", "raw_text"}
        for w in parser.parse(FULL_TAF, "Tokyo"):
            assert required == set(w.keys())

    def test_city_set_on_all_windows(self, parser):
        for w in parser.parse(FULL_TAF, "Tokyo"):
            assert w["city"] == "Tokyo"

    def test_issued_at_is_iso_utc(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        assert _is_iso_utc(windows[0]["issued_at"])

    def test_valid_from_is_iso_utc(self, parser):
        for w in parser.parse(FULL_TAF, "Tokyo"):
            assert _is_iso_utc(w["valid_from"])

    def test_valid_to_is_iso_utc(self, parser):
        for w in parser.parse(FULL_TAF, "Tokyo"):
            assert _is_iso_utc(w["valid_to"])

    def test_base_period_label(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        base = windows[0]
        assert base["group_type"] == "Base Period"

    def test_tempo_label(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        tempo_windows = [w for w in windows if "Temporary Fluctuation" == w["group_type"]]
        assert len(tempo_windows) >= 1

    def test_fm_label(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        fm_windows = [w for w in windows if w["group_type"] == "Definite Change"]
        assert len(fm_windows) == 1

    def test_becmg_label(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        becmg_windows = [w for w in windows if w["group_type"] == "Gradual Transition"]
        assert len(becmg_windows) == 1

    def test_prob30_tempo_label_is_temporary_fluctuation(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        # PROB30 TEMPO should be "Temporary Fluctuation" (same as TEMPO)
        prob_windows = [w for w in windows if "FG" in (w["sig_wx"] or "")]
        assert len(prob_windows) == 1
        assert prob_windows[0]["group_type"] == "Temporary Fluctuation"

    def test_tempo_has_sig_wx_tsra(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        tempo = [w for w in windows if w["group_type"] == "Temporary Fluctuation"
                 and "TS" in (w["sig_wx"] or "")]
        assert len(tempo) >= 1

    def test_base_wind_kt(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        base = windows[0]
        assert base["wind_kt"] == pytest.approx(10.0)

    def test_fm_wind_kt(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        fm = [w for w in windows if w["group_type"] == "Definite Change"][0]
        assert fm["wind_kt"] == pytest.approx(15.0)

    def test_prob30_sig_wx_fg(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        prob = [w for w in windows if "FG" in (w["sig_wx"] or "")]
        assert len(prob) == 1

    def test_raw_text_not_empty(self, parser):
        for w in parser.parse(FULL_TAF, "Tokyo"):
            assert w["raw_text"] and w["raw_text"].strip()


# ---------------------------------------------------------------------------
# CAVOK handling
# ---------------------------------------------------------------------------

class TestCavok:
    def test_no_crash(self, parser):
        windows = parser.parse(CAVOK_TAF, "Singapore")
        assert isinstance(windows, list)

    def test_returns_one_window(self, parser):
        windows = parser.parse(CAVOK_TAF, "Singapore")
        assert len(windows) == 1

    def test_sig_wx_is_none(self, parser):
        windows = parser.parse(CAVOK_TAF, "Singapore")
        assert windows[0]["sig_wx"] is None

    def test_temp_is_none(self, parser):
        windows = parser.parse(CAVOK_TAF, "Singapore")
        assert windows[0]["temp"] is None

    def test_wind_kt_extracted(self, parser):
        windows = parser.parse(CAVOK_TAF, "Singapore")
        assert windows[0]["wind_kt"] == pytest.approx(3.0)

    def test_group_type_is_base_period(self, parser):
        windows = parser.parse(CAVOK_TAF, "Singapore")
        assert windows[0]["group_type"] == "Base Period"


# ---------------------------------------------------------------------------
# Missing temperature
# ---------------------------------------------------------------------------

class TestMissingTemp:
    def test_no_crash(self, parser):
        windows = parser.parse(NO_TEMP_TAF, "Seoul")
        assert isinstance(windows, list)

    def test_base_temp_is_none_when_missing(self, parser):
        windows = parser.parse(NO_TEMP_TAF, "Seoul")
        assert windows[0]["temp"] is None

    def test_tempo_present(self, parser):
        windows = parser.parse(NO_TEMP_TAF, "Seoul")
        tempo_windows = [w for w in windows if w["group_type"] == "Temporary Fluctuation"]
        assert len(tempo_windows) == 1

    def test_tempo_sig_wx_has_shra(self, parser):
        windows = parser.parse(NO_TEMP_TAF, "Seoul")
        tempo = [w for w in windows if w["group_type"] == "Temporary Fluctuation"][0]
        assert tempo["sig_wx"] is not None
        assert "SH" in tempo["sig_wx"]


# ---------------------------------------------------------------------------
# PROB40 group
# ---------------------------------------------------------------------------

class TestProb40:
    def test_prob40_label_is_temporary_fluctuation(self, parser):
        windows = parser.parse(PROB40_TAF, "Tokyo")
        prob40 = [w for w in windows if w["group_type"] == "Temporary Fluctuation"]
        assert len(prob40) == 1

    def test_prob40_sig_wx_has_ts(self, parser):
        windows = parser.parse(PROB40_TAF, "Tokyo")
        prob40 = [w for w in windows if w["group_type"] == "Temporary Fluctuation"][0]
        assert prob40["sig_wx"] is not None
        assert "TS" in prob40["sig_wx"]


# ---------------------------------------------------------------------------
# Empty / unrecognisable input
# ---------------------------------------------------------------------------

class TestEmptyInput:
    def test_empty_string(self, parser):
        assert parser.parse("", "Tokyo") == []

    def test_whitespace_only(self, parser):
        assert parser.parse("   \n\t  ", "Tokyo") == []

    def test_garbage_string(self, parser):
        assert parser.parse("not a TAF at all", "Tokyo") == []

    def test_json_empty_array(self, parser):
        assert parser.parse("[]", "Tokyo") == []


# ---------------------------------------------------------------------------
# JSON-wrapped input (aviationweather.gov format)
# ---------------------------------------------------------------------------

class TestJsonWrappedInput:
    def _make_json(self, key: str, taf_text: str) -> str:
        return json.dumps([{key: taf_text}])

    def test_rawTAF_key(self, parser):
        raw = self._make_json("rawTAF", FULL_TAF)
        windows = parser.parse(raw, "Tokyo")
        assert len(windows) == 5

    def test_rawOb_key(self, parser):
        raw = self._make_json("rawOb", FULL_TAF)
        windows = parser.parse(raw, "Tokyo")
        assert len(windows) == 5

    def test_json_object_not_array(self, parser):
        raw = json.dumps({"rawTAF": FULL_TAF})
        windows = parser.parse(raw, "Tokyo")
        assert len(windows) == 5

    def test_json_empty_rawTAF(self, parser):
        raw = json.dumps([{"rawTAF": ""}])
        # Falls back to treating the JSON string as plain TAF → no header → []
        result = parser.parse(raw, "Tokyo")
        assert isinstance(result, list)

    def test_json_malformed(self, parser):
        # Malformed JSON should fall back to trying raw text
        result = parser.parse("{not valid json}", "Tokyo")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Overlapping TEMPO and PROB groups
# ---------------------------------------------------------------------------

class TestOverlappingGroups:
    def test_both_tempo_and_prob_returned(self, parser):
        taf = """\
TAF
RJTT 071700Z 0718/0824 12010KT 9999 FEW020
  TEMPO 0718/0722 4000 TSRA
  PROB30 TEMPO 0720/0724 1000 FG
"""
        windows = parser.parse(taf, "Tokyo")
        temporary = [w for w in windows if w["group_type"] == "Temporary Fluctuation"]
        assert len(temporary) == 2

    def test_first_tempo_has_tsra(self, parser):
        taf = """\
TAF
RJTT 071700Z 0718/0824 12010KT 9999 FEW020
  TEMPO 0718/0722 4000 TSRA
  PROB30 TEMPO 0720/0724 1000 FG
"""
        windows = parser.parse(taf, "Tokyo")
        tempo_ts = [w for w in windows
                    if w["group_type"] == "Temporary Fluctuation"
                    and "TS" in (w["sig_wx"] or "")]
        assert len(tempo_ts) == 1

    def test_prob30_has_fg(self, parser):
        taf = """\
TAF
RJTT 071700Z 0718/0824 12010KT 9999 FEW020
  TEMPO 0718/0722 4000 TSRA
  PROB30 TEMPO 0720/0724 1000 FG
"""
        windows = parser.parse(taf, "Tokyo")
        prob_fg = [w for w in windows
                   if w["group_type"] == "Temporary Fluctuation"
                   and "FG" in (w["sig_wx"] or "")]
        assert len(prob_fg) == 1


# ---------------------------------------------------------------------------
# issued_at extraction
# ---------------------------------------------------------------------------

class TestIssuedAt:
    def test_issued_at_matches_header(self, parser):
        # RJTT 071700Z → day=7, hour=17, min=00
        windows = parser.parse(FULL_TAF, "Tokyo")
        issued_at = windows[0]["issued_at"]
        # Should contain T17:00:00Z
        assert "T17:00:00Z" in issued_at

    def test_issued_at_same_across_all_windows(self, parser):
        windows = parser.parse(FULL_TAF, "Tokyo")
        issued_ats = {w["issued_at"] for w in windows}
        assert len(issued_ats) == 1
