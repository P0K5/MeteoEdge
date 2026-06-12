"""Polymarket taker fee approximation.

Polymarket's taker fee schedule applies to the taker side of each fill.
The formula max(1.0, 7.0*p*(1-p)) approximates the fee in cents per contract
as a function of price p (where p = price_cents / 100).

The 0.07 coefficient and quadratic form reflect that fees are roughly
proportional to the binary option's variance (p*(1-p)), peaking near 50¢
and declining toward the extremes — consistent with a percentage-of-notional
schedule on a binary contract where notional scales with p*(1-p).

Validation: Run scripts/fee_calibration.py against live fill history to
verify MAE ≤ 0.25¢ and update this docstring with sample size and date.
The script will recommend a recalibrated coefficient if MAE exceeds the
threshold.
"""


def estimate_fee_cents(price_cents: int) -> float:
    """Rough Polymarket taker fee in cents per contract.

    Approximation: max(1.0, 7.0 * p * (1 - p)), where p = price_cents / 100.

    Validation target: MAE ≤ 0.25¢ over the traded price range.
    Run scripts/fee_calibration.py to validate against actual fill history.

    Args:
        price_cents: Contract price in cents (1–99)
    Returns:
        Estimated fee in cents
    """
    p = price_cents / 100.0
    return max(1.0, 7.0 * p * (1 - p))
