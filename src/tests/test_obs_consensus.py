"""Tests for obs_consensus.compute_consensus_high (issue #322)."""
from __future__ import annotations

import pytest
from unittest.mock import patch

from src.data.obs_consensus import compute_consensus_high


def _obs(source: str, temp_f: float, station: str = "KORD") -> dict:
    return {"source": source, "temp_f": temp_f, "station": station}


class TestComputeConsensusHigh:
    def test_returns_none_when_no_obs(self):
        assert compute_consensus_high([]) is None

    def test_normal_dense_tick_accepted_within_sigma(self):
        obs = [
            _obs("metar", 70.0),
            _obs("mss", 72.0),  # 2°F above metar_max — within default 4°F sigma
        ]
        result = compute_consensus_high(obs, outlier_sigma_f=4.0)
        assert result == pytest.approx(72.0)

    def test_outlier_tick_rejected_when_above_sigma(self):
        obs = [
            _obs("metar", 73.4),
            _obs("mss", 93.2),  # 19.8°F above metar — well above 4°F sigma
        ]
        result = compute_consensus_high(obs, outlier_sigma_f=4.0)
        # spike rejected, falls back to metar_max
        assert result == pytest.approx(73.4)

    def test_no_metar_falls_back_to_dense_feed_max(self):
        obs = [
            _obs("mss", 80.0),
            _obs("amos", 82.0),
        ]
        result = compute_consensus_high(obs, outlier_sigma_f=4.0)
        assert result == pytest.approx(82.0)

    def test_disabled_flag_returns_raw_max(self):
        obs = [
            _obs("metar", 70.0),
            _obs("mss", 95.0),  # spike that would normally be rejected
        ]
        with patch("src.data.obs_consensus._ENABLED", False):
            result = compute_consensus_high(obs, outlier_sigma_f=4.0)
        assert result == pytest.approx(95.0)

    def test_borderline_tick_exactly_at_sigma_not_rejected(self):
        # temp = metar_max + sigma → NOT rejected (strict inequality: temp > threshold)
        obs = [
            _obs("metar", 70.0),
            _obs("mss", 74.0),  # exactly 4°F above: should be accepted
        ]
        result = compute_consensus_high(obs, outlier_sigma_f=4.0)
        assert result == pytest.approx(74.0)

    def test_all_metar_no_dense_returns_metar_max(self):
        obs = [
            _obs("metar", 68.0),
            _obs("metar", 71.0),
        ]
        result = compute_consensus_high(obs, outlier_sigma_f=4.0)
        assert result == pytest.approx(71.0)

    def test_multiple_dense_ticks_only_outlier_rejected(self):
        obs = [
            _obs("metar", 70.0),
            _obs("mss", 72.0),   # accepted
            _obs("amos", 95.0),  # rejected (25°F above metar_max)
            _obs("mss", 73.5),   # accepted
        ]
        result = compute_consensus_high(obs, outlier_sigma_f=4.0)
        assert result == pytest.approx(73.5)
