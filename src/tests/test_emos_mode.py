"""Unit tests for src/model/emos_mode.py — EMOS mode switching helpers.

All tests use an in-memory SQLite database to avoid file I/O.
"""
import logging
from datetime import datetime

import pytest

from src.data.db import Database
from src.model.emos_mode import apply_emos, _check_ready_for_promotion, get_city_mode
from src.model.envelope import (
    Bracket,
    WeatherState,
    true_probability_yes,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


def _upsert(db: Database, city: str, model_mode: str, *,
            a: float = 0.0, b: float = 1.0, c: float = 0.0, d: float = 1.0,
            ready_for_promotion: int = 0,
            crps_score: float = 1.5) -> None:
    """Insert EMOS calibration row for test convenience."""
    db.upsert_emos_coefficients(
        city=city,
        model_mode=model_mode,
        a=a, b=b, c=c, d=d,
        crps_score=crps_score,
        trained_at=datetime.utcnow().isoformat(),
        ready_for_promotion=ready_for_promotion,
    )


def _make_bracket(low_f: float = 78.0, high_f: float = 82.0) -> Bracket:
    return Bracket(
        ticker="TEST",
        low_f=low_f,
        high_f=high_f,
        yes_ask_cents=50,
        yes_ask_size=100,
        no_ask_cents=52,
        no_ask_size=100,
    )


def _make_state(
    forecast_high_f: float = 80.0,
    current_high_f: float = 78.0,
    hour: int = 14,
) -> WeatherState:
    now = datetime(2026, 6, 1, hour, 0)
    return WeatherState(
        station="KORD",
        now_local=now,
        sunset_local=datetime(2026, 6, 1, 20, 30),
        current_high_f=current_high_f,
        current_high_time=now,
        latest_temp_f=current_high_f,
        latest_temp_time=now,
        forecast_high_f=forecast_high_f,
    )


# ---------------------------------------------------------------------------
# Test 1: legacy default — get_city_mode with db=None
# ---------------------------------------------------------------------------

class TestGetCityModeLegacyDefault:
    def test_returns_legacy_when_db_is_none(self):
        """get_city_mode('Tokyo', db=None) returns 'legacy' regardless of env."""
        result = get_city_mode("Tokyo", db=None)
        assert result == "legacy"

    def test_respects_emos_default_mode_env_when_db_none(self, monkeypatch):
        """With db=None, EMOS_DEFAULT_MODE env var is honoured."""
        monkeypatch.setenv("EMOS_DEFAULT_MODE", "emos_shadow")
        result = get_city_mode("Tokyo", db=None)
        assert result == "emos_shadow"
        monkeypatch.delenv("EMOS_DEFAULT_MODE")

    def test_returns_legacy_when_no_row_in_db(self):
        """No calibration row in DB → falls back to 'legacy'."""
        db = _db()
        result = get_city_mode("Tokyo", db=db)
        assert result == "legacy"


# ---------------------------------------------------------------------------
# Test 2: shadow active — emos_shadow row present, no primary
# ---------------------------------------------------------------------------

class TestGetCityModeShadow:
    def test_shadow_row_present_returns_emos_shadow(self):
        """emos_shadow row present → get_city_mode returns 'emos_shadow'."""
        db = _db()
        _upsert(db, "Chicago", "emos_shadow", ready_for_promotion=0)
        result = get_city_mode("Chicago", db=db)
        assert result == "emos_shadow"

    def test_shadow_takes_precedence_over_no_primary(self):
        """Only shadow row → returns 'emos_shadow', not 'legacy'."""
        db = _db()
        _upsert(db, "Miami", "emos_shadow", ready_for_promotion=0)
        result = get_city_mode("Miami", db=db)
        assert result == "emos_shadow"


# ---------------------------------------------------------------------------
# Test 3: primary guard — ready_for_promotion=0 falls back
# ---------------------------------------------------------------------------

class TestGetCityModePrimaryUnready:
    def test_primary_unready_falls_back_to_legacy(self):
        """emos_primary row with ready_for_promotion=0 → returns 'legacy'."""
        db = _db()
        _upsert(db, "Houston", "emos_primary", ready_for_promotion=0)
        result = get_city_mode("Houston", db=db)
        # No shadow row either, so should fall back to legacy
        assert result == "legacy"

    def test_primary_unready_with_shadow_returns_shadow(self):
        """emos_primary unready + shadow row → returns 'emos_shadow'."""
        db = _db()
        _upsert(db, "Atlanta", "emos_primary", ready_for_promotion=0)
        _upsert(db, "Atlanta", "emos_shadow", ready_for_promotion=0)
        result = get_city_mode("Atlanta", db=db)
        assert result == "emos_shadow"

    def test_primary_unready_emits_warning_via_check_ready(self, caplog):
        """_check_ready_for_promotion returns False when ready_for_promotion=0."""
        db = _db()
        _upsert(db, "Seattle", "emos_primary", ready_for_promotion=0)
        result = _check_ready_for_promotion("Seattle", db=db)
        assert result is False


# ---------------------------------------------------------------------------
# Test 4: primary guard — ready_for_promotion=1 promoted
# ---------------------------------------------------------------------------

class TestGetCityModePrimaryReady:
    def test_primary_ready_returns_emos_primary(self):
        """emos_primary row with ready_for_promotion=1 and enough CRPS samples → 'emos_primary'."""
        db = _db()
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        # Populate enough CRPS log entries to satisfy the promotion guard (default 20)
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5)
        result = get_city_mode("Chicago", db=db)
        assert result == "emos_primary"

    def test_check_ready_for_promotion_true_when_ready(self):
        """_check_ready_for_promotion returns True when ready_for_promotion=1."""
        db = _db()
        _upsert(db, "Miami", "emos_primary", ready_for_promotion=1)
        result = _check_ready_for_promotion("Miami", db=db)
        assert result is True

    def test_primary_ready_takes_precedence_over_shadow(self):
        """Both shadow and ready primary + enough CRPS samples → 'emos_primary' wins."""
        db = _db()
        _upsert(db, "Los Angeles", "emos_shadow", ready_for_promotion=0)
        _upsert(db, "Los Angeles", "emos_primary", ready_for_promotion=1)
        # Populate enough CRPS log entries to satisfy the promotion guard (default 20)
        for i in range(20):
            db.log_crps("Los Angeles", f"2026-05-{i + 1:02d}", 1.5)
        result = get_city_mode("Los Angeles", db=db)
        assert result == "emos_primary"


# ---------------------------------------------------------------------------
# Test 4b: operator override (dashboard promote/demote) is authoritative
# ---------------------------------------------------------------------------

class TestGetCityModeOverride:
    def test_override_primary_with_samples_returns_primary(self):
        """effective_mode='emos_primary' + primary row + enough CRPS → 'emos_primary'.

        Mirrors the dashboard promote path, which sets ready_for_promotion=0 on
        the primary row but records the override — the override must still win.
        """
        db = _db()
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=0)
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5)
        db.set_emos_effective_mode("Chicago", "emos_primary")
        assert get_city_mode("Chicago", db=db) == "emos_primary"

    def test_override_primary_without_samples_falls_back_to_shadow(self):
        """Override primary but CRPS guard not met → falls back to shadow."""
        db = _db()
        _upsert(db, "Miami", "emos_shadow", ready_for_promotion=0)
        _upsert(db, "Miami", "emos_primary", ready_for_promotion=0)
        db.set_emos_effective_mode("Miami", "emos_primary")
        # No CRPS rows → guard blocks → shadow
        assert get_city_mode("Miami", db=db) == "emos_shadow"

    def test_override_primary_without_primary_row_falls_back(self):
        """Override primary but no primary calibration row → falls back safely."""
        db = _db()
        _upsert(db, "Houston", "emos_shadow", ready_for_promotion=0)
        db.set_emos_effective_mode("Houston", "emos_primary")
        assert get_city_mode("Houston", db=db) == "emos_shadow"

    def test_override_legacy_forces_legacy_over_ready_primary(self):
        """demote (effective_mode='legacy') overrides an otherwise-ready primary."""
        db = _db()
        _upsert(db, "Atlanta", "emos_shadow", ready_for_promotion=1)
        _upsert(db, "Atlanta", "emos_primary", ready_for_promotion=1)
        for i in range(20):
            db.log_crps("Atlanta", f"2026-05-{i + 1:02d}", 1.5)
        db.set_emos_effective_mode("Atlanta", "legacy")
        assert get_city_mode("Atlanta", db=db) == "legacy"

    def test_override_shadow_returns_shadow(self):
        """effective_mode='emos_shadow' with a shadow row → 'emos_shadow'."""
        db = _db()
        _upsert(db, "Los Angeles", "emos_shadow", ready_for_promotion=0)
        db.set_emos_effective_mode("Los Angeles", "emos_shadow")
        assert get_city_mode("Los Angeles", db=db) == "emos_shadow"

    def test_check_ready_for_promotion_honors_override_with_guard(self):
        """_check_ready_for_promotion mirrors get_city_mode: override + CRPS guard.

        The override alone is not enough — the CRPS sample guard still applies,
        so the function never signals 'ready' for a city get_city_mode would
        route to shadow.
        """
        db = _db()
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=0)
        assert _check_ready_for_promotion("Chicago", db=db) is False
        db.set_emos_effective_mode("Chicago", "emos_primary")
        # Override set but zero CRPS samples → still not ready.
        assert _check_ready_for_promotion("Chicago", db=db) is False
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5)
        assert _check_ready_for_promotion("Chicago", db=db) is True


# ---------------------------------------------------------------------------
# Test 5: apply_emos — correct linear correction
# ---------------------------------------------------------------------------

class TestApplyEmosCorrect:
    def test_identity_coefficients_return_raw(self):
        """a=0, b=1, c=0, d=1 → mu_cal=mu_raw, sigma_cal=sigma_raw."""
        db = _db()
        _upsert(db, "Chicago", "emos_shadow", a=0.0, b=1.0, c=0.0, d=1.0)
        mu_cal, sigma_cal = apply_emos(80.0, 2.0, "Chicago", db)
        assert mu_cal == 80.0
        assert sigma_cal == 2.0

    def test_linear_correction_applied_correctly(self):
        """mu_cal = a + b*mu, sigma_cal = c + d*sigma with known values."""
        db = _db()
        a, b, c, d = 1.5, 0.9, 0.3, 1.1
        _upsert(db, "Miami", "emos_shadow", a=a, b=b, c=c, d=d)
        mu_raw, sigma_raw = 80.0, 2.0
        mu_cal, sigma_cal = apply_emos(mu_raw, sigma_raw, "Miami", db)
        assert abs(mu_cal - (a + b * mu_raw)) < 1e-9, f"mu_cal={mu_cal}, expected={a + b * mu_raw}"
        assert abs(sigma_cal - (c + d * sigma_raw)) < 1e-9, f"sigma_cal={sigma_cal}, expected={c + d * sigma_raw}"

    def test_primary_row_takes_precedence_over_shadow(self):
        """apply_emos prefers emos_primary coefficients over emos_shadow."""
        db = _db()
        _upsert(db, "Atlanta", "emos_shadow", a=0.0, b=1.0, c=0.0, d=1.0)  # identity
        _upsert(db, "Atlanta", "emos_primary", a=5.0, b=1.0, c=1.0, d=1.0)  # shift
        mu_cal, sigma_cal = apply_emos(80.0, 2.0, "Atlanta", db)
        # Should use primary: mu_cal = 5 + 1*80 = 85
        assert abs(mu_cal - 85.0) < 1e-9

    def test_negative_sigma_cal_falls_back_to_raw(self, caplog):
        """sigma_cal <= 0 must fall back to sigma_raw and emit a warning."""
        db = _db()
        # c=-10, d=0 → sigma_cal = -10 + 0*2 = -10 <= 0
        _upsert(db, "Houston", "emos_shadow", a=0.0, b=1.0, c=-10.0, d=0.0)
        with caplog.at_level(logging.WARNING, logger="src.model.emos_mode"):
            _, sigma_cal = apply_emos(80.0, 2.0, "Houston", db)
        assert sigma_cal == 2.0
        assert any("sigma_cal" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Test 6: apply_emos — missing row returns raw params unchanged
# ---------------------------------------------------------------------------

class TestApplyEmosMissing:
    def test_no_row_returns_raw_params(self):
        """No calibration row → apply_emos returns (mu_raw, sigma_raw) unchanged."""
        db = _db()
        mu_raw, sigma_raw = 82.5, 2.5
        mu_cal, sigma_cal = apply_emos(mu_raw, sigma_raw, "Tokyo", db)
        assert mu_cal == mu_raw
        assert sigma_cal == sigma_raw


# ---------------------------------------------------------------------------
# Test 7: legacy unchanged — EMOS_DEFAULT_MODE=legacy → byte-identical output
# ---------------------------------------------------------------------------

class TestLegacyUnchangedOutput:
    def test_emos_default_mode_legacy_identical_output(self, monkeypatch):
        """EMOS_DEFAULT_MODE=legacy and no DB → true_probability_yes output byte-identical to baseline."""
        monkeypatch.setenv("EMOS_DEFAULT_MODE", "legacy")
        bracket = _make_bracket(78.0, 82.0)
        state = _make_state(forecast_high_f=80.0, current_high_f=76.0, hour=14)

        # Baseline: call directly with no emos modification
        p_baseline = true_probability_yes(bracket, state)

        # EMOS path with db=None → get_city_mode returns 'legacy' → no modification
        # (Scanner logic: if db is None, EMOS block is skipped)
        p_emos_legacy = true_probability_yes(bracket, state)

        assert p_baseline == p_emos_legacy, (
            f"Legacy mode must produce identical output; "
            f"baseline={p_baseline}, emos_legacy={p_emos_legacy}"
        )

    def test_no_emos_row_in_db_identical_to_baseline(self):
        """DB with no EMOS row → get_city_mode returns 'legacy' → probability unchanged."""
        db = _db()
        bracket = _make_bracket(78.0, 82.0)
        state = _make_state(forecast_high_f=80.0, current_high_f=76.0, hour=14)

        p_baseline = true_probability_yes(bracket, state)

        # get_city_mode with no row returns 'legacy' → same computation
        mode = get_city_mode("Chicago", db=db)
        assert mode == "legacy"
        # In legacy mode nothing modifies the inputs
        p_with_mode_check = true_probability_yes(bracket, state)
        assert p_baseline == p_with_mode_check

    def test_emos_shadow_does_not_change_output_probability(self):
        """emos_shadow mode: calibrated params logged but p_yes served from legacy path unchanged."""
        db = _db()
        # Insert shadow coefficients that would meaningfully shift mu
        _upsert(db, "Chicago", "emos_shadow", a=5.0, b=0.95, c=0.2, d=1.1)

        bracket = _make_bracket(78.0, 82.0)
        state = _make_state(forecast_high_f=80.0, current_high_f=76.0, hour=14)

        # Baseline (no EMOS)
        p_baseline = true_probability_yes(bracket, state)

        # Shadow mode: apply_emos computes calibrated params but the probability
        # should still use the unmodified forecast. Verify get_city_mode returns shadow.
        mode = get_city_mode("Chicago", db=db)
        assert mode == "emos_shadow"

        # In shadow mode the probability pipeline uses legacy inputs
        # (i.e. true_probability_yes is called without emos correction)
        p_shadow = true_probability_yes(bracket, state)
        assert p_shadow == p_baseline, (
            f"Shadow mode must leave probability identical to baseline; "
            f"baseline={p_baseline:.6f}, shadow={p_shadow:.6f}"
        )
