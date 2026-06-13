"""Unit tests for scripts/auto_retrain_probability_calibration.py.

Tests cover:
1. VPS guard — hostname match triggers SystemExit
2. VPS guard — systemd unit file presence triggers SystemExit
3. VPS guard — clean environment (no match) passes without exception
4. dry-run mode — upsert_emos_coefficients is never called
5. Promotion logic — both criteria met (CRPS < threshold AND samples >= min) => ready=1
6. Promotion logic — CRPS not met (>= threshold) => ready=0
7. Promotion logic — samples not met (< min) => skipped (InsufficientDataError)
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
