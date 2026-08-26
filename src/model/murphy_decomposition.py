"""Murphy (1973) Brier-score decomposition: BS = Reliability - Resolution + Uncertainty.

Issue #1048: the sigma-lever reconstruction needs to tell apart a genuine
sharpening of resolved predictions (Resolution up) from an accident of
reliability on one sample (Reliability down, Resolution flat) -- a single
BSS number conflates the two. This module is the shared helper the report
script uses instead of hand-rolling the binning inline (per the issue's
explicit instruction).

Uses the SAME bucket edges and bucketing convention (``[lo, hi)`` on the
predicted probability) as ``src.scripts.calibration_report.build_reliability``
-- imported, not re-derived, so this decomposition partitions samples
identically to the gate's own reliability table.

Binned decomposition, not exact per-sample decomposition: within each bucket
every sample's raw forecast probability is replaced by the bucket's own mean
predicted probability. The identity
``BS = Reliability - Resolution + Uncertainty`` is then EXACT for that
binned quantity, but only an approximation of the true (unbinned) Brier
score reported by ``calibration_report.brier_score`` -- the difference is
the within-bucket forecast variance the binning discards. Both figures are
returned so a caller can see how large that gap is, rather than silently
picking one.
"""
from __future__ import annotations

from src.scripts.calibration_report import BUCKET_EDGES, brier_score


def murphy_decomposition(
    samples: "list[tuple[float, bool]]",
    edges: "list[float]" = BUCKET_EDGES,
) -> dict:
    """Return the Murphy (1973) decomposition of the Brier score over *samples*.

    Args:
        samples: list of (predicted_p_yes, yes_won) pairs -- same shape
            ``calibration_report.brier_score``/``build_reliability`` take.
        edges: bucket boundaries; defaults to the gate's own ``BUCKET_EDGES``.

    Returns a dict with:
        n:               sample count (0 when *samples* is empty; every
                          other key is ``None`` in that case).
        base_rate:        overall observed YES frequency (``obar``).
        reliability:      mean squared gap between each bucket's mean
                          forecast and its observed frequency, weighted by
                          bucket size -- 0 for a perfectly calibrated model.
        resolution:       mean squared gap between each bucket's observed
                          frequency and the overall base rate, weighted by
                          bucket size -- how much the forecast's bucketing
                          actually discriminates outcomes.
        uncertainty:      base_rate * (1 - base_rate) -- the irreducible
                          variance of the outcome itself, independent of the
                          forecaster.
        bs_decomposed:    reliability - resolution + uncertainty.
        bs_actual:        the true (unbinned) Brier score over *samples*,
                          via ``calibration_report.brier_score`` -- compare
                          against ``bs_decomposed`` to see the binning gap.
        n_buckets_used:   number of buckets that received at least one
                          sample (informational -- a variant with a smaller
                          population may use fewer buckets).
    """
    n = len(samples)
    if n == 0:
        return {
            "n": 0, "base_rate": None, "reliability": None, "resolution": None,
            "uncertainty": None, "bs_decomposed": None, "bs_actual": None,
            "n_buckets_used": 0,
        }

    base_rate = sum(1.0 for _, w in samples if w) / n

    reliability_sum = 0.0
    resolution_sum = 0.0
    n_buckets_used = 0
    for lo, hi in zip(edges, edges[1:]):
        bucket = [(p, w) for p, w in samples if lo <= p < hi]
        if not bucket:
            continue
        n_buckets_used += 1
        n_k = len(bucket)
        mean_pred = sum(p for p, _ in bucket) / n_k
        obs_freq = sum(1.0 for _, w in bucket if w) / n_k
        reliability_sum += n_k * (mean_pred - obs_freq) ** 2
        resolution_sum += n_k * (obs_freq - base_rate) ** 2

    reliability = reliability_sum / n
    resolution = resolution_sum / n
    uncertainty = base_rate * (1.0 - base_rate)
    bs_decomposed = reliability - resolution + uncertainty

    return {
        "n": n,
        "base_rate": base_rate,
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "bs_decomposed": bs_decomposed,
        "bs_actual": brier_score(samples),
        "n_buckets_used": n_buckets_used,
    }


def format_murphy_decomposition(decomp: dict, title: str) -> str:
    """Render *decomp* (from ``murphy_decomposition``) as a small text block."""
    if decomp["n"] == 0:
        return f"\n=== {title} ===\nn=0 -- no samples."
    lines = [f"\n=== {title} ===", f"n = {decomp['n']} (buckets used: {decomp['n_buckets_used']})"]
    lines.append(f"base_rate    = {decomp['base_rate']:.4f}")
    lines.append(f"reliability  = {decomp['reliability']:.4f}  (lower is better; 0 = perfectly calibrated)")
    lines.append(f"resolution   = {decomp['resolution']:.4f}  (higher is better; 0 = no discrimination)")
    lines.append(f"uncertainty  = {decomp['uncertainty']:.4f}  (forecaster-independent)")
    lines.append(f"BS (decomposed) = {decomp['bs_decomposed']:.4f}")
    lines.append(f"BS (actual)     = {decomp['bs_actual']:.4f}"
                 if decomp["bs_actual"] is not None else "BS (actual)     = n/a")
    return "\n".join(lines)
