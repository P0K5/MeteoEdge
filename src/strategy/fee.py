"""Polymarket taker fee model.

Polymarket's taker fee schedule applies to the taker side of each fill,
while makers pay no fee. The formula is:
    fee_cents = 100 * feeRate * p * (1 - p)
where p = price_cents / 100 and feeRate is the rate for the market type.

Weather markets use feeRate=0.05 (0.05%).
Crypto markets use feeRate=0.07 (0.07%).

Published schedule: https://docs.polymarket.com/trading/fees
(Checked 2026-08-28: Weather markets confirmed at 0.05% taker fee, 0% maker fee)
"""

# Weather market taker fee rate (0.05%)
WEATHER_FEE_RATE = 0.05


def estimate_fee_cents(price_cents: int, maker: bool = False) -> float:
    """Polymarket fee in cents per contract.

    Implements the published fee schedule: 100 * feeRate * p * (1 - p),
    where p = price_cents / 100.

    Published schedule: https://docs.polymarket.com/trading/fees
    (Checked 2026-08-28: Weather markets use 0.05% taker, 0% maker)

    Args:
        price_cents: Contract price in cents (1–99)
        maker: If True, return 0.0 (maker pays no fee); if False (default),
               return taker fee.
    Returns:
        Estimated fee in cents (0.0 for makers, taker fee for takers)
    """
    if maker:
        return 0.0

    p = price_cents / 100.0
    return 100.0 * WEATHER_FEE_RATE * p * (1 - p)
