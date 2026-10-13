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


def effective_stake_usd(filled_stake_usd: "float | None", stake_usd: float) -> float:
    """Return the USD actually spent on a real fill (issue #1336).

    Mirrors ``COALESCE(filled_stake_usd, stake_usd)`` (the schema contract in
    ``src/data/db.py``): the recorded fill cost wins whenever one exists --
    full AND partial fills both record it now -- and the row's intended
    ``stake_usd`` is used only for rows written before the column was
    populated (legacy full fills, or any row whose fill record was never
    retrieved). Used by copy_live_settle for both realized P&L and the
    committed-exposure side of the wallet-balance drift check, so the two
    can never disagree about what a position cost.
    """
    return float(filled_stake_usd) if filled_stake_usd is not None else float(stake_usd)


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


def compute_pnl_at_observed_price_usd(
    *, entry_price: float, stake_usd: float, outcome_index: int, yes_price: float,
) -> float:
    """P&L if the position's shares pay out at an operator-observed final YES
    price (issue #1301), e.g. 0.5 for a 50/50 void. Same share math as
    ``compute_realized_pnl_usd`` with payout-per-share = ``yes_price`` (YES
    side) or ``1 - yes_price`` (NO side): ``stake * (payout / entry - 1)``.
    At yes_price 1.0/0.0 it equals ``compute_realized_pnl_usd``. Only reachable
    from the operator CLI."""
    if not 0.0 <= yes_price <= 1.0:
        raise ValueError("yes_price must be within [0, 1]")
    payout = yes_price if outcome_index == 0 else 1.0 - yes_price
    if entry_price <= 0:
        raise ValueError(f"entry_price={entry_price!r} <= 0")
    return stake_usd * (payout / entry_price - 1.0)


def is_guaranteed_loss_pair(entry_price_a: float, entry_price_b: float) -> bool:
    """Return True if two opposite-outcome legs of the same binary market,
    held simultaneously, can only ever pay out less than their combined
    cost (issue #1316).

    A binary market's two outcome tokens pay out exactly $1.00 combined,
    always (one pays $1, the other $0). Paying >= $1.00 total to hold both
    legs is therefore a mathematically guaranteed loss, fixed at entry,
    regardless of which side eventually resolves. The boundary (exactly
    $1.00) counts as guaranteed loss: "guaranteed loss" means "cannot
    possibly profit", and a combined cost of exactly $1.00 nets exactly
    $0.00 — never a gain. This is ``copy_signal_loop.py``'s
    ``opposite_side_above_parity`` skip-reason condition, lifted out here
    so the live guard and any offline analysis apply the identical rule.
    """
    return (entry_price_a + entry_price_b) >= 1.0


def opposite_side_pair_locked_loss_usd(
    entry_price_a: float, stake_usd_a: float, entry_price_b: float, stake_usd_b: float,
) -> float:
    """Return the dollar loss locked in at entry for an above-parity
    opposite-side pair (issue #1316), or ``0.0`` if the pair is not
    actually above parity.

    Approximates both legs as sized at the AVERAGE of the two stakes --
    exact when both legs use the same flat per-trade stake (the common
    case, both paper and live today), an approximation otherwise (e.g. a
    wallet whose ``stake_per_trade`` changed between the two fills).
    Generalizes the simple ``(price_0 + price_1 - 1.00) x stake``
    arithmetic issue #1316 used for its own measurement (which assumed
    equal $5/leg stakes) to unequal stakes, without claiming more
    precision than the two different stake amounts actually support.
    """
    if not is_guaranteed_loss_pair(entry_price_a, entry_price_b):
        return 0.0
    avg_stake_usd = (stake_usd_a + stake_usd_b) / 2.0
    return (entry_price_a + entry_price_b - 1.0) * avg_stake_usd


def summarize_opposite_side_overlap(pairs: "list[dict]") -> "dict[str, dict]":
    """Aggregate raw overlap pairs (``Database.get_opposite_side_overlap_pairs``)
    into a per-wallet disclosure summary (issue #1316).

    Returns ``{address: {"n_above_parity_pairs": int,
    "n_above_parity_pairs_still_open": int, "locked_loss_usd": float,
    "locked_loss_usd_still_open": float}}`` for every wallet with at least
    one above-parity pair — a wallet with none is simply absent, mirroring
    ``Database.get_copy_realized_pnl_by_wallet``'s "absent, not zero"
    convention. Pairs at or below parity (a genuine sub-parity arbitrage,
    option (b)'s permitted case) contribute nothing and are silently
    skipped, not counted as zero-loss entries.

    ``*_still_open`` counts/sums only pairs where at least one of the two
    legs has not yet settled -- this is what a per-wallet performance
    surface needs to keep disclosing per the issue's "until the historical
    positions have settled out" requirement. Once both legs of a pair are
    settled, that pair's loss is already fully and correctly reflected in
    the wallet's own realized P&L, so there is nothing further to flag for
    it; the non-``_still_open`` totals are kept too for one-off historical
    reporting (e.g. a PR's own drag quantification), where the fully
    historical picture — including already-settled pairs — is the point.
    """
    summary: "dict[str, dict]" = {}
    for pair in pairs:
        loss_usd = opposite_side_pair_locked_loss_usd(
            pair["a_entry_price"], pair["a_stake_usd"],
            pair["b_entry_price"], pair["b_stake_usd"],
        )
        if loss_usd <= 0.0:
            continue
        row = summary.setdefault(pair["address"], {
            "n_above_parity_pairs": 0,
            "n_above_parity_pairs_still_open": 0,
            "locked_loss_usd": 0.0,
            "locked_loss_usd_still_open": 0.0,
        })
        row["n_above_parity_pairs"] += 1
        row["locked_loss_usd"] += loss_usd
        if pair["a_status"] == "open" or pair["b_status"] == "open":
            row["n_above_parity_pairs_still_open"] += 1
            row["locked_loss_usd_still_open"] += loss_usd
    return summary
