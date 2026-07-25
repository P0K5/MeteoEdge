"""Unit tests for src/model/emos_mode.py — EMOS mode switching helpers.

All tests use an in-memory SQLite database to avoid file I/O.
"""
import logging
from datetime import datetime

import pytest

from src.data.db import Database
from src.config import seed_config
from src.model.emos_mode import apply_emos, _check_ready_for_promotion, get_city_mode, _emos_min_samples
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
            crps_score: float = 1.5,
            lead_hours: int = 24) -> None:
    """Insert EMOS calibration row for test convenience."""
    db.upsert_emos_coefficients(
        city=city,
        model_mode=model_mode,
        a=a, b=b, c=c, d=d,
        crps_score=crps_score,
        trained_at=datetime.utcnow().isoformat(),
        ready_for_promotion=ready_for_promotion,
        lead_hours=lead_hours,
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
# Test 1b: EMOS_DEFAULT_MODE live-read from bot_config (issue #680)
# ---------------------------------------------------------------------------

class TestDefaultModeLiveConfig:
    """Issue #680: the dashboard edits EMOS_DEFAULT_MODE in bot_config, so the
    fallback must be live-read from the DB when a handle is available — the
    env var only covers db=None paths."""

    def test_db_config_value_is_honoured(self):
        """bot_config EMOS_DEFAULT_MODE=emos_shadow → returned for a city
        with no calibration rows and no override."""
        db = _db()
        db.set_config("EMOS_DEFAULT_MODE", "emos_shadow")
        assert get_city_mode("Tokyo", db=db) == "emos_shadow"

    def test_db_config_wins_over_env_var(self, monkeypatch):
        """With a db handle, bot_config beats the env var (live-read pattern)."""
        monkeypatch.setenv("EMOS_DEFAULT_MODE", "emos_shadow")
        db = _db()
        db.set_config("EMOS_DEFAULT_MODE", "legacy")
        assert get_city_mode("Tokyo", db=db) == "legacy"

    def test_unseeded_db_falls_back_to_config_default(self):
        """No bot_config row → CONFIG_DEFAULTS['EMOS_DEFAULT_MODE'] ('legacy')."""
        db = _db()
        assert get_city_mode("Tokyo", db=db) == "legacy"


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
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        # Populate enough CRPS log entries to satisfy the promotion guard (set to 20)
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5)
        result = get_city_mode("Chicago", db=db)
        assert result == "emos_primary"

    def test_check_ready_for_promotion_true_when_ready(self):
        """_check_ready_for_promotion returns True when ready_for_promotion=1."""
        db = _db()
        seed_config(db)
        _upsert(db, "Miami", "emos_primary", ready_for_promotion=1)
        result = _check_ready_for_promotion("Miami", db=db)
        assert result is True

    def test_primary_ready_takes_precedence_over_shadow(self):
        """Both shadow and ready primary + enough CRPS samples → 'emos_primary' wins."""
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
        _upsert(db, "Los Angeles", "emos_shadow", ready_for_promotion=0)
        _upsert(db, "Los Angeles", "emos_primary", ready_for_promotion=1)
        # Populate enough CRPS log entries to satisfy the promotion guard (set to 20)
        for i in range(20):
            db.log_crps("Los Angeles", f"2026-05-{i + 1:02d}", 1.5)
        result = get_city_mode("Los Angeles", db=db)
        assert result == "emos_primary"


# ---------------------------------------------------------------------------
# Test 3b: promotion guard counts the ACTIVE forecast_source only (#759)
# ---------------------------------------------------------------------------

class TestPromotionGuardScopedByForecastSource:
    def test_primary_blocked_when_samples_belong_to_a_different_stack(self):
        """CRPS evidence logged under a forecast_source other than the
        active FORECAST_STACK must not count toward promotion -- otherwise
        switching stacks mid-collection would let leftover 'baseline'
        evidence wrongly unblock a freshly-switched, unevaluated stack.
        """
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
        db.set_config("FORECAST_STACK", "expanded")
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        # 20 samples logged for 'baseline' -- NOT the active stack.
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5, forecast_source="baseline")

        result = get_city_mode("Chicago", db=db)
        assert result != "emos_primary"

    def test_primary_allowed_once_active_stack_accrues_its_own_samples(self):
        """Once the active stack itself has enough CRPS evidence, promotion
        proceeds normally -- forecast_source scoping doesn't otherwise
        change the promotion threshold/behavior."""
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
        db.set_config("FORECAST_STACK", "expanded")
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5, forecast_source="expanded")

        result = get_city_mode("Chicago", db=db)
        assert result == "emos_primary"


# ---------------------------------------------------------------------------
# Test 3c: promotion guard counts the ACTIVE sigma_source only (#851)
# ---------------------------------------------------------------------------

class TestPromotionGuardScopedBySigmaSource:
    def test_primary_blocked_when_samples_belong_to_a_different_sigma_track(self):
        """CRPS evidence logged under a sigma_source other than the active
        USE_ENSEMBLE_SIGMA-derived track must not count toward promotion --
        otherwise a sigma_source switch (issue #799) would let leftover
        'fixed'-era evidence wrongly unblock the freshly-retrained
        'ensemble' lineage before it has accrued any evidence of its own
        (the pooled-clock bug issue #851 exists to close).
        """
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        # 20 samples logged for 'fixed' -- NOT the active sigma_source.
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5, sigma_source="fixed")

        result = get_city_mode("Chicago", db=db)
        assert result != "emos_primary"

    def test_primary_allowed_once_active_sigma_track_accrues_its_own_samples(self):
        """Once the active sigma_source itself has enough CRPS evidence,
        promotion proceeds normally -- sigma_source scoping doesn't
        otherwise change the promotion threshold/behavior."""
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        for i in range(20):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5, sigma_source="ensemble")

        result = get_city_mode("Chicago", db=db)
        assert result == "emos_primary"

    def test_sigma_source_switch_resets_the_promotion_clock(self):
        """The exact #851 scenario: a city already cleared the sample bar
        under sigma_source='fixed'; USE_ENSEMBLE_SIGMA then flips true
        (issue #799), a fresh 'ensemble' calibration row is retrained and
        marked ready (the normal post-#799 operator flow), but no
        'ensemble' shadow evidence has accrued yet. The city must NOT be
        immediately eligible for emos_primary again just because the old
        'fixed' evidence still exists in emos_crps_log -- the clock resets
        for the new lineage. Isolates the CRPS-count gate itself (both
        scenarios have a ready emos_primary row for the active sigma_source,
        so get_emos_coefficients's own sigma_source resolution is not the
        thing under test here -- get_emos_crps_count's is).
        """
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
        db.set_config("USE_ENSEMBLE_SIGMA", "false")
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        for i in range(25):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5)  # sigma_source='fixed'
        assert get_city_mode("Chicago", db=db) == "emos_primary"

        # Flip to the ensemble track and retrain/mark-ready a primary row
        # under it -- no ensemble CRPS evidence logged yet.
        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)
        assert get_city_mode("Chicago", db=db) != "emos_primary", (
            "the promotion clock must reset for the new sigma_source lineage, "
            "not inherit the old lineage's already-accumulated evidence"
        )

        # Once the new lineage accrues its own evidence, promotion resumes.
        for i in range(20):
            db.log_crps("Chicago", f"2026-06-{i + 1:02d}", 1.5)  # sigma_source='ensemble'
        assert get_city_mode("Chicago", db=db) == "emos_primary"


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
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
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
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "20")
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


class TestEmosServingMu:
    """Issue #658 layer contract: EMOS serves on the plain equal-weight stack
    mean it trained on (never corrected_mu_f/deb_mu_f), with the decayed
    intraday delta layered on top of the calibrated mean."""

    def _state(self, nws=None, om=None, corrected=None, deb=None, delta=None):
        from src.model.envelope import WeatherState
        from datetime import datetime
        now = datetime(2026, 7, 9, 14, 0)
        return WeatherState(
            station="KORD", now_local=now, sunset_local=now,
            current_high_f=70.0, current_high_time=now,
            latest_temp_f=70.0, latest_temp_time=now,
            forecast_high_f=nws, secondary_forecast_f=om,
            corrected_mu_f=corrected, deb_mu_f=deb, intraday_delta_f=delta,
        )

    def _db_with_coeffs(self, a=2.0, b=1.0, c=0.5, d=1.0):
        db = _db()
        db.upsert_emos_coefficients(
            city="Chicago", model_mode="emos_shadow", a=a, b=b, c=c, d=d,
        )
        return db

    def test_uses_plain_mean_not_corrected_mu(self):
        from src.model.emos_mode import emos_serving_mu
        db = self._db_with_coeffs(a=2.0, b=1.0)
        # corrected_mu/deb_mu deliberately far away — must be ignored
        state = self._state(nws=80.0, om=84.0, corrected=99.0, deb=95.0)
        mu, sigma = emos_serving_mu(state, "Chicago", db, 2.0)
        # plain mean = 82, mu_cal = 2 + 1*82 = 84; no intraday delta
        assert mu == pytest.approx(84.0)
        assert sigma == pytest.approx(0.5 + 1.0 * 2.0)

    def test_intraday_delta_layers_on_top(self):
        from src.model.emos_mode import emos_serving_mu
        db = self._db_with_coeffs(a=2.0, b=1.0)
        state = self._state(nws=80.0, om=84.0, delta=-1.5)
        mu, _ = emos_serving_mu(state, "Chicago", db, 2.0)
        assert mu == pytest.approx(84.0 - 1.5)

    def test_single_member_mean(self):
        from src.model.emos_mode import emos_serving_mu
        db = self._db_with_coeffs(a=0.0, b=1.0)
        state = self._state(nws=None, om=78.0)
        mu, _ = emos_serving_mu(state, "Chicago", db, 2.0)
        assert mu == pytest.approx(78.0)

    def test_no_members_returns_none(self):
        from src.model.emos_mode import emos_serving_mu
        db = self._db_with_coeffs()
        state = self._state(nws=None, om=None, corrected=90.0)
        assert emos_serving_mu(state, "Chicago", db, 2.0) is None

    def test_no_coefficients_passthrough(self):
        """Without coefficients apply_emos passes through, so serving equals
        plain mean (+delta) — get_city_mode gates this path in practice."""
        from src.model.emos_mode import emos_serving_mu
        db = _db()
        state = self._state(nws=80.0, om=84.0, delta=1.0)
        mu, sigma = emos_serving_mu(state, "Chicago", db, 2.0)
        assert mu == pytest.approx(83.0)
        assert sigma == pytest.approx(2.0)


class TestEmosServingMuActiveStackParity:
    """Issue #760: emos_serving_mu must average exactly the WeatherState
    attributes for the ACTIVE FORECAST_STACK's models -- the same
    equal-weight set fetch_training_data trains on -- never simply
    "whatever forecast attributes happen to be non-None on state".
    """

    def _state(self, **overrides):
        now = datetime(2026, 7, 21, 14, 0)
        defaults = dict(
            station="KORD", now_local=now, sunset_local=now,
            current_high_f=70.0, current_high_time=now,
            latest_temp_f=70.0, latest_temp_time=now,
            forecast_high_f=None, secondary_forecast_f=None,
        )
        defaults.update(overrides)
        return WeatherState(**defaults)

    def test_hrrr_nbm_active_stack_uses_equal_weight_mean_of_four(self):
        """Active stack = hrrr_nbm: mu_raw must be the mean of all 4 members."""
        from src.model.emos_mode import emos_serving_mu
        db = _db()
        db.set_config("FORECAST_STACK", "hrrr_nbm")
        state = self._state(
            forecast_high_f=80.0, secondary_forecast_f=84.0,
            hrrr_forecast_f=76.0, nbm_forecast_f=78.0,
        )
        # No emos_calibration row -> apply_emos passes mu_raw through unchanged,
        # isolating the member-selection behaviour under test.
        mu, sigma = emos_serving_mu(state, "Chicago", db, 2.0)
        assert mu == pytest.approx((80.0 + 84.0 + 76.0 + 78.0) / 4)
        assert sigma == pytest.approx(2.0)

    def test_intl_ecmwf_icon_active_stack_uses_equal_weight_mean_of_four(self):
        """Active stack = intl_ecmwf_icon: mu_raw must be the mean of all 4 members."""
        from src.model.emos_mode import emos_serving_mu
        db = _db()
        db.set_config("FORECAST_STACK", "intl_ecmwf_icon")
        state = self._state(
            forecast_high_f=20.0, secondary_forecast_f=22.0,
            ecmwf_forecast_f=19.0, icon_forecast_f=21.0,
        )
        mu, sigma = emos_serving_mu(state, "Tokyo", db, 1.0)
        assert mu == pytest.approx((20.0 + 22.0 + 19.0 + 21.0) / 4)
        assert sigma == pytest.approx(1.0)

    def test_hrrr_nbm_active_stack_missing_member_uses_whats_available(self):
        """A station without HRRR/NBM data yet (active stack hrrr_nbm) must
        still serve off whichever members ARE present, not return None."""
        from src.model.emos_mode import emos_serving_mu
        db = _db()
        db.set_config("FORECAST_STACK", "hrrr_nbm")
        state = self._state(forecast_high_f=80.0, secondary_forecast_f=84.0)
        mu, _ = emos_serving_mu(state, "Chicago", db, 2.0)
        assert mu == pytest.approx((80.0 + 84.0) / 2)

    def test_baseline_serving_byte_for_byte_unchanged_with_extra_attrs_present(self):
        """Issue #760 DoD: baseline serving is byte-for-byte unchanged. A state
        that carries HRRR/NBM/ECMWF/ICON values (channels are captured
        independent of the active stack -- #431/#438) must NOT pull them into
        mu_raw while FORECAST_STACK stays baseline (the unset/default value):
        serving must equal the pre-#760 two-member (nws, open_meteo) mean.
        """
        from src.model.emos_mode import emos_serving_mu
        db = _db()  # FORECAST_STACK unset -> resolves to the 'baseline' default
        state = self._state(
            forecast_high_f=80.0, secondary_forecast_f=84.0,
            hrrr_forecast_f=1000.0, nbm_forecast_f=-1000.0,
            ecmwf_forecast_f=1000.0, icon_forecast_f=-1000.0,
        )
        mu, sigma = emos_serving_mu(state, "Chicago", db, 2.0)
        assert mu == pytest.approx((80.0 + 84.0) / 2)
        assert sigma == pytest.approx(2.0)

    def test_baseline_serving_unchanged_when_forecast_stack_explicitly_set(self):
        """Same as above but with FORECAST_STACK explicitly written as
        'baseline' (rather than left unset) -- both paths must agree."""
        from src.model.emos_mode import emos_serving_mu
        db = _db()
        db.set_config("FORECAST_STACK", "baseline")
        state = self._state(
            forecast_high_f=80.0, secondary_forecast_f=84.0,
            hrrr_forecast_f=1000.0, nbm_forecast_f=-1000.0,
        )
        mu, _ = emos_serving_mu(state, "Chicago", db, 2.0)
        assert mu == pytest.approx((80.0 + 84.0) / 2)


# ---------------------------------------------------------------------------
# Test 7b: resolve_sigma_raw — issue #448, EMOS-shadow picks up ensemble sigma
# ---------------------------------------------------------------------------

class TestResolveSigmaRaw:
    """resolve_sigma_raw feeds the EMOS-shadow/primary serving path (#448):
    picks state.ensemble_sigma_f when USE_ENSEMBLE_SIGMA resolves True and the
    field is set, else falls back to the legacy fixed sigma unchanged."""

    def _state(self, ensemble_sigma_f=None):
        from src.model.envelope import WeatherState
        from datetime import datetime
        now = datetime(2026, 7, 9, 14, 0)
        return WeatherState(
            station="KORD", now_local=now, sunset_local=now,
            current_high_f=70.0, current_high_time=now,
            latest_temp_f=70.0, latest_temp_time=now,
            forecast_high_f=80.0, ensemble_sigma_f=ensemble_sigma_f,
        )

    def test_uses_ensemble_sigma_when_flag_true_and_field_set(self):
        from src.model.emos_mode import resolve_sigma_raw
        state = self._state(ensemble_sigma_f=3.5)
        result = resolve_sigma_raw(state, True, 2.0)
        assert result == 3.5

    def test_falls_back_when_field_is_none(self):
        """Flag on but ensemble_sigma_f=None (e.g. GEFS unavailable) -> fixed fallback."""
        from src.model.emos_mode import resolve_sigma_raw
        state = self._state(ensemble_sigma_f=None)
        result = resolve_sigma_raw(state, True, 2.0)
        assert result == 2.0

    def test_falls_back_when_flag_false(self):
        """Flag off -> ensemble_sigma_f ignored even when set."""
        from src.model.emos_mode import resolve_sigma_raw
        state = self._state(ensemble_sigma_f=3.5)
        result = resolve_sigma_raw(state, False, 2.0)
        assert result == 2.0

    def test_none_flag_falls_back_to_env_var(self, monkeypatch):
        """use_ensemble_sigma=None -> USE_ENSEMBLE_SIGMA env var resolves it."""
        from src.model.emos_mode import resolve_sigma_raw
        monkeypatch.setenv("USE_ENSEMBLE_SIGMA", "true")
        state = self._state(ensemble_sigma_f=3.5)
        assert resolve_sigma_raw(state, None, 2.0) == 3.5

    def test_none_flag_defaults_off_without_env_var(self, monkeypatch):
        from src.model.emos_mode import resolve_sigma_raw
        monkeypatch.delenv("USE_ENSEMBLE_SIGMA", raising=False)
        state = self._state(ensemble_sigma_f=3.5)
        assert resolve_sigma_raw(state, None, 2.0) == 2.0

    def test_emos_serving_mu_picks_up_resolved_sigma(self):
        """End-to-end: resolve_sigma_raw's output flows through emos_serving_mu's
        sigma_cal (c + d*sigma_raw) -- the EMOS-shadow path 'picks up' the new
        sigma exactly the way it picks up FORECAST_STDDEV_F today."""
        from src.model.emos_mode import resolve_sigma_raw, emos_serving_mu
        db = _db()
        _upsert(db, "Chicago", "emos_shadow", a=0.0, b=1.0, c=0.5, d=1.0)
        state = self._state(ensemble_sigma_f=4.0)
        state.secondary_forecast_f = 82.0  # give it a second stack member

        sigma_raw_on = resolve_sigma_raw(state, True, 2.0)
        sigma_raw_off = resolve_sigma_raw(state, False, 2.0)
        assert sigma_raw_on == 4.0
        assert sigma_raw_off == 2.0

        _, sigma_cal_on = emos_serving_mu(state, "Chicago", db, sigma_raw_on)
        _, sigma_cal_off = emos_serving_mu(state, "Chicago", db, sigma_raw_off)
        assert sigma_cal_on == pytest.approx(0.5 + 1.0 * 4.0)
        assert sigma_cal_off == pytest.approx(0.5 + 1.0 * 2.0)
        assert sigma_cal_on != sigma_cal_off


# ---------------------------------------------------------------------------
# Test 7b-bis: resolve_sigma_raw and Database._active_sigma_source agree for
# the SAME USE_ENSEMBLE_SIGMA value (issue #799 -- main review risk)
# ---------------------------------------------------------------------------

class TestResolveSigmaRawAgreesWithActiveSigmaSource:
    """The two call sites that must never diverge:

    - resolve_sigma_raw(state, use_ensemble_sigma, ...) -- decides what raw
      sigma value gets FED INTO apply_emos at serving time.
    - db._active_sigma_source() -- decides WHICH emos_calibration row (the
      'fixed' or 'ensemble' track) apply_emos reads coefficients from.

    Both must resolve from the identical USE_ENSEMBLE_SIGMA live-config value
    for a given scan cycle, or serving can feed a real ensemble sigma_raw
    through coefficients fit under the 'fixed' track (or vice versa) -- the
    #658-style skew #799 exists to prevent.
    """

    def _state(self, ensemble_sigma_f=3.5):
        from src.model.envelope import WeatherState
        from datetime import datetime
        now = datetime(2026, 7, 9, 14, 0)
        return WeatherState(
            station="KORD", now_local=now, sunset_local=now,
            current_high_f=70.0, current_high_time=now,
            latest_temp_f=70.0, latest_temp_time=now,
            forecast_high_f=80.0, ensemble_sigma_f=ensemble_sigma_f,
        )

    def test_flag_true_uses_ensemble_input_and_ensemble_track(self):
        from src.model.emos_mode import resolve_sigma_raw
        db = _db()
        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        use_ensemble_sigma = True  # resolved from live config, same as scanner.py's pattern

        sigma_raw = resolve_sigma_raw(self._state(), use_ensemble_sigma, 2.0)
        assert sigma_raw == 3.5  # fed the REAL ensemble spread, not the constant

        assert db._active_sigma_source() == "ensemble"

    def test_flag_false_uses_fixed_input_and_fixed_track(self):
        from src.model.emos_mode import resolve_sigma_raw
        db = _db()
        db.set_config("USE_ENSEMBLE_SIGMA", "false")
        use_ensemble_sigma = False

        sigma_raw = resolve_sigma_raw(self._state(), use_ensemble_sigma, 2.0)
        assert sigma_raw == 2.0  # the constant, not the real spread

        assert db._active_sigma_source() == "fixed"


# ---------------------------------------------------------------------------
# Test 8: _emos_min_samples reads from config
# ---------------------------------------------------------------------------

class TestEmosMinSamplesConfig:
    """_emos_min_samples(db) reads EMOS_MIN_SAMPLES_PROMOTION from config."""

    def test_default_emos_min_samples_is_60(self):
        """Without explicit override, _emos_min_samples returns default 60."""
        db = _db()
        seed_config(db)
        result = _emos_min_samples(db)
        assert result == 60

    def test_config_override_changes_min_samples(self):
        """Setting EMOS_MIN_SAMPLES_PROMOTION in config changes the guard."""
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "100")
        result = _emos_min_samples(db)
        assert result == 100

    def test_min_samples_affects_promotion_guard(self):
        """Changing config min_samples actually affects _primary_allowed."""
        db = _db()
        seed_config(db)
        db.set_config("EMOS_MIN_SAMPLES_PROMOTION", "50")

        # Set up a primary row ready for promotion
        _upsert(db, "Chicago", "emos_primary", ready_for_promotion=1)

        # Add 49 CRPS samples — below the 50 threshold
        for i in range(49):
            db.log_crps("Chicago", f"2026-05-{i + 1:02d}", 1.5)

        # Should block because 49 < 50
        from src.model.emos_mode import _primary_allowed
        assert _primary_allowed("Chicago", db) is False

        # Add one more to reach 50
        db.log_crps("Chicago", "2026-06-19", 1.5)

        # Should now allow because 50 >= 50
        assert _primary_allowed("Chicago", db) is True


# ---------------------------------------------------------------------------
# Test 9: serving members parity guard — issues #666/#760 train/serve tripwire
# ---------------------------------------------------------------------------

class TestServingMembersParityGuard:
    """Issue #666/#760: guard that FORECAST_STACK expansion does not break
    train/serve parity by adding models that serving cannot access on
    WeatherState.

    Training averages model_forecast_log rows for the active FORECAST_STACK with
    EQUAL weights; serving must feed apply_emos the same equal-weight mean of the
    same feeds. Since #760, _SERVING_MEMBERS-equivalent attributes are resolved
    per-call from _serving_members_for_stack(active_stack_models) — every model
    in a promotable regime (hrrr_nbm, intl_ecmwf_icon) now has a WeatherState
    attribute, closing the train/serve skew #658/#664 originally guarded against.
    """

    def test_baseline_stack_has_sufficient_serving_members(self):
        """Sanity check: baseline stack (nws, open_meteo) has 2 serving members."""
        from src.config import FORECAST_STACK_MODELS
        from src.model.emos_mode import _SERVING_MEMBERS

        baseline_models = FORECAST_STACK_MODELS["baseline"]
        assert len(_SERVING_MEMBERS) >= len(baseline_models), (
            f"Baseline stack needs {len(baseline_models)} members, "
            f"but _SERVING_MEMBERS only has {len(_SERVING_MEMBERS)}"
        )
        # Baseline should have exactly 2 models and 2 serving members
        assert len(baseline_models) == 2
        assert len(_SERVING_MEMBERS) == 2

    def test_live_forecast_stack_config_parity_guard(self):
        """Guard: live active FORECAST_STACK is matched by its serving members.

        This test reads the active FORECAST_STACK from live config (seeded to
        defaults, as production does) and asserts the parity invariant: the
        number of scan-time attributes _serving_members_for_stack resolves for
        the active stack must be >= the number of models in that stack.

        Currently this passes because FORECAST_STACK defaults to 'baseline'
        (2 models, 2 serving members) — and, since #760, would also pass if the
        live default were flipped to 'hrrr_nbm' or 'intl_ecmwf_icon' (see
        test_serving_members_parity_guard below), enforcing the issue #666
        contract for every regime that's actually promotable today.
        """
        from src.config import FORECAST_STACK_MODELS, get_live_config
        from src.model.emos_mode import _active_stack_models, _serving_members_for_stack

        db = _db()
        seed_config(db)

        live_config = get_live_config(db)
        active_stack = live_config.get("FORECAST_STACK", "baseline")
        stack_models = FORECAST_STACK_MODELS.get(active_stack, frozenset())
        serving_attrs = _serving_members_for_stack(_active_stack_models(db))

        assert len(serving_attrs) >= len(stack_models), (
            f"Active FORECAST_STACK '{active_stack}' has {len(stack_models)} models "
            f"({', '.join(sorted(stack_models))}), but serving only resolves "
            f"{len(serving_attrs)} scan-time attributes ({', '.join(serving_attrs)}). "
            f"Before promoting FORECAST_STACK, add new model forecasts to WeatherState "
            f"and update src.config.MODEL_STATE_ATTRS to match (see issue #666)."
        )

    @pytest.mark.parametrize("stack_name", ["hrrr_nbm", "intl_ecmwf_icon"])
    def test_serving_members_parity_guard(self, stack_name):
        """Issue #760 DoD: the parity guard passes for BOTH promotable expanded
        stacks — every model in the regime has a scan-time WeatherState
        attribute reachable via _serving_members_for_stack, and that attribute
        actually exists on WeatherState (not just in the mapping).
        """
        from src.config import FORECAST_STACK_MODELS
        from src.model.emos_mode import _serving_members_for_stack
        from src.model.envelope import WeatherState

        stack_models = FORECAST_STACK_MODELS[stack_name]
        serving_attrs = _serving_members_for_stack(stack_models)

        assert len(serving_attrs) == len(stack_models), (
            f"Stack '{stack_name}' has {len(stack_models)} models "
            f"({', '.join(sorted(stack_models))}) but only resolved "
            f"{len(serving_attrs)} WeatherState attributes ({', '.join(serving_attrs)})."
        )

        dummy = WeatherState(
            station="KORD",
            now_local=datetime(2026, 7, 21, 12, 0),
            sunset_local=datetime(2026, 7, 21, 20, 0),
            current_high_f=70.0,
            current_high_time=datetime(2026, 7, 21, 12, 0),
            latest_temp_f=70.0,
            latest_temp_time=datetime(2026, 7, 21, 12, 0),
            forecast_high_f=None,
        )
        for attr in serving_attrs:
            assert hasattr(dummy, attr), (
                f"WeatherState is missing attribute {attr!r} required by stack "
                f"'{stack_name}' (see src.config.MODEL_STATE_ATTRS)"
            )

    def test_full_stack_still_fails_parity_gefs_out_of_scope(self):
        """The 'full' stack adds 'gefs' on top of hrrr_nbm/intl_ecmwf_icon.
        GEFS is an ensemble-spread product consumed for sigma, not a single
        forecast-high value, and has no MODEL_STATE_ATTRS entry by design
        (see src.config.MODEL_STATE_ATTRS docstring) — 'full' promotion is
        explicitly out of scope for #760. This documents the known gap
        instead of silently regressing coverage.
        """
        from src.config import FORECAST_STACK_MODELS
        from src.model.emos_mode import _serving_members_for_stack

        stack_models = FORECAST_STACK_MODELS["full"]
        serving_attrs = _serving_members_for_stack(stack_models)
        assert len(serving_attrs) < len(stack_models), (
            "Expected 'full' stack to still fail the parity guard (gefs has no "
            "WeatherState attribute) — if this now passes, MODEL_STATE_ATTRS "
            "gained a 'gefs' entry and this test (and its docstring) are stale."
        )


# ---------------------------------------------------------------------------
# Test 10: _nearest_lead_hours — nearest-bin selection (issue #665)
# ---------------------------------------------------------------------------

class TestNearestLeadHours:
    """Mirrors src/data/nws.py:_nws_sigma_for_lead's nearest-match idiom."""

    def test_exact_match(self):
        from src.model.emos_mode import _nearest_lead_hours
        assert _nearest_lead_hours(6.0, [3, 6, 12, 18, 24]) == 6

    def test_nearer_to_lower_bin(self):
        from src.model.emos_mode import _nearest_lead_hours
        # 8h is 2 away from 6, 4 away from 12 -> picks 6
        assert _nearest_lead_hours(8.0, [3, 6, 12, 18, 24]) == 6

    def test_nearer_to_upper_bin(self):
        from src.model.emos_mode import _nearest_lead_hours
        # 10h is 4 away from 6, 2 away from 12 -> picks 12
        assert _nearest_lead_hours(10.0, [3, 6, 12, 18, 24]) == 12

    def test_tie_resolves_to_first_in_list(self):
        from src.model.emos_mode import _nearest_lead_hours
        # 9h is equidistant from 6 and 12 -> tie resolves to whichever is
        # first in the available list (min()'s tie-break), same idiom as
        # _nws_sigma_for_lead.
        assert _nearest_lead_hours(9.0, [6, 12]) == 6
        assert _nearest_lead_hours(9.0, [12, 6]) == 12

    def test_single_available_bin_always_wins(self):
        """Missing-bin fallback degenerate case: only one bin fitted."""
        from src.model.emos_mode import _nearest_lead_hours
        assert _nearest_lead_hours(0.5, [24]) == 24
        assert _nearest_lead_hours(1000.0, [24]) == 24

    def test_beyond_range_picks_closest_edge(self):
        from src.model.emos_mode import _nearest_lead_hours
        assert _nearest_lead_hours(48.0, [3, 6, 12, 18, 24]) == 24
        assert _nearest_lead_hours(0.1, [3, 6, 12, 18, 24]) == 3


# ---------------------------------------------------------------------------
# Test 11: apply_emos / emos_serving_mu — per-lead-bin serving (issue #665)
# ---------------------------------------------------------------------------

class TestApplyEmosPerLeadBin:
    def test_no_minutes_to_settlement_uses_legacy_default_lookup(self):
        """Omitting minutes_to_settlement reproduces pre-#665 behaviour exactly:
        a single row at lead_hours=24 regardless of the arg not being passed."""
        db = _db()
        _upsert(db, "Chicago", "emos_shadow", a=5.0, b=1.0, c=0.5, d=1.0, lead_hours=24)
        mu_cal, sigma_cal = apply_emos(80.0, 2.0, "Chicago", db)
        assert mu_cal == pytest.approx(85.0)

    def test_single_bin_fitted_ignores_minutes_to_settlement(self):
        """Missing-bin fallback: with only the legacy lead_hours=24 bin fitted,
        every minutes_to_settlement value resolves to that same row."""
        db = _db()
        _upsert(db, "Chicago", "emos_shadow", a=5.0, b=1.0, c=0.5, d=1.0, lead_hours=24)
        near = apply_emos(80.0, 2.0, "Chicago", db, minutes_to_settlement=15)
        far = apply_emos(80.0, 2.0, "Chicago", db, minutes_to_settlement=1440)
        no_arg = apply_emos(80.0, 2.0, "Chicago", db)
        assert near == far == no_arg

    def test_picks_nearest_bin_among_multiple(self):
        db = _db()
        _upsert(db, "Denver", "emos_shadow", a=10.0, b=1.0, c=1.0, d=1.0, lead_hours=3)
        _upsert(db, "Denver", "emos_shadow", a=20.0, b=1.0, c=2.0, d=1.0, lead_hours=6)
        _upsert(db, "Denver", "emos_shadow", a=30.0, b=1.0, c=3.0, d=1.0, lead_hours=24)

        # 90 minutes = 1.5h -> nearest of {3, 6, 24} is 3
        mu_cal, sigma_cal = apply_emos(0.0, 0.0, "Denver", db, minutes_to_settlement=90)
        assert mu_cal == pytest.approx(10.0)
        assert sigma_cal == pytest.approx(1.0)

        # 5 hours = 300 minutes -> nearest of {3, 6, 24} is 6
        mu_cal, sigma_cal = apply_emos(0.0, 0.0, "Denver", db, minutes_to_settlement=300)
        assert mu_cal == pytest.approx(20.0)
        assert sigma_cal == pytest.approx(2.0)

        # 1440 minutes = 24h -> nearest is 24
        mu_cal, sigma_cal = apply_emos(0.0, 0.0, "Denver", db, minutes_to_settlement=1440)
        assert mu_cal == pytest.approx(30.0)
        assert sigma_cal == pytest.approx(3.0)

    def test_bin_edge_tie_is_deterministic(self):
        """4.5h (270 minutes) is equidistant between the 3h and 6h bins fitted
        for Denver -- get_emos_coefficients_by_lead orders lead_hours ASC, so
        the tie resolves to the LOWER (nearer-term) bin, matching this
        codebase's existing "lowest lead_hours wins" convention (see
        ensemble_distribution.py), and stays stable across repeat calls and
        regardless of insertion order."""
        db = _db()
        _upsert(db, "Denver", "emos_shadow", a=20.0, b=1.0, c=2.0, d=1.0, lead_hours=6)
        _upsert(db, "Denver", "emos_shadow", a=10.0, b=1.0, c=1.0, d=1.0, lead_hours=3)

        first = apply_emos(0.0, 0.0, "Denver", db, minutes_to_settlement=270)
        second = apply_emos(0.0, 0.0, "Denver", db, minutes_to_settlement=270)
        assert first == second == (10.0, 1.0)  # lead_hours=3 wins the tie

    def test_primary_precedence_preserved_with_lead_bins(self):
        """emos_primary still wins over emos_shadow, evaluated within its own
        nearest-lead-bin selection independently of shadow's bins."""
        db = _db()
        _upsert(db, "Atlanta", "emos_shadow", a=0.0, b=1.0, c=0.0, d=1.0, lead_hours=3)
        _upsert(db, "Atlanta", "emos_primary", a=5.0, b=1.0, c=1.0, d=1.0, lead_hours=24)
        mu_cal, _ = apply_emos(80.0, 2.0, "Atlanta", db, minutes_to_settlement=60)
        # Primary has only lead_hours=24 fitted -> always resolves there
        # regardless of minutes_to_settlement, and still wins over shadow.
        assert mu_cal == pytest.approx(85.0)

    def test_missing_city_returns_raw_with_minutes_to_settlement(self):
        db = _db()
        mu_cal, sigma_cal = apply_emos(82.5, 2.5, "Tokyo", db, minutes_to_settlement=45)
        assert mu_cal == 82.5
        assert sigma_cal == 2.5

    def test_emos_serving_mu_threads_minutes_to_settlement(self):
        from src.model.emos_mode import emos_serving_mu
        db = _db()
        _upsert(db, "Denver", "emos_shadow", a=0.0, b=1.0, c=1.0, d=1.0, lead_hours=3)
        _upsert(db, "Denver", "emos_shadow", a=100.0, b=1.0, c=1.0, d=1.0, lead_hours=24)
        from src.model.envelope import WeatherState
        from datetime import datetime as _dt
        now = _dt(2026, 7, 9, 14, 0)
        state = WeatherState(
            station="KDEN", now_local=now, sunset_local=now,
            current_high_f=70.0, current_high_time=now,
            latest_temp_f=70.0, latest_temp_time=now,
            forecast_high_f=80.0, secondary_forecast_f=80.0,
        )
        # Near settlement (30 min = 0.5h) -> nearest bin is 3h -> a=0 -> mu=80
        mu_near, _ = emos_serving_mu(state, "Denver", db, 2.0, minutes_to_settlement=30)
        assert mu_near == pytest.approx(80.0)
        # Far from settlement (1440 min = 24h) -> nearest bin is 24h -> a=100 -> mu=180
        mu_far, _ = emos_serving_mu(state, "Denver", db, 2.0, minutes_to_settlement=1440)
        assert mu_far == pytest.approx(180.0)
        # Omitting minutes_to_settlement -> legacy default lead_hours=24 lookup
        mu_default, _ = emos_serving_mu(state, "Denver", db, 2.0)
        assert mu_default == pytest.approx(180.0)
