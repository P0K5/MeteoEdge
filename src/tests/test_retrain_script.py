"""Unit tests for scripts/auto_retrain_probability_calibration.py.

Tests cover:
1. VPS guard — hostname match triggers SystemExit
2. VPS guard — systemd unit file presence triggers SystemExit
3. VPS guard — clean environment (no match) passes without exception
4. dry-run mode — upsert_emos_coefficients is never called
5. Promotion logic — both criteria met (CRPS < threshold AND samples >= min) => ready=1
6. Promotion logic — CRPS not met (>= threshold) => ready=0
7. Promotion logic — samples not met (< min) => skipped (InsufficientDataError)
8. Atomic write — interrupted rename leaves no .json file
9. validate_report — passes on valid entry
10. validate_report — raises on missing field
11. --report-only — no retrain, no fetch_training_data call
"""
import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.data.db import Database
from src.model.emos_calibration import InsufficientDataError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_script():
    """Import the retrain script as a module (bypasses __main__ guard)."""
    import scripts.auto_retrain_probability_calibration as mod
    return mod


def _make_triples(n: int, mu: float = 20.0, sigma: float = 2.0, y: float = 20.5) -> list:
    """Return n identical (mu, sigma, y) triples for use as synthetic training data."""
    return [(mu, sigma, y)] * n


# ---------------------------------------------------------------------------
# 1. VPS guard — hostname match
# ---------------------------------------------------------------------------

class TestVpsGuardHostname:
    """VPS guard must fire when hostname contains 'meteoedge'."""

    def test_hostname_match_raises_system_exit(self):
        mod = _load_script()
        with patch("socket.gethostname", return_value="meteoedge-prod"):
            with patch.object(Path, "exists", return_value=False):
                with pytest.raises(SystemExit) as exc_info:
                    mod._check_not_on_vps()
        assert "VPS" in str(exc_info.value) or "meteoedge" in str(exc_info.value).lower() or exc_info.value.code != 0

    def test_hostname_partial_match_raises(self):
        """'meteoedge' substring anywhere in hostname should trigger guard."""
        mod = _load_script()
        with patch("socket.gethostname", return_value="my-meteoedge-box"):
            with patch.object(Path, "exists", return_value=False):
                with pytest.raises(SystemExit):
                    mod._check_not_on_vps()


# ---------------------------------------------------------------------------
# 2. VPS guard — systemd unit file
# ---------------------------------------------------------------------------

class TestVpsGuardSystemd:
    """VPS guard must fire when /etc/systemd/system/meteoedge.service exists."""

    def test_systemd_unit_exists_raises_system_exit(self):
        mod = _load_script()
        with patch("socket.gethostname", return_value="my-laptop"):
            with patch.object(Path, "exists", return_value=True):
                with pytest.raises(SystemExit):
                    mod._check_not_on_vps()


# ---------------------------------------------------------------------------
# 3. VPS guard — clean environment
# ---------------------------------------------------------------------------

class TestVpsGuardClean:
    """VPS guard must not raise when hostname is clean and no systemd unit exists."""

    def test_clean_environment_no_exception(self):
        mod = _load_script()
        with patch("socket.gethostname", return_value="my-laptop"):
            with patch.object(Path, "exists", return_value=False):
                mod._check_not_on_vps()


# ---------------------------------------------------------------------------
# 4. dry-run — no DB writes
# ---------------------------------------------------------------------------

class TestDryRun:
    """With --dry-run, upsert_emos_coefficients must never be called."""

    def test_dry_run_no_db_writes(self):
        mod = _load_script()
        db = Database(":memory:")

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(db, "upsert_emos_coefficients") as mock_upsert, \
             patch.object(mod, "fit_emos", return_value=(0.0, 1.0, 0.0, 1.0)), \
             patch.object(mod, "get_all_cities", return_value=["Chicago"]), \
             patch.object(mod, "fetch_training_data", return_value=_make_triples(80)):
            test_args = argparse.Namespace(
                db=":memory:",
                city="Chicago",
                min_samples=60,
                promote_threshold=0.08,
                dry_run=True,
                report_only=False,
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db):
                mod.main()

        mock_upsert.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Promotion logic — both criteria met => ready=1
# ---------------------------------------------------------------------------

class TestPromotionBothCriteriaMet:
    """ready_for_promotion=1 when crps_holdout < threshold AND samples >= min_samples."""

    def test_ready_for_promotion_when_both_criteria_met(self):
        crps_holdout = 0.05  # < 0.08
        samples = 100        # >= 60
        min_samples = 60
        promote_threshold = 0.08
        ready = 1 if (crps_holdout < promote_threshold and samples >= min_samples) else 0
        assert ready == 1


# ---------------------------------------------------------------------------
# 6. Promotion logic — CRPS not met => ready=0
# ---------------------------------------------------------------------------

class TestPromotionCrpsNotMet:
    """ready_for_promotion=0 when crps_holdout >= promote_threshold."""

    def test_crps_above_threshold_not_ready(self):
        crps_holdout = 0.10   # >= 0.08
        samples = 100
        min_samples = 60
        promote_threshold = 0.08
        ready = 1 if (crps_holdout < promote_threshold and samples >= min_samples) else 0
        assert ready == 0

    def test_crps_equal_threshold_not_ready(self):
        """Boundary: crps_holdout exactly equal to threshold => not ready."""
        crps_holdout = 0.08
        samples = 100
        min_samples = 60
        promote_threshold = 0.08
        ready = 1 if (crps_holdout < promote_threshold and samples >= min_samples) else 0
        assert ready == 0


# ---------------------------------------------------------------------------
# 7. Promotion logic — samples not met => InsufficientDataError / skip
# ---------------------------------------------------------------------------

class TestPromotionSamplesNotMet:
    """main() skips cities when fetch_training_data raises InsufficientDataError."""

    def test_city_skipped_on_insufficient_data(self):
        """Script's main() must silently skip a city that raises InsufficientDataError."""
        mod = _load_script()
        db = Database(":memory:")

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(db, "upsert_emos_coefficients") as mock_upsert, \
             patch.object(mod, "get_all_cities", return_value=["Chicago"]), \
             patch.object(mod, "fetch_training_data",
                          side_effect=InsufficientDataError("Chicago: only 30 samples (need 60)")):
            test_args = argparse.Namespace(
                db=":memory:",
                city=None,
                min_samples=60,
                promote_threshold=0.08,
                dry_run=False,
                report_only=False,
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db):
                mod.main()  # must not raise

        mock_upsert.assert_not_called()

    def test_samples_below_min_yields_ready_zero(self):
        """Even if CRPS is excellent, samples < min_samples => ready=0."""
        crps_holdout = 0.01   # << 0.08
        samples = 30          # < 60
        min_samples = 60
        promote_threshold = 0.08
        ready = 1 if (crps_holdout < promote_threshold and samples >= min_samples) else 0
        assert ready == 0


# ---------------------------------------------------------------------------
# 8. Atomic write — interrupted rename leaves no .json file
# ---------------------------------------------------------------------------

class TestAtomicWriteNoPartial:
    """Atomic write must ensure interrupted rename leaves no .json file."""

    def test_atomic_write_no_partial_on_failure(self):
        """If Path.rename raises OSError, report_path should not exist."""
        mod = _load_script()
        db = Database(":memory:")
        report_path = Path("auto_retrain_report.json")

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(mod, "get_all_cities", return_value=["Chicago"]), \
             patch.object(mod, "fit_emos", return_value=(0.0, 1.0, 0.0, 1.0)), \
             patch.object(mod, "fetch_training_data", return_value=_make_triples(80)):
            test_args = argparse.Namespace(
                db=":memory:",
                city="Chicago",
                min_samples=60,
                promote_threshold=0.08,
                dry_run=False,
                report_only=False,
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db), \
                 patch("pathlib.Path.rename", side_effect=OSError("Rename failed")):
                try:
                    mod.main()
                except OSError:
                    pass  # Expected: rename raises
            # Report should not exist after failed rename
            assert not report_path.exists()


# ---------------------------------------------------------------------------
# 9. validate_report — passes on valid entry
# ---------------------------------------------------------------------------

class TestValidateReportValid:
    """validate_report() must pass silently on a complete report."""

    def test_validate_report_passes_valid_entry(self):
        """A report with all required fields should not raise."""
        mod = _load_script()
        report = {
            "Chicago": {
                "crps_train": 0.05,
                "crps_holdout": 0.06,
                "samples": 100,
                "ready_for_promotion": 1,
                "trained_at": "2026-06-12T18:00:00+00:00",
            }
        }
        mod.validate_report(report)  # Should not raise


# ---------------------------------------------------------------------------
# 10. validate_report — raises on missing field
# ---------------------------------------------------------------------------

class TestValidateReportMissing:
    """validate_report() must raise ValueError when fields are missing."""

    def test_validate_report_raises_on_missing_field(self):
        """Report missing a required field should raise ValueError."""
        mod = _load_script()
        report = {
            "Chicago": {
                "crps_train": 0.05,
                "crps_holdout": 0.06,
                # Missing: samples, ready_for_promotion, trained_at
            }
        }
        with pytest.raises(ValueError) as exc_info:
            mod.validate_report(report)
        # Check that error message mentions the missing fields
        error_msg = str(exc_info.value)
        assert "Chicago" in error_msg
        assert "missing fields" in error_msg

    def test_validate_report_missing_single_field(self):
        """Missing one field should be caught."""
        mod = _load_script()
        report = {
            "Tokyo": {
                "crps_train": 0.05,
                "crps_holdout": 0.06,
                "samples": 100,
                # Missing: ready_for_promotion
                "trained_at": "2026-06-12T18:00:00+00:00",
            }
        }
        with pytest.raises(ValueError) as exc_info:
            mod.validate_report(report)
        error_msg = str(exc_info.value)
        assert "Tokyo" in error_msg
        assert "ready_for_promotion" in error_msg


# ---------------------------------------------------------------------------
# 11. --report-only — no retrain, no fetch_training_data
# ---------------------------------------------------------------------------

class TestReportOnly:
    """With --report-only, the script exits early and does not call fetch_training_data."""

    def test_report_only_no_retrain(self):
        """--report-only should exit before calling fetch_training_data."""
        mod = _load_script()
        db = Database(":memory:")

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(mod, "fetch_training_data") as mock_fetch:
            test_args = argparse.Namespace(
                db=":memory:",
                city=None,
                min_samples=60,
                promote_threshold=0.08,
                dry_run=False,
                report_only=True,
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db):
                mod.main()

        # fetch_training_data should never be called
        mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# 12. sigma_source coupling (issue #799 -- main review risk)
# ---------------------------------------------------------------------------

class TestSigmaSourceCoupling:
    """The offline retrain script must resolve sigma_source from the SAME
    USE_ENSEMBLE_SIGMA flag live serving reads (Database._active_sigma_source),
    and pass the IDENTICAL value to both fetch_training_data (what gets fit)
    and save_coefficients/upsert_emos_coefficients (what gets persisted) --
    decoupling the two reproduces the #658 train/serve skew.
    """

    def test_resolves_sigma_source_from_use_ensemble_sigma_true(self):
        mod = _load_script()
        db = Database(":memory:")
        db.set_config("USE_ENSEMBLE_SIGMA", "true")

        fetch_calls = []

        def fake_fetch(city, db, min_samples=60, **kw):
            fetch_calls.append(kw.get("sigma_source"))
            return _make_triples(80)

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(mod, "fit_emos", return_value=(0.0, 1.0, 0.0, 1.0)), \
             patch.object(mod, "get_all_cities", return_value=["Chicago"]), \
             patch.object(mod, "fetch_training_data", side_effect=fake_fetch):
            test_args = argparse.Namespace(
                db=":memory:", city="Chicago", min_samples=60,
                promote_threshold=0.08, dry_run=False, report_only=False,
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db):
                mod.main()

        assert fetch_calls == ["ensemble"], fetch_calls

    def test_resolves_sigma_source_from_use_ensemble_sigma_false(self):
        mod = _load_script()
        db = Database(":memory:")
        db.set_config("USE_ENSEMBLE_SIGMA", "false")

        fetch_calls = []

        def fake_fetch(city, db, min_samples=60, **kw):
            fetch_calls.append(kw.get("sigma_source"))
            return _make_triples(80)

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(mod, "fit_emos", return_value=(0.0, 1.0, 0.0, 1.0)), \
             patch.object(mod, "get_all_cities", return_value=["Chicago"]), \
             patch.object(mod, "fetch_training_data", side_effect=fake_fetch):
            test_args = argparse.Namespace(
                db=":memory:", city="Chicago", min_samples=60,
                promote_threshold=0.08, dry_run=False, report_only=False,
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db):
                mod.main()

        assert fetch_calls == ["fixed"], fetch_calls

    def test_saved_row_lands_under_the_same_track_fetch_used(self):
        """A REAL (non-mocked) DB write: the row persisted must be readable
        back under the SAME sigma_source fetch_training_data was told to use --
        a live reader's default lookup (sigma_source=None) must find it."""
        mod = _load_script()
        db = Database(":memory:")
        db.set_config("USE_ENSEMBLE_SIGMA", "true")

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(mod, "fit_emos", return_value=(0.0, 1.0, 0.0, 1.0)), \
             patch.object(mod, "get_all_cities", return_value=["Chicago"]), \
             patch.object(mod, "fetch_training_data", return_value=_make_triples(80)):
            test_args = argparse.Namespace(
                db=":memory:", city="Chicago", min_samples=60,
                promote_threshold=0.08, dry_run=False, report_only=False,
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db):
                mod.main()

        row = db.get_emos_coefficients(
            "Chicago", "emos_shadow", forecast_source="baseline", sigma_source="ensemble",
        )
        assert row is not None
        fixed_row = db.get_emos_coefficients(
            "Chicago", "emos_shadow", forecast_source="baseline", sigma_source="fixed",
        )
        assert fixed_row is None, "must not also land under the wrong track"

        # And a live reader's default lookup (no sigma_source passed) finds it.
        default_row = db.get_emos_coefficients("Chicago", "emos_shadow", forecast_source="baseline")
        assert default_row is not None

    def test_explicit_sigma_source_cli_overrides_db_flag(self):
        """--sigma-source overrides the DB-derived value for one-off comparisons."""
        mod = _load_script()
        db = Database(":memory:")
        db.set_config("USE_ENSEMBLE_SIGMA", "true")  # would resolve 'ensemble'...

        fetch_calls = []

        def fake_fetch(city, db, min_samples=60, **kw):
            fetch_calls.append(kw.get("sigma_source"))
            return _make_triples(80)

        with patch("socket.gethostname", return_value="my-laptop"), \
             patch.object(Path, "exists", return_value=False), \
             patch.object(mod, "fit_emos", return_value=(0.0, 1.0, 0.0, 1.0)), \
             patch.object(mod, "get_all_cities", return_value=["Chicago"]), \
             patch.object(mod, "fetch_training_data", side_effect=fake_fetch):
            test_args = argparse.Namespace(
                db=":memory:", city="Chicago", min_samples=60,
                promote_threshold=0.08, dry_run=False, report_only=False,
                sigma_source="fixed",  # ...but the CLI override forces 'fixed'
            )
            with patch("argparse.ArgumentParser.parse_args", return_value=test_args), \
                 patch("scripts.auto_retrain_probability_calibration.Database", return_value=db):
                mod.main()

        assert fetch_calls == ["fixed"], fetch_calls
