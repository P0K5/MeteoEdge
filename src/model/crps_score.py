"""CRPS (Continuous Ranked Probability Score) for Gaussian predictive distributions.

Stateless utility. No DB, no I/O, no side effects.
Reference: Gneiting & Raftery (2007) doi:10.1198/016214506000001437
"""
import math
from typing import Optional


def crps_gaussian(mu: float, sigma: float, y: float) -> float:
    """CRPS for a single Gaussian forecast (mu, sigma) vs observation y.

    Handles sigma=0 (deterministic) by returning |mu - y|.

    Args:
        mu: Mean of the Gaussian forecast
        sigma: Standard deviation of the Gaussian forecast
        y: Observed value

    Returns:
        CRPS value (always >= 0)
    """
    if sigma <= 0:
        return abs(mu - y)
    z = (y - mu) / sigma
    # Standard normal PDF and CDF via math.erf
    phi_z = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)   # φ(z)
    Phi_z = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))           # Φ(z)
    return sigma * (z * (2 * Phi_z - 1) + 2 * phi_z - 1.0 / math.sqrt(math.pi))


def mean_crps(forecasts: list[tuple[float, float, float]]) -> Optional[float]:
    """Mean CRPS over a list of (mu, sigma, y) triples.

    Args:
        forecasts: List of tuples (mu, sigma, y) where:
            mu: Mean of Gaussian forecast
            sigma: Standard deviation of Gaussian forecast
            y: Observed value

    Returns:
        Mean CRPS if forecasts is non-empty, None otherwise
    """
    if not forecasts:
        return None
    return sum(crps_gaussian(mu, sigma, y) for mu, sigma, y in forecasts) / len(forecasts)
