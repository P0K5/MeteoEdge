"""Copy-trading backtest-vs-realized P&L comparison (epic #1102 story C3).

The architecture doc's phase-7 go/no-go gate ("compare realized paper P&L
to the backtest's flat-stake numbers", ``docs/design/copy-trading-architecture.md``
"Suggested phasing" step 7) needs this comparison to exist as a queryable
figure. This module is that figure -- for a followed wallet, it compares:

- **Realized** paper P&L: story C1's ``Database.get_copy_realized_pnl_by_wallet``
  (issue #1131), summed over that wallet's settled ``copy_positions`` rows.
- **Projected** P&L: the wallet's most recent screening run's
  ``copy_wallet_candidates.flat_dollar_pnl`` (from the screening pipeline,
  epic #1099), read via ``Database.get_latest_wallet_screenings``. Per the
  architecture doc's phasing section, this is the exact field to compare
  against -- not ``mirrored_dollar_pnl`` or ``mean_roi``/``median_roi``.

This module is DB-aware (unlike ``src/data/copy_pnl.py``, which is
deliberately DB-free) -- it takes a ``Database`` instance and calls the two
read methods above rather than re-querying ``copy_positions`` /
``copy_wallet_candidates`` directly.

Divergence convention (documented once here, applied consistently):

- ``divergence_usd = realized_pnl_usd - projected_flat_dollar_pnl`` --
  positive means the realized figure beat the backtest's projection.
- ``divergence_pct = divergence_usd / abs(projected_flat_dollar_pnl) * 100``
  -- divided by the projection's *magnitude* (not its signed value) so the
  sign of ``divergence_pct`` always matches ``divergence_usd``, even when
  the backtest itself projected a loss.

Both divergence figures are ``None`` when there is no projected figure to
compare against (no screening row for the wallet), and ``divergence_pct``
alone is ``None`` when the projected figure is exactly ``0`` (nothing to
express a percentage of), even though ``divergence_usd`` is still
well-defined in that case.

"No data yet" convention for the two edge cases the acceptance criteria
call out:

- A wallet with a screening row but zero settled positions: realized P&L
  reads as ``0.0`` (``n_settled=0``), not ``None`` and not an exception --
  mirroring ``Database.get_copy_realized_pnl_total``'s existing "zero, not
  None" convention for "no settled positions yet".
- A wallet with settled positions but no ``copy_wallet_candidates`` row
  (should not happen given epic ordering -- a followed wallet was
  necessarily screened first -- but this is a defensive edge case, not an
  expected path): the projected figure and both divergence figures are
  ``None``, not ``0`` and not an exception.
"""
from __future__ import annotations

from src.data.db import Database


def _combine_wallet_pnl(
    address: str,
    realized_row: "dict | None",
    screening_row: "dict | None",
) -> dict:
    """Pure combination of one wallet's realized/projected rows into the
    comparison dict. Shared by the per-wallet and aggregate functions below
    so both apply the exact same "no data yet" and divergence conventions.
    """
    if realized_row is not None:
        n_settled = realized_row["n_settled"]
        realized_pnl_usd = realized_row["total_pnl_usd"]
    else:
        n_settled = 0
        realized_pnl_usd = 0.0

    projected_flat_dollar_pnl = (
        screening_row.get("flat_dollar_pnl") if screening_row is not None else None
    )

    if projected_flat_dollar_pnl is not None:
        divergence_usd = realized_pnl_usd - projected_flat_dollar_pnl
        if projected_flat_dollar_pnl != 0:
            divergence_pct = (
                divergence_usd / abs(projected_flat_dollar_pnl) * 100
            )
        else:
            divergence_pct = None
    else:
        divergence_usd = None
        divergence_pct = None

    return {
        "address": address,
        "n_settled": n_settled,
        "realized_pnl_usd": realized_pnl_usd,
        "projected_flat_dollar_pnl": projected_flat_dollar_pnl,
        "divergence_usd": divergence_usd,
        "divergence_pct": divergence_pct,
    }


def get_wallet_backtest_comparison(db: Database, address: str) -> dict:
    """Return *address*'s realized-vs-projected P&L comparison.

    ``{'address', 'n_settled', 'realized_pnl_usd', 'projected_flat_dollar_pnl',
    'divergence_usd', 'divergence_pct'}`` -- see the module docstring for the
    "no data yet" and divergence conventions.

    Calls ``db.get_copy_realized_pnl_by_wallet(address)`` (already filtered
    to one wallet) and ``db.get_latest_wallet_screenings()`` (unfiltered,
    per that method's contract -- filtered to *address* here rather than
    adding a new DB query for it).
    """
    realized_rows = db.get_copy_realized_pnl_by_wallet(address)
    realized_row = realized_rows[0] if realized_rows else None

    screening_row = next(
        (row for row in db.get_latest_wallet_screenings() if row["address"] == address),
        None,
    )

    return _combine_wallet_pnl(address, realized_row, screening_row)


def get_backtest_comparison_total(db: Database) -> dict:
    """Aggregate realized-vs-projected P&L across every followed wallet
    that has **both** figures (a latest screening row with a non-``None``
    ``flat_dollar_pnl``, and a realized figure -- ``0.0`` counts as present
    per this module's "no data yet" convention).

    A wallet with settled positions but no screening row at all is excluded
    from this aggregate entirely (there's nothing to sum against) rather
    than folded in with a ``None`` projected figure.

    Returns ``{'n_wallets', 'n_settled', 'realized_pnl_usd',
    'projected_flat_dollar_pnl', 'divergence_usd', 'divergence_pct'}``.
    ``divergence_pct`` is ``None`` when the summed projected figure is
    exactly ``0`` (or when there are no wallets with both figures at all).
    """
    realized_by_address = {
        row["address"]: row for row in db.get_copy_realized_pnl_by_wallet()
    }

    per_wallet = []
    for screening_row in db.get_latest_wallet_screenings():
        address = screening_row["address"]
        comparison = _combine_wallet_pnl(
            address, realized_by_address.get(address), screening_row
        )
        if comparison["projected_flat_dollar_pnl"] is not None:
            per_wallet.append(comparison)

    n_settled = sum(c["n_settled"] for c in per_wallet)
    realized_pnl_usd = sum(c["realized_pnl_usd"] for c in per_wallet)
    projected_flat_dollar_pnl = sum(c["projected_flat_dollar_pnl"] for c in per_wallet)
    divergence_usd = realized_pnl_usd - projected_flat_dollar_pnl
    divergence_pct = (
        divergence_usd / abs(projected_flat_dollar_pnl) * 100
        if projected_flat_dollar_pnl != 0
        else None
    )

    return {
        "n_wallets": len(per_wallet),
        "n_settled": n_settled,
        "realized_pnl_usd": realized_pnl_usd,
        "projected_flat_dollar_pnl": projected_flat_dollar_pnl,
        "divergence_usd": divergence_usd,
        "divergence_pct": divergence_pct,
    }
