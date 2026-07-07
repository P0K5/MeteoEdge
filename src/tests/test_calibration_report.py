"""Unit tests for the pure computation core of src/scripts/calibration_report.py.

Covers:
- pick_samples: final-per-market selection (lowest minutes_to_settlement),
  hourly dedup (last snapshot per market per hour), and filtering of
  non-0x tickers / missing raw_p_yes.
- build_reliability: bucketing, mean prediction, observed frequency.
- brier_score: exact values and empty-input handling.

No network, no DB — everything operates on in-memory snapshot dicts.
"""
import pytest

from src.scripts.calibration_report import (
    pick_samples,
    build_reliability,
    brier_score,
)


def _snap(ticker="0xabc", ts="2026-07-01T10:00:00+00:00", raw_p_yes=0.1,
          minutes=300, capped=None):
    return {
        "ticker": ticker, "ts": ts, "raw_p_yes": raw_p_yes,
        "minutes_to_settlement": minutes,
        "capped_p_yes": capped if capped is not None else max(raw_p_yes, 0.05),
    }


class TestPickSamples:
    def test_final_is_lowest_minutes_to_settlement(self):
        snaps = [
            _snap(ts="2026-07-01T08:00:00+00:00", raw_p_yes=0.30, minutes=600),
            _snap(ts="2026-07-01T14:00:00+00:00", raw_p_yes=0.10, minutes=60),
            _snap(ts="2026-07-01T11:00:00+00:00", raw_p_yes=0.20, minutes=300),
        ]
        final, _ = pick_samples(snaps)
        assert final["0xabc"]["raw_p_yes"] == 0.10

    def test_hourly_dedup_keeps_last_in_hour(self):
        snaps = [
            _snap(ts="2026-07-01T10:05:00+00:00", raw_p_yes=0.30),
            _snap(ts="2026-07-01T10:55:00+00:00", raw_p_yes=0.20),
            _snap(ts="2026-07-01T11:05:00+00:00", raw_p_yes=0.10),
        ]
        _, hourly = pick_samples(snaps)
        assert len(hourly) == 2
        assert hourly[("0xabc", "2026-07-01T10")]["raw_p_yes"] == 0.20
        assert hourly[("0xabc", "2026-07-01T11")]["raw_p_yes"] == 0.10

    def test_non_0x_tickers_and_missing_raw_p_ignored(self):
        snaps = [
            _snap(ticker="KORD-order-123"),
            {**_snap(), "raw_p_yes": None},
        ]
        final, hourly = pick_samples(snaps)
        assert final == {} and hourly == {}

    def test_markets_kept_separate(self):
        snaps = [_snap(ticker="0xaaa"), _snap(ticker="0xbbb")]
        final, _ = pick_samples(snaps)
        assert set(final) == {"0xaaa", "0xbbb"}


class TestBuildReliability:
    def test_buckets_and_frequencies(self):
        # 4 samples in [0, 0.02): 1 YES -> obs 25%
        # 2 samples in [0.50, 0.65): 2 YES -> obs 100%
        samples = [(0.0, False), (0.01, False), (0.01, False), (0.015, True),
                   (0.55, True), (0.60, True)]
        rows = build_reliability(samples)
        assert len(rows) == 2
        low, mid = rows
        assert low["n"] == 4
        assert low["obs_freq"] == pytest.approx(0.25)
        assert low["mean_pred"] == pytest.approx((0.0 + 0.01 + 0.01 + 0.015) / 4)
        assert mid["n"] == 2
        assert mid["obs_freq"] == pytest.approx(1.0)

    def test_empty_buckets_omitted(self):
        rows = build_reliability([(0.99, True)])
        assert len(rows) == 1
        assert rows[0]["bucket"].startswith("0.95")

    def test_p_of_exactly_one_lands_in_top_bucket(self):
        rows = build_reliability([(1.0, True)])
        assert len(rows) == 1
        assert rows[0]["n"] == 1


class TestBrierScore:
    def test_exact_value(self):
        # (0.2 - 0)^2 = 0.04 ; (0.9 - 1)^2 = 0.01 -> mean 0.025
        assert brier_score([(0.2, False), (0.9, True)]) == pytest.approx(0.025)

    def test_perfect_and_worst(self):
        assert brier_score([(0.0, False), (1.0, True)]) == 0.0
        assert brier_score([(1.0, False), (0.0, True)]) == 1.0

    def test_empty_returns_none(self):
        assert brier_score([]) is None
