"""Copy-trading realized P&L math (epic #1102 story C1).

Pure, DB-free module — deliberately separate from ``src/data/db.py`` so
story C2 (the settlement script) and story C3 (backtest-comparison) can both
import it without pulling in ``Database``.

This module only computes P&L from an *already-resolved* outcome. Deriving
that resolution truth is out of scope here — see
``src.data.polymarket.fetch_market_resolution`` (consumed by story C2, not
this module) for the True=YES / False=NO contract this feeds from.
"""
from __future__ import annotations


def copy_position_won(outcome_index: int, yes_won: bool) -> bool:
    """Return True if a position on *outcome_index* won, given *yes_won*.

    ``outcome_index`` is the positional slot aligned with the Gamma API's
    ``outcomePrices`` array (index 0 = YES), matching
    ``fetch_market_resolution``'s True=YES/False=NO contract:

    - ``outcome_index == 0`` (YES) wins when ``yes_won`` is True.
    - ``outcome_index == 1`` (NO) wins when ``yes_won`` is False.
    """
    if outcome_index == 0:
        return yes_won
    return not yes_won


def compute_realized_pnl_usd(
    *,
    entry_price: float,
    stake_usd: float,
    outcome_index: int,
    yes_won: bool,
) -> float:
    """Compute a copy-trading position's realized P&L in USD.

    Prediction-market math: buying ``stake_usd`` of a side priced at
    ``entry_price`` (a 0-1 probability) buys ``stake_usd / entry_price``
    shares. A winning share pays out $1; a losing share pays out $0. So:

    - Win:  ``pnl = shares - stake_usd = stake_usd * (1 - entry_price) / entry_price``
    - Loss: ``pnl = -stake_usd`` (the whole stake is lost; no division by
      ``entry_price`` needed, so a losing position is well-defined even at
      the ``entry_price == 0`` boundary the DB's CHECK constraint allows).

    Args:
        entry_price: This position's own fill price, 0-1 probability.
        stake_usd: Amount staked on this position, in USD.
        outcome_index: 0 (YES) or 1 (NO) — the side this position is on.
        yes_won: The market's resolved outcome (True = YES won), from
            ``fetch_market_resolution``. Not re-derived here.

    Returns:
        Realized P&L in USD — positive on a win, ``-stake_usd`` on a loss.

    Raises:
        ValueError: On a *winning* position with ``entry_price <= 0``. A
            real fill can never happen at price 0 (there is no such thing
            as a free winning share), so this is a data-integrity error,
            not a value this function can silently divide by — unlike the
            loss branch above, which needs no division and stays valid at
            that boundary.
    """
    if copy_position_won(outcome_index, yes_won):
        if entry_price <= 0:
            raise ValueError(
                f"cannot compute P&L for a winning position with entry_price={entry_price!r} "
                "(division by zero/negative price) -- a real fill can't happen at price <= 0"
            )
        return stake_usd * (1 - entry_price) / entry_price
    return -stake_usd
