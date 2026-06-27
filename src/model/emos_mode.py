"""City-level EMOS mode selection — picks coefficients by active forecast source.

The emos_mode table stores the preferred forecast_source per city. When a city
has validated (ready_for_promotion=1) EMOS coefficients for the active stack,
this module routes correction through them. Otherwise it falls back to the
legacy 'nws_open_meteo' coefficients (or raw model output if none exist).
"""
from __future__ import annotations

from src.model.emos_calibration import EmosFit, load_coefficients

LEGACY_SOURCE = "nws_open_meteo"


def get_active_source(db, city: str) -> str:
    """Return the active forecast_source for a city from emos_mode table.

    Falls back to LEGACY_SOURCE if no entry exists.
    """
    sql = "SELECT forecast_source FROM emos_mode WHERE city = ? LIMIT 1"
    with db._lock:
        row = db._conn.execute(sql, (city,)).fetchone()
    return row[0] if row else LEGACY_SOURCE


def set_active_source(db, city: str, forecast_source: str) -> None:
    """Set the active forecast_source for a city.

    Only call this after check_ready_for_promotion() returns True for
    the target source. Upserts the emos_mode row.
    """
    sql = """
        INSERT INTO emos_mode (city, forecast_source)
        VALUES (?, ?)
        ON CONFLICT(city) DO UPDATE SET forecast_source = excluded.forecast_source
    """
    with db._lock:
        db._conn.execute(sql, (city, forecast_source))
        db._conn.commit()


def get_coefficients_for_city(db, city: str, forecast_source: "str | None" = None) -> "EmosFit | None":
    """Load the best available EMOS coefficients for a city.

    Priority:
    1. forecast_source (if explicitly provided and ready_for_promotion=1)
    2. Active source from emos_mode table (if ready_for_promotion=1)
    3. Legacy source (nws_open_meteo) if ready_for_promotion=1
    4. None — caller must handle missing coefficients gracefully

    This ensures that promoting a new forecast stack never silently regresses
    a city that hasn't been retrained yet.
    """
    sources_to_try = []
    if forecast_source:
        sources_to_try.append(forecast_source)
    active = get_active_source(db, city)
    if active not in sources_to_try:
        sources_to_try.append(active)
    if LEGACY_SOURCE not in sources_to_try:
        sources_to_try.append(LEGACY_SOURCE)

    for src in sources_to_try:
        fit = load_coefficients(db, city, src)
        if fit is not None:
            return fit
    return None


def apply_emos(model_mu: float, sigma_naive: float, fit: EmosFit) -> tuple[float, float]:
    """Apply EMOS linear correction to a raw forecast.

    Returns (corrected_mu, corrected_sigma). sigma is floored at 0.1°F.
    """
    mu = fit.a + fit.b * model_mu
    sigma = max(0.1, fit.c + fit.d * sigma_naive)
    return mu, sigma
