"""Per-city rolling residual bias correction for MeteoEdge.

Computes a rolling signed mean error (mean_signed_error) and MAE (rolling_mae)
from historical ``intraday_corrections.delta_f`` entries to correct for
systematic warm bias at specific stations.

``delta_f = observed - model`` (positive mean → model under-predicts the actual
high).  We add ``mean_signed_error`` to ``corrected_mu_f`` so the forecast
distribution shifts toward where real highs tend to fall.

Main entry points:
    compute_residual_stats(city, db) → ResidualStats | None
    apply_residual_correction(city, mu_f, db) → (float, ResidualStats | None)
"""
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (all overrideable via env vars)
# ---------------------------------------------------------------------------

# Number of trailing calendar days to include in the rolling window.
RESIDUAL_WINDOW_DAYS: int = int(os.getenv("RESIDUAL_WINDOW_DAYS", "30"))

# Minimum number of correction rows required before the bias is applied.
RESIDUAL_MIN_SAMPLES: int = int(os.getenv("RESIDUAL_MIN_SAMPLES", "10"))

# Hard clamp on the applied bias correction (°F). The correction is clamped to
# [-RESIDUAL_MAX_CORRECTION_F, +RESIDUAL_MAX_CORRECTION_F] before being added.
RESIDUAL_MAX_CORRECTION_F: float = float(os.getenv("RESIDUAL_MAX_CORRECTION_F", "5.0"))

# MAE threshold above which live NO entries are suppressed.
MAX_RESIDUAL_MAE_F_FOR_LIVE: float = float(os.getenv("MAX_RESIDUAL_MAE_F_FOR_LIVE", "8.0"))

# Master on/off switch.
RESIDUAL_CORRECTION_ENABLED: bool = (
    os.getenv("RESIDUAL_CORRECTION_ENABLED", "true").lower() == "true"
)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ResidualStats:
    """Rolling residual statistics for a single city."""
    city: str
    mean_signed_error: float    # mean(delta_f) over window — positive = warm bias
    rolling_mae: float          # mean(|delta_f|) over window
    sample_count: int           # number of rows used
    correction_applied: bool    # True when bias was added to corrected_mu_f
    live_suppressed: bool       # True when MAE exceeds MAX_RESIDUAL_MAE_F_FOR_LIVE

    @property
    def clamped_correction(self) -> float:
        """Correction actually applied (clamped to ±RESIDUAL_MAX_CORRECTION_F)."""
        return max(
            -RESIDUAL_MAX_CORRECTION_F,
            min(RESIDUAL_MAX_CORRECTION_F, self.mean_signed_error),
        )


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------

def _query_trailing_deltas(city: str, db, window_days: int) -> list[float]:
    """Return delta_f values for *city* in the trailing *window_days* calendar days.

    Performs a parameterised SQL query directly on the underlying SQLite connection
    so we don't need to add a new DB method just for this window query.
    Uses ``DISTINCT (date, obs_time)`` semantics via the PRIMARY KEY — no dedup
    needed because the table has a ``(city, date, obs_time)`` primary key.

    Returns an empty list when the DB is unavailable or the query fails.
    """
    since_date: str = (date.today() - timedelta(days=window_days)).isoformat()
    try:
        cur = db._conn.execute(
            "SELECT delta_f FROM intraday_corrections "
            "WHERE city=? AND date>=? ORDER BY date ASC, obs_time ASC",
            (city, since_date),
        )
        return [float(row[0]) for row in cur.fetchall()]
    except Exception as exc:
        log.warning("[residual] DB query failed for city=%s: %s", city, exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_residual_stats(
    city: str,
    db,
    *,
    window_days: int = RESIDUAL_WINDOW_DAYS,
    min_samples: int = RESIDUAL_MIN_SAMPLES,
    max_correction_f: float = RESIDUAL_MAX_CORRECTION_F,
    mae_threshold: float = MAX_RESIDUAL_MAE_F_FOR_LIVE,
) -> "ResidualStats | None":
    """Compute rolling residual stats for *city*.

    Returns ``None`` when the feature is disabled (``RESIDUAL_CORRECTION_ENABLED``
    is False) or when there are fewer than *min_samples* observations in the
    trailing window (silently — do not raise, do not apply).

    Args:
        city:            Polymarket city name (e.g. "Busan").
        db:              Database instance with ``_conn`` attribute.
        window_days:     Trailing calendar-day window (default: RESIDUAL_WINDOW_DAYS).
        min_samples:     Minimum row count required before applying (default: RESIDUAL_MIN_SAMPLES).
        max_correction_f: Clamp limit (default: RESIDUAL_MAX_CORRECTION_F).
        mae_threshold:   MAE above which live NO entries are suppressed (default: MAX_RESIDUAL_MAE_F_FOR_LIVE).

    Returns:
        ResidualStats or None.
    """
    if not RESIDUAL_CORRECTION_ENABLED:
        return None

    deltas = _query_trailing_deltas(city, db, window_days)

    if len(deltas) < min_samples:
        log.debug(
            "[residual] %s: only %d samples (need %d) — skipping correction",
            city, len(deltas), min_samples,
        )
        return None

    n = len(deltas)
    mean_signed = sum(deltas) / n
    rolling_mae = sum(abs(d) for d in deltas) / n

    clamped = max(-max_correction_f, min(max_correction_f, mean_signed))
    correction_applied = clamped != 0.0
    live_suppressed = rolling_mae > mae_threshold

    return ResidualStats(
        city=city,
        mean_signed_error=mean_signed,
        rolling_mae=rolling_mae,
        sample_count=n,
        correction_applied=correction_applied,
        live_suppressed=live_suppressed,
    )


def apply_residual_correction(
    city: str,
    mu_f: float,
    db,
    *,
    window_days: int = RESIDUAL_WINDOW_DAYS,
    min_samples: int = RESIDUAL_MIN_SAMPLES,
    max_correction_f: float = RESIDUAL_MAX_CORRECTION_F,
    mae_threshold: float = MAX_RESIDUAL_MAE_F_FOR_LIVE,
) -> "tuple[float, ResidualStats | None]":
    """Apply residual bias correction to *mu_f* for *city*.

    Returns ``(corrected_mu_f, stats)`` where ``stats`` is None when correction
    was skipped (feature disabled or insufficient data), and a ResidualStats when
    the correction was computed (even if it happened to be zero after clamping).

    Logs the before/after when a non-zero correction is applied.
    """
    stats = compute_residual_stats(
        city, db,
        window_days=window_days,
        min_samples=min_samples,
        max_correction_f=max_correction_f,
        mae_threshold=mae_threshold,
    )

    if stats is None:
        return mu_f, None

    correction = stats.clamped_correction
    if correction == 0.0:
        return mu_f, stats

    corrected = mu_f + correction
    log.info(
        "[residual] %s: applying bias correction %+.2f°F "
        "(mean_err=%+.2f, clamped=%+.2f, n=%d, MAE=%.2f) "
        "mu_f %.1f → %.1f",
        city, correction,
        stats.mean_signed_error, correction,
        stats.sample_count, stats.rolling_mae,
        mu_f, corrected,
    )
    return corrected, stats
