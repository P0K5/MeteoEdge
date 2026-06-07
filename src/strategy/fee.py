"""Polymarket taker fee approximation.

Polymarket's taker fee schedule is ~2% of order value. This approximation
(0.07 * p * (1-p)) is calibrated to the fee at common price levels.
"""


def estimate_fee_cents(price_cents: int) -> float:
    """Rough Polymarket taker fee in cents per contract.

    Approximation: 0.07 * p * (1 - p), floored at 1¢.

    Args:
        price_cents: Contract price in cents (1–99)
    Returns:
        Estimated fee in cents
    """
    p = price_cents / 100.0
    return max(1.0, 7.0 * p * (1 - p))
