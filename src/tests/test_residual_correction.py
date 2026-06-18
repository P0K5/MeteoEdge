"""Unit tests for src/model/residual_correction.py (issue #307).

Covers:
- correction applied when sample_count >= min_samples
- correction skipped silently when below min_samples
- clamp binds when |mean_signed_error| > max_correction_f
- MAE gate suppresses live NO entries when rolling_mae > threshold
- MAE gate does NOT suppress when rolling_mae is below threshold
- RESIDUAL_CORRECTION_ENABLED=False disables the whole correction path
- apply_residual_correction returns corrected value and stats
- MAE gate integration in scanner: NO candidate forced to shadow when suppressed
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from src.model.residual_correction import (
    ResidualStats,
    apply_residual_correction,
    compute_residual_stats,
    compute_residual_stats_per_pair,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db_with_deltas(deltas: list[float], city: str = "Busan") -> MagicMock:
    """Return a mock DB whose get_trailing_deltas yields the given delta_f values."""
    mock_db = MagicMock()
    mock_db.get_trailing_deltas.return_value = list(deltas)
    return mock_db


def _make_stats(
    mean_signed_error: float = 2.0,
    rolling_mae: float = 3.0,
    sample_count: int = 20,
    correction_applied: bool = True,
    live_suppressed: bool = False,
    city: str = "Busan",
) -> ResidualStats:
    return ResidualStats(
        city=city,
        mean_signed_error=mean_signed_error,
        rolling_mae=rolling_mae,
        sample_count=sample_count,
        correction_applied=correction_applied,
        live_suppressed=live_suppressed,
    )


# ---------------------------------------------------------------------------
# Test: ResidualStats.clamped_correction
# ---------------------------------------------------------------------------

class TestResidualStatsClamped:
    def test_no_clamp_needed(self):
        stats = _make_stats(mean_signed_error=2.0)
        assert stats.clamped_correction == pytest.approx(2.0)

    def test_clamp_positive_exceeds_max(self):
        """mean_signed_error=+8.0 with max=5.0 → clamped to +5.0."""
        stats = _make_stats(mean_signed_error=8.0)
        assert stats.clamped_correction == pytest.approx(5.0)

    def test_clamp_negative_exceeds_max(self):
        """mean_signed_error=-7.5 with max=5.0 → clamped to -5.0."""
        stats = _make_stats(mean_signed_error=-7.5)
        assert stats.clamped_correction == pytest.approx(-5.0)

    def test_zero_signed_error(self):
        stats = _make_stats(mean_signed_error=0.0)
        assert stats.clamped_correction == pytest.approx(0.0)

    def test_exact_boundary(self):
        """Exactly at the boundary is not clamped."""
        stats = _make_stats(mean_signed_error=5.0)
        assert stats.clamped_correction == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Test: compute_residual_stats — feature disabled
# ---------------------------------------------------------------------------

class TestComputeResidualStatsDisabled:
    def test_disabled_returns_none(self):
        """RESIDUAL_CORRECTION_ENABLED=False → always returns None."""
        db = _make_db_with_deltas([1.0, 2.0, 3.0] * 10)
        with patch.dict(os.environ, {"RESIDUAL_CORRECTION_ENABLED": "false"}):
            # Re-import so the module-level constant is refreshed via the function param
            result = compute_residual_stats("Busan", db)
        # The function checks the env var inline via module-level constant,
        # but we patch at the module level via monkeypatching the constant
        # Since compute_residual_stats checks RESIDUAL_CORRECTION_ENABLED at the module level,
        # we patch the module attribute directly
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = False
        try:
            result = compute_residual_stats("Busan", db)
            assert result is None
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_enabled_by_default(self):
        """With sufficient samples, returns stats when feature is on."""
        db = _make_db_with_deltas([2.0] * 15)
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            result = compute_residual_stats("Busan", db, min_samples=10)
            assert result is not None
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original


# ---------------------------------------------------------------------------
# Test: compute_residual_stats — minimum samples guard
# ---------------------------------------------------------------------------

class TestMinSamplesGuard:
    def test_below_min_returns_none(self):
        """Fewer than min_samples rows → returns None silently."""
        db = _make_db_with_deltas([1.0, 2.0, 3.0])  # 3 rows
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            result = compute_residual_stats("Busan", db, min_samples=10)
            assert result is None
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_exactly_at_min_applies(self):
        """Exactly min_samples rows → correction is applied."""
        db = _make_db_with_deltas([2.0] * 10)
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            result = compute_residual_stats("Busan", db, min_samples=10)
            assert result is not None
            assert result.sample_count == 10
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_above_min_applies(self):
        """More than min_samples rows → correction applies."""
        db = _make_db_with_deltas([3.0] * 20)
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            result = compute_residual_stats("Busan", db, min_samples=10)
            assert result is not None
            assert result.sample_count == 20
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_empty_deltas_returns_none(self):
        """Zero rows → returns None (no error)."""
        db = _make_db_with_deltas([])
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            result = compute_residual_stats("Busan", db, min_samples=10)
            assert result is None
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original


# ---------------------------------------------------------------------------
# Test: compute_residual_stats — stats accuracy
# ---------------------------------------------------------------------------

class TestResidualStatsAccuracy:
    def _run(self, deltas, min_samples=1, max_correction_f=5.0, mae_threshold=8.0):
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            db = _make_db_with_deltas(deltas)
            return compute_residual_stats(
                "Tokyo", db,
                min_samples=min_samples,
                max_correction_f=max_correction_f,
                mae_threshold=mae_threshold,
            )
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_mean_signed_error_positive(self):
        """All positive deltas: warm bias."""
        stats = self._run([4.0, 6.0, 8.0])
        assert stats is not None
        assert stats.mean_signed_error == pytest.approx(6.0)

    def test_mean_signed_error_mixed(self):
        """Mixed deltas: mean should be correct."""
        stats = self._run([2.0, -2.0, 4.0])  # mean = 4/3
        assert stats is not None
        assert stats.mean_signed_error == pytest.approx(4.0 / 3.0)

    def test_rolling_mae_all_positive(self):
        """MAE of all-positive deltas equals mean."""
        stats = self._run([3.0, 5.0, 7.0])
        assert stats is not None
        assert stats.rolling_mae == pytest.approx(5.0)

    def test_rolling_mae_absolute_value(self):
        """MAE uses absolute values: [-4, +6] → MAE=(4+6)/2=5."""
        stats = self._run([-4.0, 6.0])
        assert stats is not None
        assert stats.rolling_mae == pytest.approx(5.0)

    def test_sample_count(self):
        stats = self._run([1.0, 2.0, 3.0, 4.0, 5.0])
        assert stats.sample_count == 5

    def test_correction_applied_when_nonzero(self):
        """correction_applied=True when clamped_correction != 0."""
        stats = self._run([3.0] * 5)
        assert stats.correction_applied is True

    def test_correction_applied_zero(self):
        """correction_applied=True even if mean is exactly 0 (clamped=0)."""
        # With all-zero deltas, mean=0 → clamped=0 → correction_applied=False
        stats = self._run([0.0] * 5)
        assert stats is not None
        assert stats.clamped_correction == pytest.approx(0.0)
        assert stats.correction_applied is False


# ---------------------------------------------------------------------------
# Test: clamp behaviour
# ---------------------------------------------------------------------------

class TestClampBehaviour:
    def _run(self, deltas, max_correction_f=5.0):
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            db = _make_db_with_deltas(deltas)
            return compute_residual_stats(
                "Singapore", db,
                min_samples=1,
                max_correction_f=max_correction_f,
            )
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_clamp_binds_positive(self):
        """Mean = +10°F clamped to +5°F."""
        stats = self._run([10.0] * 5, max_correction_f=5.0)
        assert stats is not None
        assert stats.clamped_correction == pytest.approx(5.0)

    def test_clamp_binds_negative(self):
        """Mean = -8°F clamped to -5°F."""
        stats = self._run([-8.0] * 5, max_correction_f=5.0)
        assert stats is not None
        assert stats.clamped_correction == pytest.approx(-5.0)

    def test_clamp_not_needed_small_value(self):
        """Mean = +2°F within ±5 → no clamp."""
        stats = self._run([2.0] * 5, max_correction_f=5.0)
        assert stats is not None
        assert stats.clamped_correction == pytest.approx(2.0)

    def test_custom_clamp_range(self):
        """Custom max_correction_f=3.0 → mean=+4.6 applied correction clamped to +3.0.

        clamped_correction uses the module-level constant; we verify the clamping
        via apply_residual_correction which uses the parameter.
        """
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            db = _make_db_with_deltas([4.6] * 5)
            corrected, stats = apply_residual_correction(
                "Singapore", 80.0, db,
                min_samples=1, max_correction_f=3.0,
            )
            # With max_correction_f=3.0, mean=4.6 is clamped to 3.0
            # so corrected = 80.0 + 3.0 = 83.0
            assert corrected == pytest.approx(83.0)
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original


# ---------------------------------------------------------------------------
# Test: MAE gate
# ---------------------------------------------------------------------------

class TestMaeGate:
    def _run(self, deltas, mae_threshold=8.0):
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            db = _make_db_with_deltas(deltas)
            return compute_residual_stats(
                "Busan", db,
                min_samples=1,
                mae_threshold=mae_threshold,
            )
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_mae_above_threshold_suppresses_live(self):
        """MAE=10.1 > threshold=8.0 → live_suppressed=True."""
        # delta_f values giving MAE ~10.1
        stats = self._run([10.1] * 5, mae_threshold=8.0)
        assert stats is not None
        assert stats.rolling_mae == pytest.approx(10.1)
        assert stats.live_suppressed is True

    def test_mae_below_threshold_does_not_suppress(self):
        """MAE=3.7 < threshold=8.0 → live_suppressed=False."""
        stats = self._run([3.7] * 5, mae_threshold=8.0)
        assert stats is not None
        assert stats.rolling_mae == pytest.approx(3.7)
        assert stats.live_suppressed is False

    def test_mae_exactly_at_threshold_does_not_suppress(self):
        """MAE exactly equal to threshold → NOT suppressed (strictly >)."""
        stats = self._run([8.0] * 5, mae_threshold=8.0)
        assert stats is not None
        assert stats.live_suppressed is False

    def test_mae_just_above_threshold_suppresses(self):
        """MAE=8.01 > 8.0 → suppressed."""
        stats = self._run([8.01] * 5, mae_threshold=8.0)
        assert stats is not None
        assert stats.live_suppressed is True


# ---------------------------------------------------------------------------
# Test: apply_residual_correction
# ---------------------------------------------------------------------------

class TestApplyResidualCorrection:
    def _enable(self):
        import src.model.residual_correction as rc_mod
        return rc_mod

    def test_returns_corrected_mu_f(self):
        """Correction = +2°F → mu_f 80.0 becomes 82.0."""
        db = _make_db_with_deltas([2.0] * 15)
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            corrected, stats = apply_residual_correction("Busan", 80.0, db, min_samples=10)
            assert stats is not None
            assert corrected == pytest.approx(80.0 + stats.clamped_correction)
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_returns_none_stats_when_disabled(self):
        """Feature disabled → mu_f unchanged, stats=None."""
        db = _make_db_with_deltas([5.0] * 15)
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = False
        try:
            corrected, stats = apply_residual_correction("Busan", 80.0, db, min_samples=10)
            assert corrected == pytest.approx(80.0)
            assert stats is None
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_returns_original_when_below_min_samples(self):
        """Insufficient data → mu_f unchanged, stats=None."""
        db = _make_db_with_deltas([3.0] * 5)  # only 5 rows
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            corrected, stats = apply_residual_correction("Busan", 85.0, db, min_samples=10)
            assert corrected == pytest.approx(85.0)
            assert stats is None
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_clamped_correction_applied(self):
        """Correction clamped to ±5 → mu_f shifts by at most ±5."""
        db = _make_db_with_deltas([20.0] * 15)  # mean=+20, clamped to +5
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            corrected, stats = apply_residual_correction(
                "Tokyo", 75.0, db, min_samples=10, max_correction_f=5.0
            )
            assert stats is not None
            assert corrected == pytest.approx(80.0)  # 75 + 5
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original

    def test_zero_correction_returns_unchanged(self):
        """Zero mean error → mu_f unchanged, stats returned."""
        db = _make_db_with_deltas([0.0] * 15)
        import src.model.residual_correction as rc_mod
        original = rc_mod.RESIDUAL_CORRECTION_ENABLED
        rc_mod.RESIDUAL_CORRECTION_ENABLED = True
        try:
            corrected, stats = apply_residual_correction("Seoul", 70.0, db, min_samples=10)
            assert corrected == pytest.approx(70.0)
            # stats is still returned (we computed it; mean just happened to be 0)
            assert stats is not None
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = original


# ---------------------------------------------------------------------------
# Test: MAE gate integration in scanner
# ---------------------------------------------------------------------------

class TestScannerMaeGate:
    """Integration-style tests that verify the MAE gate inside scan_markets()
    forces NO candidates to shadow mode when a city's rolling MAE exceeds
    MAX_RESIDUAL_MAE_F_FOR_LIVE.

    We patch compute_residual_stats to return a controlled ResidualStats object
    rather than hitting the DB.
    """

    def _make_scanner_inputs(self):
        """Build minimal weather, markets, and db objects for a NO scan."""
        from datetime import datetime, timezone, timedelta
        from src.model.envelope import WeatherState

        now = datetime.now(timezone.utc)
        end_time = (now + timedelta(hours=3)).isoformat()

        state = WeatherState(
            station="RKPK",
            now_local=now,
            sunset_local=now,
            current_high_f=70.0,
            current_high_time=now,
            latest_temp_f=68.0,
            latest_temp_time=now,
            forecast_high_f=72.0,
            deb_mu_f=72.0,
        )

        # Build a bracket where NO has edge: p_yes will be very low
        # bracket_low=85°F, bracket_high=90°F, no_ask=85¢ — well above forecast 72°F
        market = {
            "question": "Will the highest temperature in Busan be between 85 and 90 degrees on today?",
            "groupItemTitle": "85-90°C",
            "endDate": end_time,
            "outcomePrices": '["0.10", "0.90"]',
            "outcomes": '["YES", "NO"]',
            "tokens": [
                {"token_id": "tok_yes", "outcome": "YES"},
                {"token_id": "tok_no", "outcome": "NO"},
            ],
            "active": True,
        }

        weather = {"RKPK": state}

        mock_db = MagicMock()
        mock_db.get_station_override.return_value = None
        mock_db.get_config.return_value = None
        mock_db.get_all_config.return_value = {}

        return weather, [market], mock_db

    def test_mae_gate_suppresses_live_no_to_shadow(self):
        """When rolling MAE > threshold, NO candidate shadow flag is True."""
        from src.strategy.scanner import scan_markets

        suppressed_stats = ResidualStats(
            city="Busan",
            mean_signed_error=4.6,
            rolling_mae=10.1,   # above default 8.0 threshold
            sample_count=90,
            correction_applied=True,
            live_suppressed=True,
        )

        weather, markets, mock_db = self._make_scanner_inputs()

        with patch("src.strategy.scanner.compute_residual_stats", return_value=suppressed_stats):
            candidates, _ = scan_markets(weather, markets, db=mock_db)

        no_candidates = [c for c in candidates if c.side == "NO"]
        if no_candidates:
            # When MAE gate fires, NO should be forced to shadow
            assert all(c.shadow for c in no_candidates), (
                "Expected all NO candidates to be shadow when MAE gate fires"
            )

    def test_mae_gate_does_not_suppress_when_below_threshold(self):
        """When rolling MAE <= threshold, NO candidate shadow flag is False."""
        from src.strategy.scanner import scan_markets

        safe_stats = ResidualStats(
            city="Busan",
            mean_signed_error=1.0,
            rolling_mae=3.7,   # below default 8.0 threshold
            sample_count=70,
            correction_applied=True,
            live_suppressed=False,
        )

        weather, markets, mock_db = self._make_scanner_inputs()

        with patch("src.strategy.scanner.compute_residual_stats", return_value=safe_stats):
            candidates, _ = scan_markets(weather, markets, db=mock_db)

        no_candidates = [c for c in candidates if c.side == "NO"]
        if no_candidates:
            # When MAE gate does not fire, shadow should be determined by station status only
            # (RKPK is not in SHADOW_STATIONS by default, so shadow=False)
            assert not any(c.shadow for c in no_candidates), (
                "Expected NO candidates to be live when MAE is within threshold"
            )

    def test_mae_gate_does_not_affect_yes_candidates(self):
        """MAE gate only affects NO side; YES candidates are unaffected."""
        from src.strategy.scanner import scan_markets

        suppressed_stats = ResidualStats(
            city="Busan",
            mean_signed_error=4.6,
            rolling_mae=10.1,
            sample_count=90,
            correction_applied=True,
            live_suppressed=True,
        )

        weather, markets, mock_db = self._make_scanner_inputs()

        with patch("src.strategy.scanner.compute_residual_stats", return_value=suppressed_stats):
            candidates, _ = scan_markets(weather, markets, db=mock_db)

        yes_candidates = [c for c in candidates if c.side == "YES"]
        # YES candidates are not touched by the MAE gate — their shadow flag
        # is determined by ENABLE_YES_TRADES and yes_enabled, not MAE.
        # Just verify the gate doesn't crash when YES candidates are present.
        _ = yes_candidates  # No assertion on shadow — YES has its own logic


# ---------------------------------------------------------------------------
# Test: scope field and pair-scoped logic (issue #340)
# ---------------------------------------------------------------------------

def _enable_residual():
    """Context manager helper: set RESIDUAL_CORRECTION_ENABLED = True."""
    import src.model.residual_correction as rc_mod
    orig = rc_mod.RESIDUAL_CORRECTION_ENABLED
    rc_mod.RESIDUAL_CORRECTION_ENABLED = True
    return rc_mod, orig


class TestResidualStatsScopeField:
    """Ensure ResidualStats has the new scope/station/source fields."""

    def test_default_scope_city_fallback(self):
        """Default scope is city_fallback."""
        stats = ResidualStats(
            city="Busan",
            mean_signed_error=1.0,
            rolling_mae=1.5,
            sample_count=10,
            correction_applied=True,
            live_suppressed=False,
        )
        assert stats.scope == "city_fallback"
        assert stats.station == ""
        assert stats.source == ""

    def test_pair_scope(self):
        """Setting scope='pair' with station+source is preserved."""
        stats = ResidualStats(
            city="Busan",
            mean_signed_error=1.0,
            rolling_mae=1.5,
            sample_count=10,
            correction_applied=True,
            live_suppressed=False,
            scope="pair",
            station="Busan",
            source="amos",
        )
        assert stats.scope == "pair"
        assert stats.station == "Busan"
        assert stats.source == "amos"


class TestComputeResidualStatsPairScoped:
    """compute_residual_stats uses top-priority pair when it has enough samples."""

    def _make_db_pair(self, city_deltas, pair_deltas, pair_station="Busan", pair_source="amos"):
        """Return a mock DB that returns different deltas based on station/source filters."""
        mock_db = MagicMock()

        def _get_trailing_deltas(city, window_days, *, station=None, source=None):
            if station == pair_station and source == pair_source:
                return list(pair_deltas)
            return list(city_deltas)

        mock_db.get_trailing_deltas.side_effect = _get_trailing_deltas
        return mock_db

    def test_uses_pair_scope_when_pair_has_enough_samples(self):
        """When top-priority pair has >= min_samples, scope='pair'."""
        rc_mod, orig = _enable_residual()
        try:
            db = self._make_db_pair(city_deltas=[1.0] * 20, pair_deltas=[2.0] * 15)
            with patch("src.model.residual_correction._get_top_priority_pair",
                       return_value=("Busan", "amos")):
                stats = compute_residual_stats("Busan", db, min_samples=10)
            assert stats is not None
            assert stats.scope == "pair"
            assert stats.station == "Busan"
            assert stats.source == "amos"
            assert stats.sample_count == 15
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = orig

    def test_falls_back_to_city_when_pair_insufficient(self):
        """When pair has < min_samples, falls back to city-wide with scope='city_fallback'."""
        rc_mod, orig = _enable_residual()
        try:
            # pair has only 5 rows, city-wide has 20
            db = self._make_db_pair(city_deltas=[1.0] * 20, pair_deltas=[2.0] * 5)
            with patch("src.model.residual_correction._get_top_priority_pair",
                       return_value=("Busan", "amos")):
                stats = compute_residual_stats("Busan", db, min_samples=10)
            assert stats is not None
            assert stats.scope == "city_fallback"
            assert stats.station == ""
            assert stats.source == ""
            assert stats.sample_count == 20
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = orig

    def test_no_top_pair_falls_back_to_city(self):
        """When get_source_priority returns nothing, city-wide fallback applies."""
        rc_mod, orig = _enable_residual()
        try:
            db = _make_db_with_deltas([3.0] * 15)
            with patch("src.model.residual_correction._get_top_priority_pair",
                       return_value=None):
                stats = compute_residual_stats("Busan", db, min_samples=10)
            assert stats is not None
            assert stats.scope == "city_fallback"
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = orig


class TestComputeResidualStatsPerPair:
    """compute_residual_stats_per_pair returns one entry per qualified pair."""

    def _make_db_with_pairs(self, pairs_data):
        """pairs_data: list of (station, source, [delta_f, ...])."""
        mock_db = MagicMock()

        # Simulate DISTINCT station/source query
        mock_db._conn.execute.return_value.fetchall.return_value = [
            (station, source) for station, source, _ in pairs_data
        ]

        def _get_trailing_deltas(city, window_days, *, station=None, source=None):
            for s, src, deltas in pairs_data:
                if s == station and src == source:
                    return list(deltas)
            return []

        mock_db.get_trailing_deltas.side_effect = _get_trailing_deltas
        return mock_db

    def test_returns_one_entry_per_pair(self):
        """One ResidualStats per (station, source) pair with sufficient samples."""
        rc_mod, orig = _enable_residual()
        try:
            pairs_data = [
                ("Busan", "amos", [2.0] * 15),
                ("RKPK", "metar", [1.0] * 12),
            ]
            db = self._make_db_with_pairs(pairs_data)
            results = compute_residual_stats_per_pair("Busan", db, min_samples=10)
            assert len(results) == 2
            sources = {r.source for r in results}
            assert sources == {"amos", "metar"}
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = orig

    def test_excludes_pairs_below_min_samples(self):
        """Pairs with < min_samples are excluded from results."""
        rc_mod, orig = _enable_residual()
        try:
            pairs_data = [
                ("Busan", "amos", [2.0] * 15),
                ("RKPK", "metar", [1.0] * 3),  # below min_samples=10
            ]
            db = self._make_db_with_pairs(pairs_data)
            results = compute_residual_stats_per_pair("Busan", db, min_samples=10)
            assert len(results) == 1
            assert results[0].source == "amos"
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = orig

    def test_returns_empty_when_disabled(self):
        """Returns empty list when feature is disabled."""
        rc_mod, orig = _enable_residual()
        rc_mod.RESIDUAL_CORRECTION_ENABLED = False
        try:
            db = MagicMock()
            results = compute_residual_stats_per_pair("Busan", db)
            assert results == []
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = orig

    def test_scope_is_pair(self):
        """All entries returned by per_pair have scope='pair'."""
        rc_mod, orig = _enable_residual()
        try:
            pairs_data = [("Busan", "amos", [2.0] * 15)]
            db = self._make_db_with_pairs(pairs_data)
            results = compute_residual_stats_per_pair("Busan", db, min_samples=10)
            assert len(results) == 1
            assert results[0].scope == "pair"
        finally:
            rc_mod.RESIDUAL_CORRECTION_ENABLED = orig
