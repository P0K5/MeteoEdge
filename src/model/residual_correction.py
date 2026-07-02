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
    compute_residual_stats_per_pair(city, db) → list[ResidualStats]
"""
import logging
import os
from dataclasses import dataclass
from datetime import date as _date, timedelta
from typing import Literal

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

# Calendar date (YYYY-MM-DD) the #576 fix for issue #572 deployed: it switched
# the intraday consensus basis from a hardcoded open_meteo-only blend to the
# live DEB-weighted blend, and started tagging each delta with the
# `basis_weights` snapshot used to produce it.
#
# `intraday_corrections` rows dated *before* this deploy date were recorded
# under the legacy hardcoded basis; rows on/after it reflect the live DEB
# basis. We gate the trailing window on this date rather than comparing
# `basis_weights` content directly because the column's DEFAULT value is
# textually identical to the legacy hardcoded basis (`{"open_meteo": 1.0}`) —
# a genuinely-legacy row and a post-deploy row where DEB's live weights
# happen to equal the fallback are indistinguishable by content alone. Date
# is the only reliable regime discriminator (issue #586).
DEB_BASIS_DEPLOY_DATE: str = os.getenv("DEB_BASIS_DEPLOY_DATE", "2026-07-02")


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class ResidualStats:
    """Rolling residual statistics for a single city (or city+station+source pair)."""
    city: str
    mean_signed_error: float    # mean(delta_f) over window — positive = warm bias
    rolling_mae: float          # mean(|delta_f|) over window
    sample_count: int           # number of rows used
    correction_applied: bool    # True when bias was added to corrected_mu_f
    live_suppressed: bool       # True when MAE exceeds MAX_RESIDUAL_MAE_F_FOR_LIVE
    scope: Literal["pair", "city_fallback"] = "city_fallback"
    station: str = ""           # source station identifier (empty = city-wide)
    source: str = ""            # source name (empty = city-wide)
    basis_deploy_date: str = "" # deploy-date cutoff applied to exclude mixed-basis deltas (#586)

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

def _query_trailing_deltas(
    city: str,
    db,
    window_days: int,
    *,
    station: "str | None" = None,
    source: "str | None" = None,
    deploy_date: str = DEB_BASIS_DEPLOY_DATE,
) -> list[float]:
    """Return delta_f values for *city* in the trailing *window_days* calendar days.

    Optional *station* and *source* narrow results to a specific (station, source)
    pair.  When both are None the query is city-wide.

    *deploy_date* excludes any row recorded before the #576 basis-regime
    deploy date, so the rolling mean never blends legacy open_meteo-only-basis
    deltas with live DEB-basis deltas (issue #586). Passed straight through as
    ``min_date`` to ``db.get_trailing_deltas`` — it only raises the window's
    lower bound, it never widens it, so city-wide fallback semantics are
    unchanged.

    Returns an empty list when the DB is unavailable or the query fails.
    """
    try:
        return db.get_trailing_deltas(
            city, window_days, station=station, source=source, min_date=deploy_date
        )
    except Exception as exc:
        log.warning("[residual] DB query failed for city=%s: %s", city, exc)
        return []


def _build_stats(
    city: str,
    deltas: list[float],
    *,
    max_correction_f: float,
    mae_threshold: float,
    scope: Literal["pair", "city_fallback"],
    station: str = "",
    source: str = "",
    deploy_date: str = DEB_BASIS_DEPLOY_DATE,
) -> ResidualStats:
    """Build a ResidualStats from a list of deltas."""
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
        scope=scope,
        station=station,
        source=source,
        basis_deploy_date=deploy_date,
    )


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
    deploy_date: str = DEB_BASIS_DEPLOY_DATE,
) -> "ResidualStats | None":
    """Compute rolling residual stats for *city*.

    Tries to scope the computation to the top-priority (station, source) pair
    for the city.  Falls back to city-wide when the pair has fewer than
    *min_samples* entries.  Sets ``ResidualStats.scope`` accordingly.

    Returns ``None`` when the feature is disabled (``RESIDUAL_CORRECTION_ENABLED``
    is False) or when there are fewer than *min_samples* observations in the
    trailing window (silently — do not raise, do not apply).

    CRITICAL DESIGN DECISION (issue #586): rows recorded before *deploy_date*
    are excluded from the window because they were computed under a different
    consensus basis (see ``DEB_BASIS_DEPLOY_DATE`` docstring above). If that
    exclusion drops the sample below *min_samples*, we deliberately do NOT
    fall back to including the older, mixed-basis rows just to reach the
    threshold — a stale/no-correction outcome is preferred over a correction
    blended across two different bias regimes, since a mixed-basis mean can
    be actively wrong in either direction rather than merely noisy. This is
    the same "insufficient data → skip" path used for any other under-sample
    case; no special-casing is needed beyond querying with the basis filter
    applied up front.

    Args:
        city:            Polymarket city name (e.g. "Busan").
        db:              Database instance.
        window_days:     Trailing calendar-day window (default: RESIDUAL_WINDOW_DAYS).
        min_samples:     Minimum row count required before applying (default: RESIDUAL_MIN_SAMPLES).
        max_correction_f: Clamp limit (default: RESIDUAL_MAX_CORRECTION_F).
        mae_threshold:   MAE above which live NO entries are suppressed (default: MAX_RESIDUAL_MAE_F_FOR_LIVE).
        deploy_date:     Exclude rows recorded before this date — a different
                         consensus basis regime (default: DEB_BASIS_DEPLOY_DATE).

    Returns:
        ResidualStats or None.
    """
    if not RESIDUAL_CORRECTION_ENABLED:
        return None

    # --- Try pair-scoped query first ---
    top_pair = _get_top_priority_pair(city)
    if top_pair is not None:
        pair_station, pair_source = top_pair
        pair_deltas = _query_trailing_deltas(
            city, db, window_days,
            station=pair_station, source=pair_source, deploy_date=deploy_date,
        )
        if len(pair_deltas) >= min_samples:
            return _build_stats(
                city, pair_deltas,
                max_correction_f=max_correction_f,
                mae_threshold=mae_threshold,
                scope="pair",
                station=pair_station,
                source=pair_source,
                deploy_date=deploy_date,
            )
        log.debug(
            "[residual] %s: pair (%s/%s) only %d samples (post-basis-filter, "
            "deploy_date=%s) — falling back to city-wide",
            city, pair_station, pair_source, len(pair_deltas), deploy_date,
        )

    # --- City-wide fallback ---
    deltas = _query_trailing_deltas(city, db, window_days, deploy_date=deploy_date)

    if len(deltas) < min_samples:
        # Under-sample after basis-regime exclusion: prefer no correction
        # over a mixed-basis one (see CRITICAL DESIGN DECISION above).
        log.debug(
            "[residual] %s: only %d samples (need %d, post-basis-filter "
            "deploy_date=%s) — skipping correction rather than mixing basis "
            "regimes",
            city, len(deltas), min_samples, deploy_date,
        )
        return None

    return _build_stats(
        city, deltas,
        deploy_date=deploy_date,
        max_correction_f=max_correction_f,
        mae_threshold=mae_threshold,
        scope="city_fallback",
    )


def compute_residual_stats_per_pair(
    city: str,
    db,
    *,
    window_days: int = RESIDUAL_WINDOW_DAYS,
    min_samples: int = RESIDUAL_MIN_SAMPLES,
    max_correction_f: float = RESIDUAL_MAX_CORRECTION_F,
    mae_threshold: float = MAX_RESIDUAL_MAE_F_FOR_LIVE,
    deploy_date: str = DEB_BASIS_DEPLOY_DATE,
) -> "list[ResidualStats]":
    """Return one ResidualStats per distinct (station, source) pair in the trailing window.

    Only pairs with at least *min_samples* entries are returned.  Empty list when
    the feature is disabled or no qualified pairs exist.

    Args:
        city:            Polymarket city name.
        db:              Database instance.
        window_days:     Trailing calendar-day window (default: RESIDUAL_WINDOW_DAYS).
        min_samples:     Minimum row count per pair (default: RESIDUAL_MIN_SAMPLES).
        max_correction_f: Clamp limit (default: RESIDUAL_MAX_CORRECTION_F).
        mae_threshold:   MAE suppression threshold (default: MAX_RESIDUAL_MAE_F_FOR_LIVE).
        deploy_date:     Exclude rows recorded before this date — a different
                         consensus basis regime (default: DEB_BASIS_DEPLOY_DATE).

    Returns:
        List of ResidualStats, one per qualified (station, source) pair.
    """
    if not RESIDUAL_CORRECTION_ENABLED:
        return []

    since_date = (_date.today() - timedelta(days=window_days)).isoformat()

    # Query distinct pairs that have data in the trailing window
    try:
        rows = db.get_distinct_pairs(city, since_date)
    except Exception as exc:
        log.warning("[residual] DB query failed for city=%s: %s", city, exc)
        return []

    results: list[ResidualStats] = []
    for row in rows:
        pair_station = row[0]
        pair_source = row[1]
        deltas = _query_trailing_deltas(
            city, db, window_days,
            station=pair_station, source=pair_source, deploy_date=deploy_date,
        )
        if len(deltas) < min_samples:
            # Under-sample after basis-regime exclusion: skip this pair
            # rather than mixing basis regimes (see compute_residual_stats).
            continue
        results.append(
            _build_stats(
                city, deltas,
                max_correction_f=max_correction_f,
                mae_threshold=mae_threshold,
                scope="pair",
                station=pair_station,
                source=pair_source,
                deploy_date=deploy_date,
            )
        )
    return results


def apply_residual_correction(
    city: str,
    mu_f: float,
    db,
    *,
    window_days: int = RESIDUAL_WINDOW_DAYS,
    min_samples: int = RESIDUAL_MIN_SAMPLES,
    max_correction_f: float = RESIDUAL_MAX_CORRECTION_F,
    mae_threshold: float = MAX_RESIDUAL_MAE_F_FOR_LIVE,
    deploy_date: str = DEB_BASIS_DEPLOY_DATE,
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
        deploy_date=deploy_date,
    )

    if stats is None:
        return mu_f, None

    correction = max(-max_correction_f, min(max_correction_f, stats.mean_signed_error))
    if correction == 0.0:
        return mu_f, stats

    corrected = mu_f + correction
    log.info(
        "[residual] %s: applying bias correction %+.2f°F "
        "(mean_err=%+.2f, clamped=%+.2f, n=%d, MAE=%.2f, scope=%s, "
        "basis=deb-since:%s) "
        "mu_f %.1f → %.1f",
        city, correction,
        stats.mean_signed_error, correction,
        stats.sample_count, stats.rolling_mae, stats.scope,
        stats.basis_deploy_date,
        mu_f, corrected,
    )
    return corrected, stats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_top_priority_pair(city: str) -> "tuple[str, str] | None":
    """Return (station, source) for the top-priority source for *city*, or None."""
    try:
        from src.config import get_source_priority
        sources = get_source_priority(city)
        if sources:
            top = sources[0]
            return top["station"], top["source"]
    except Exception:
        pass
    return None
