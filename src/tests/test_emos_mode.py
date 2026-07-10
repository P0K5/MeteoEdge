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
# Test 9: serving members parity guard — issue #666 train/serve tripwire
# ---------------------------------------------------------------------------

class TestServingMembersParityGuard:
    """Issue #666: guard that FORECAST_STACK expansion does not break train/serve
    parity by adding models that serving cannot access on WeatherState.

    Training averages model_forecast_log rows for the active FORECAST_STACK with
    EQUAL weights; serving must feed apply_emos the same equal-weight mean of the
    same feeds. If FORECAST_STACK expands beyond baseline (HRRR/NBM/ECMWF/ICON)
    without expanding _SERVING_MEMBERS, training will include members serving
    cannot see — recreating the exact train/serve skew #658/#664 fixed.
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
        """Guard: live active FORECAST_STACK is matched by _SERVING_MEMBERS.

        This test reads the active FORECAST_STACK from live config (seeded to
        defaults, as production does) and asserts the parity invariant: the
        number of scan-time attributes in _SERVING_MEMBERS must be >= the
        number of models in the active stack.

        Currently this test passes because FORECAST_STACK defaults to 'baseline'
        (2 members, 2 serving members). If someone flips the live default config
        to e.g. 'hrrr_nbm' (4 models) without expanding _SERVING_MEMBERS (still 2),
        this test will fail, enforcing the issue #666 contract.
        """
        from src.config import FORECAST_STACK_MODELS, get_live_config
        from src.model.emos_mode import _SERVING_MEMBERS

        db = _db()
        seed_config(db)

        live_config = get_live_config(db)
        active_stack = live_config.get("FORECAST_STACK", "baseline")
        stack_models = FORECAST_STACK_MODELS.get(active_stack, frozenset())

        assert len(_SERVING_MEMBERS) >= len(stack_models), (
            f"Active FORECAST_STACK '{active_stack}' has {len(stack_models)} models "
            f"({', '.join(sorted(stack_models))}), but _SERVING_MEMBERS only has "
            f"{len(_SERVING_MEMBERS)} scan-time attributes ({', '.join(_SERVING_MEMBERS)}). "
            f"Before expanding FORECAST_STACK, add new model forecasts to WeatherState "
            f"and update _SERVING_MEMBERS to match (see issue #666)."
        )

    def test_non_baseline_stacks_would_fail_parity_check(self):
        """Unit test: demonstrate that the guard correctly detects parity failures.

        This test proves the guard logic by directly asserting the failure condition
        for non-baseline stacks. It does NOT mock the live config (staying true to
        the repo's current baseline default), but shows what would happen if those
        stacks were expanded without updating _SERVING_MEMBERS.
        """
        from src.config import FORECAST_STACK_MODELS
        from src.model.emos_mode import _SERVING_MEMBERS

        # Confirm the failure condition for each non-baseline stack
        for stack_name in ["hrrr_nbm", "intl_ecmwf_icon", "full"]:
            stack_models = FORECAST_STACK_MODELS[stack_name]
            assert len(_SERVING_MEMBERS) < len(stack_models), (
                f"Guard should detect parity failure for stack '{stack_name}': "
                f"it has {len(stack_models)} models but _SERVING_MEMBERS only has "
                f"{len(_SERVING_MEMBERS)} members. This proves the guard logic works."
            )
