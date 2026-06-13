"""EMOS deployment mode helpers.

Controls whether each city uses legacy Gaussian, EMOS shadow, or EMOS primary mode.
"""
import logging
import os

log = logging.getLogger(__name__)


def get_city_mode(city: str, db=None) -> str:
    """Return the deployment mode for a city: 'legacy', 'emos_shadow', or 'emos_primary'.

    Reads from emos_calibration table. Falls back to EMOS_DEFAULT_MODE env var (default 'legacy').
    Returns 'legacy' when db is None.
    """
    if db is None:
        return os.environ.get("EMOS_DEFAULT_MODE", "legacy")
    row = db.get_emos_coefficients(city, "emos_shadow") or db.get_emos_coefficients(city, "emos_primary")
    if row is None:
        return os.environ.get("EMOS_DEFAULT_MODE", "legacy")
    # Check if emos_primary is available and ready
    primary = db.get_emos_coefficients(city, "emos_primary")
    if primary and primary.get("ready_for_promotion") == 1:
        return "emos_primary"
    shadow = db.get_emos_coefficients(city, "emos_shadow")
    if shadow:
        return "emos_shadow"
    return os.environ.get("EMOS_DEFAULT_MODE", "legacy")


def apply_emos(mu_raw: float, sigma_raw: float, city: str, db) -> tuple[float, float]:
    """Apply EMOS linear correction: mu_cal = a + b*mu, sigma_cal = c + d*sigma.

    Falls back to (mu_raw, sigma_raw) if no coefficients found.
    """
    # Try emos_primary first, then emos_shadow
    row = db.get_emos_coefficients(city, "emos_primary") or db.get_emos_coefficients(city, "emos_shadow")
    if row is None:
        return mu_raw, sigma_raw
    a, b, c, d = row["a"], row["b"], row["c"], row["d"]
    mu_cal = a + b * mu_raw
    sigma_cal = c + d * sigma_raw
    if sigma_cal <= 0:
        log.warning("[emos] sigma_cal=%.4f <= 0 for %s — using raw sigma", sigma_cal, city)
        sigma_cal = sigma_raw
    return mu_cal, sigma_cal


def _check_ready_for_promotion(city: str, db) -> bool:
    """Return True only if emos_primary row exists with ready_for_promotion=1."""
    row = db.get_emos_coefficients(city, "emos_primary")
    return row is not None and row.get("ready_for_promotion") == 1
