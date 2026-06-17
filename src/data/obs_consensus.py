"""Multi-source observation consensus and outlier rejection for daily high.

Algorithm:
  - Split obs into metar and dense (everything else).
  - Compute metar_max = max(metar temps).
  - Reject dense ticks where temp > metar_max + outlier_sigma_f.
  - Return max of remaining dense temps.
  - Fallback to metar_max if no valid dense ticks remain.
  - Return None when both sets are empty.
  - When no METAR is available, return max(dense temps) without rejection.

Controlled by two env vars:
  CONSENSUS_ENABLED         (default "true")  — "false" bypasses rejection
  CONSENSUS_OUTLIER_SIGMA_F (default "4.0")   — rejection threshold
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_ENABLED = os.environ.get("CONSENSUS_ENABLED", "true").lower() != "false"
_OUTLIER_SIGMA_F = float(os.environ.get("CONSENSUS_OUTLIER_SIGMA_F", "4.0"))


def compute_consensus_high(
    obs_list: list[dict],
    outlier_sigma_f: float = _OUTLIER_SIGMA_F,
) -> "float | None":
    """Return the consensus daily-high temperature from a list of observations.

    Args:
        obs_list:        List of obs dicts each with at least {"source": str, "temp_f": float}.
        outlier_sigma_f: Rejection threshold in °F above metar_max. Uses the
                         CONSENSUS_OUTLIER_SIGMA_F env var as default.

    Returns:
        Consensus high in °F, or None when obs_list is empty.
    """
    if not obs_list:
        return None

    if not _ENABLED:
        return max(float(o["temp_f"]) for o in obs_list)

    metar_temps = [float(o["temp_f"]) for o in obs_list if o.get("source") == "metar"]
    dense_obs = [o for o in obs_list if o.get("source") != "metar"]
    dense_temps = [float(o["temp_f"]) for o in dense_obs]

    if not metar_temps and not dense_temps:
        return None

    if not metar_temps:
        return max(dense_temps)

    metar_max = max(metar_temps)

    if not dense_temps:
        return metar_max

    threshold = metar_max + outlier_sigma_f
    accepted: list[float] = []
    for obs in dense_obs:
        t = float(obs["temp_f"])
        if t > threshold:
            log.warning(
                "[obs_consensus] station=%s rejected tick=%.1f metar_max=%.1f delta=%.1f",
                obs.get("station", "?"),
                t,
                metar_max,
                t - metar_max,
            )
        else:
            accepted.append(t)

    return max(accepted) if accepted else metar_max
