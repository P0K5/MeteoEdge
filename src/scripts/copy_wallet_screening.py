"""Scheduled wallet-screening runner (epic #1099, story 2).

Turns `copy_trade_backtest.py`'s screening logic into a script that
**persists every run** to `copy_wallet_candidates` (story 1, issue #1108)
and implements the stability check the architecture doc requires: a wallet
is only `eligible_to_follow` when its latest run agrees with the
immediately-previous one.

This is what would have caught the spike's own instability finding before
it reached a followed-wallet decision: wallet `0xd3b034d7...` looked like
the best candidate in one run (`n_resolved=7498`, `median_roi=+33.4%`) and
reversed completely 15 hours later (`n_resolved=2271`, `median_roi=-100%`),
because `get_wallet_trades()` (`src/data/polymarket_traders.py`) can be cut
short of a wallet's true history -- by the server's own undocumented
offset ceiling (observed at 10,500, issue #1233) well before this
function's own defensive `MAX_TRADE_PAGES * page_size` = 20,000 depth cap
is ever reached -- and returns a recency-biased window in that case (see
`docs/design/copy-trading-architecture.md`, "Known limitation").

**No live/paper trading of any kind.** Output is rows in
`copy_wallet_candidates` only -- this script never places an order.

**Which stats get persisted/compared.** The architecture doc's
`copy_wallet_candidates` schema has one `win_rate`/`mean_roi`/`median_roi`
column each (not separate trader/copier columns) -- the copier's own
post-slippage numbers are persisted, since eligibility is about whether
*following* this wallet stays profitable, not whether the original trader
was profitable.

**`--min-trades` vs. the `check_quality()` sample-size condition (issue
#1248).** Two different things named similarly -- don't conflate them.
`--min-trades` (CLI arg, default 0, unset in production) is a `run()`-level
skip: a wallet below it is dropped from `addresses` iteration BEFORE
anything is persisted, so there is no `copy_wallet_candidates` row and no
audit trail for why it was skipped. `check_quality()`'s
`insufficient_resolved_trades` condition, by contrast, is a *judgement*:
the row is still written with `eligible_to_follow=0` and the reason is
logged, exactly like every other quality-gate rejection. `--min-trades`
keeps its current meaning and default here -- it is not replaced by the
quality gate.

Usage::

    python -m src.scripts.copy_wallet_screening --window month --top 20
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Try to import fcntl for POSIX file locking (Mac/Linux deployment).
# On Windows dev machines, fall back to a no-op (issue #1247).
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import CONFIG_DEFAULTS, get_live_config  # noqa: E402
from src.data.db import Database  # noqa: E402
from src.data.polymarket_traders import get_leaderboard, wallet_address  # noqa: E402
from src.scripts.copy_trade_backtest import (  # noqa: E402
    DEFAULT_SLIPPAGE_BPS,
    backtest_wallet,
)

log = logging.getLogger(__name__)

#: Module-level lock object held for the process lifetime (issue #1247).
#: Assigned by main() after acquisition to prevent garbage collection.
_LOCK = None

#: Rate-limit-budget decision (epic #1099 story 2/3): running this screening
#: job as its own process means its calls to gamma-api.polymarket.com are
#: NOT jointly throttled with the live weather bot's own traffic on that
#: host (src.http_client.DomainRateLimiter is a process-local singleton).
#: Bounding worst-case call volume per run via a hard cap is the v1
#: mitigation -- a cross-process shared limiter is explicitly out of scope.
MAX_WALLETS_PER_RUN = 50

#: Quality gate thresholds (issue #1209). `check_stability()` only proves a
#: screening run was *reproducible* -- these thresholds separately gate on
#: whether the wallet is actually worth following. Derived from the
#: 2026-09-25 screening audit (see issue #1209 for the measured evidence).

#: Fallback minimum resolved trades required before check_quality() will
#: trust a wallet's profitability/tail/truncation metrics at all (issue
#: #1248), used only when no DB is available to read the live config value
#: (see `_min_resolved_trades_threshold()`). Mirrors
#: `CONFIG_DEFAULTS["COPY_SCREEN_MIN_RESOLVED_TRADES"]` -- keep these two in
#: sync; the live-editable config value is what actually governs behaviour
#: in the normal, DB-available path. Without this gate, a wallet with e.g.
#: two resolved trades can pass every other condition below by chance --
#: see 0x365f951dc2 (n_resolved=2, median_roi=+2.79), the sole
#: recommendation in the 2026-09-29 advisory report.
QUALITY_MIN_RESOLVED_TRADES = CONFIG_DEFAULTS["COPY_SCREEN_MIN_RESOLVED_TRADES"]

#: A wallet must be profitable under the flat-stake model we actually trade
#: (not merely under its own, possibly much larger, position sizing).
QUALITY_MIN_FLAT_DOLLAR_PNL = 0.0

#: The median trade itself must be profitable -- a positive mean built on a
#: negative/zero median means most trades lose money.
QUALITY_MIN_MEDIAN_ROI = 0.0

#: Reject tail-driven P&L: wallets whose mean ROI is propped up by a handful
#: of outsized winners do not survive flat-stake copying (see
#: docs/design/copy-trading-architecture.md, "Background"). Only applied
#: when median_roi > 0 -- see check_quality()'s docstring for why ordering
#: relative to the median_roi check below is not load-bearing.
QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO = 3.0

#: Gate wallets whose trade history was truncated -- their metrics are not
#: run-to-run stable on their own (see this module's docstring and issue
#: #1209). Rather than an unconditional reject, check_quality()'s condition
#: (d) admits a truncated wallet when its flat-stake edge reproduced in the
#: immediately-previous screening run too (issue #1217) -- see that
#: function's docstring.
#:
#: **Issue #1233: no `QUALITY_MAX_TOTAL_TRADES`-style constant here
#: anymore, deliberately.** This gate used to infer truncation downstream
#: by comparing `n_buy_trades + n_sell_excluded` against a constant meant
#: to describe the fetcher's own cap -- and went silently inert TWICE for
#: the same structural reason: the constant described something other
#: than reality (first the wrong units -- buys vs. buys+sells, issue
#: #1209/#1211; then our own page cap rather than the server's actual,
#: lower, undocumented ceiling, issue #1233). `get_wallet_trades()`
#: (`src/data/polymarket_traders.py`) is the only code that observes *why*
#: pagination stopped, so condition (d) below consumes its `truncated`
#: flag (threaded through by `backtest_wallet()`) directly, instead of
#: re-deriving truncation from a count a third time.


def lock_acquired(lock_file: Path):
    """Acquire an exclusive single-instance lock, crash-safe.

    Returns:
        - On POSIX (fcntl available): open file object if the lock was acquired,
          None if another instance already holds it. Caller MUST retain the
          file object at module scope for the process lifetime; on process exit
          or crash, fcntl.flock releases it automatically.
        - On Windows (fcntl unavailable): returns True (no-op fallback, allows
          the script to run). This is deliberate: the script is only deployed
          on the Mac mini (see .claude/instructions/governance.md).

    Args:
        lock_file: path to the lockfile (e.g. data/.copy_wallet_screening.lock)

    Raises:
        OSError if the lockfile cannot be opened (e.g. data/ doesn't exist)
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_file, "w")
    if fcntl is None:
        # Windows dev machine: no-op fallback, allow it to run.
        return True
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(f"{os.getpid()}\n")
        f.flush()
        return f
    except OSError:
        # Another instance holds the lock.
        f.close()
        return None


class PersistentResolutionCache(dict):
    """A run-scoped resolution cache shared across every wallet in one
    screening run (tier 1, issue #1221), optionally backed by the
    persistent ``market_resolutions`` DB table (tier 2) so a market
    resolved on a PRIOR run costs zero network calls today.

    Deliberately a plain ``dict`` subclass, not a new interface: it is
    passed straight through as ``backtest_wallet()``'s/``resolve_payout()``'s
    (``src/scripts/copy_trade_backtest.py``) ``cache`` argument unmodified
    -- that module stays entirely ignorant of the DB (it must keep working
    with no ``Database`` at all -- see its module docstring), and this
    class only overrides ``__contains__`` (consult the DB on an in-memory
    miss) and ``__setitem__`` (persist a genuinely resolved market on a
    network miss).

    **Poisoning invariant -- the single most important correctness
    property here.** Only a resolved market (``True``/``False``) is ever
    persisted to the DB. ``fetch_market_resolution`` returning ``None``
    (unresolved) is still cached in the in-memory dict for the rest of
    THIS run (matching the old per-wallet cache's own behaviour -- avoids
    a second network call for the same still-unresolved market a few
    seconds later in the same run), but is never written to the DB -- a
    negative DB entry would permanently misclassify a market that goes on
    to resolve later. See ``Database.cache_market_resolution``'s docstring
    for the writer-side half of this contract.

    ``hits``/``misses`` count every ``market in cache`` check a run makes
    (i.e. once per ``resolve_payout()`` lookup): a hit is answered without
    a network call (already known this run, or found in the persistent
    table); a miss requires a resolution be fetched (batched or, on
    fallback, single-market).

    **Batching's effect on these counts (issue #1227).** ``run()`` passes
    ``batch_resolve=True`` to ``backtest_wallet()``, which -- before its own
    per-trade loop -- batch-resolves a wallet's distinct BUY markets that
    aren't already in this cache and primes it with the results (see
    ``backtest_wallet``'s docstring). That priming step's own
    ``market not in cache`` check is what earns the "miss" for a genuinely
    new market (exactly once, matching the pre-#1227 first-encounter miss);
    every later ``resolve_payout()`` check for that market -- including
    what used to be that very first trade -- now finds it already primed,
    so it counts as a hit. Net effect: for a wallet with N buy trades
    against a newly-seen market, the pre-#1227 count was 1 miss + (N-1)
    hits; with batching it's 1 miss + N hits -- one hit higher, because the
    network work that used to happen inline on the first trade now happens
    in the priming step instead. ``misses`` still means exactly what it
    always meant ("this many distinct markets needed a network round trip
    this run"); only ``hits`` is nominally larger as a mechanical side
    effect of moving that work earlier, not a change in what "resolved
    from cache" means.
    """

    def __init__(self, db: "Database | None" = None):
        super().__init__()
        self._db = db
        self.hits = 0
        self.misses = 0

    def __contains__(self, market) -> bool:
        if super().__contains__(market):
            self.hits += 1
            return True
        if self._db is not None:
            cached = self._db.get_cached_market_resolution(market)
            if cached is not None:
                super().__setitem__(market, cached)
                self.hits += 1
                return True
        self.misses += 1
        return False

    def __setitem__(self, market, value) -> None:
        super().__setitem__(market, value)
        if value is not None and self._db is not None:
            self._db.cache_market_resolution(market, value)


def sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def check_stability(current: dict, previous: "dict | None") -> bool:
    """True only if *current*'s run agrees with the immediately-previous run.

    Pure function, no I/O. ``previous=None`` (first-ever run for a wallet)
    is always unstable -- there's nothing yet to agree with. A wallet is
    stable only when BOTH:

    (a) ``sign(current["median_roi"]) == sign(previous["median_roi"])`` --
        a ``median_roi`` of exactly ``0`` never matches another ``0``
        (treated as unstable, not stable-at-zero).
    (b) ``n_resolved`` hasn't swung by more than 25% relative to the
        previous run's ``n_resolved`` (floor of 1 in the denominator so a
        previous ``n_resolved=0`` can't divide by zero).

    ``median_roi`` on EITHER side being ``None`` (a wallet with zero
    resolved trades -- see ``copy_trade_backtest.py``'s ``_stats()``) is
    treated as unstable rather than raising, so this stays safe for any
    caller, not just one that happens to pre-filter zero-resolved wallets
    (issue #1245). ``sign()`` itself is never called with ``None`` -- this
    guard runs first, so ``sign()`` stays a plain numeric helper.
    """
    if previous is None:
        return False

    current_median = current["median_roi"]
    previous_median = previous["median_roi"]
    if current_median is None or previous_median is None:
        return False

    current_sign = sign(current_median)
    previous_sign = sign(previous_median)
    if current_sign == 0 or previous_sign == 0 or current_sign != previous_sign:
        return False

    previous_n = previous["n_resolved"]
    delta_ratio = abs(current["n_resolved"] - previous_n) / max(previous_n, 1)
    if delta_ratio > 0.25:
        return False

    return True


def check_quality(
    current: dict, previous: "dict | None", min_resolved_trades: "int | None" = None,
) -> "tuple[bool, str]":
    """True (with reason ``"ok"``) only if *current* passes every
    sample-size/profitability/tail-risk/history-completeness quality gate
    (issue #1209, relaxed for truncated wallets by issue #1217, sample size
    added by issue #1248).

    Pure function, no I/O -- *previous* is the same immediately-previous
    screening row the caller already fetched for ``check_stability()``, not
    a new query, and *min_resolved_trades* is the already-resolved
    threshold value (live-config lookup, if any, happens in the caller --
    see ``_min_resolved_trades_threshold()`` -- not in here). Composable
    with -- not folded into -- ``check_stability()``: a wallet must pass
    BOTH to be ``eligible_to_follow``. Checked in this order:

    (a) ``current["n_resolved"]`` must be >= *min_resolved_trades*
        (``QUALITY_MIN_RESOLVED_TRADES`` if not given) -- checked FIRST, so
        that a wallet failing both this and a downstream condition logs the
        real disqualifier (too few observations to draw any conclusion at
        all) rather than a symptom of it (e.g. an unstable-looking
        ``median_roi``). Boundary is inclusive: exactly
        *min_resolved_trades* passes. See 0x365f951dc2
        (``n_resolved=2``, ``median_roi=+2.79``) -- the coin-flip case this
        condition exists to catch.
    (b) ``current["flat_dollar_pnl"]`` must be > ``QUALITY_MIN_FLAT_DOLLAR_PNL``
        -- profitable under the flat-stake model we actually trade, not just
        under the wallet's own position sizing. A missing (``None``)
        ``flat_dollar_pnl`` (e.g. the run was invoked without
        ``--flat-stake``) fails this check too, since flat-stake
        profitability cannot be confirmed without it.
    (c) When ``median_roi > 0``, ``mean_roi`` must be <=
        ``QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO * median_roi`` -- rejects
        tail-driven wallets whose P&L is concentrated in a few large
        winners, a profile that does not survive flat-stake copying (see
        docs/design/copy-trading-architecture.md, "Background").
    (d) ``median_roi`` must be > ``QUALITY_MIN_MEDIAN_ROI``.
    (e) ``current["truncated"]`` (issue #1233 -- reported by
        ``get_wallet_trades()``/``backtest_wallet()``, not re-derived here
        from a trade count against a constant; see the module-level comment
        above condition (e)'s old constant for why that approach is
        deliberately not being repeated a third time). When it's ``True``
        -- the wallet's fetched history is genuinely incomplete, so its
        metrics aren't run-to-run stable on their own -- it is admitted
        only if the flat-stake edge *reproduced*: ``previous["flat_dollar_pnl"]``
        must also be ``> 0``. (b) above already requires the CURRENT run to
        be positive; this is a second, independent confirmation from the
        PREVIOUS run, not a restatement of (b). Two distinct rejection
        reasons, so the logs tell apart "can't tell yet" from "checked, and
        it didn't reproduce":

        - ``previous`` is ``None`` (no prior run for this wallet) or its
          ``flat_dollar_pnl`` is itself ``None`` (that prior run was invoked
          without ``--flat-stake``) -- reproducibility cannot be established
          from a single data point either way, so this fails as
          ``"trade_history_truncated_no_previous_run"``.
        - ``previous`` exists and its ``flat_dollar_pnl`` is <= 0 -- the
          edge did NOT reproduce (see issue #1217's ``0x5268527977`` case:
          current +1129, previous -671), so this fails as
          ``"trade_history_truncated_unreproducible"``.

    Checks (c) and (d) interact: (c) only fires when ``median_roi > 0``, so
    a wallet with ``median_roi <= 0`` always falls through to fail on (d)
    instead -- the relative order of (c) and (d) is not load-bearing for
    correctness, only for which failure reason gets logged first.
    """
    if min_resolved_trades is None:
        min_resolved_trades = QUALITY_MIN_RESOLVED_TRADES
    if current["n_resolved"] < min_resolved_trades:
        return False, "insufficient_resolved_trades"

    flat_pnl = current.get("flat_dollar_pnl")
    if flat_pnl is None or not flat_pnl > QUALITY_MIN_FLAT_DOLLAR_PNL:
        return False, "flat_dollar_pnl_not_positive"

    median_roi = current["median_roi"]
    mean_roi = current["mean_roi"]
    if median_roi > 0 and mean_roi > QUALITY_MAX_MEAN_MEDIAN_ROI_RATIO * median_roi:
        return False, "tail_driven_pnl"

    if not median_roi > QUALITY_MIN_MEDIAN_ROI:
        return False, "median_roi_not_positive"

    if current.get("truncated"):
        previous_flat_pnl = previous.get("flat_dollar_pnl") if previous else None
        if previous_flat_pnl is None:
            return False, "trade_history_truncated_no_previous_run"
        if not previous_flat_pnl > 0:
            return False, "trade_history_truncated_unreproducible"

    return True, "ok"


def _min_resolved_trades_threshold(db: "Database | None") -> int:
    """Return the live ``COPY_SCREEN_MIN_RESOLVED_TRADES`` value as an
    ``int``, falling back to ``QUALITY_MIN_RESOLVED_TRADES`` -- logged at
    WARNING, not silently -- if *db* is ``None`` or the live-config lookup
    itself fails (e.g. DB corruption) or somehow returns a non-numeric
    value. Mirrors ``copy_wallet_health.py``'s ``_min_decisions_threshold()``
    (issue #1225) -- follow that pattern rather than inventing a second one.
    """
    if db is None:
        return QUALITY_MIN_RESOLVED_TRADES
    try:
        raw = get_live_config(db).get(
            "COPY_SCREEN_MIN_RESOLVED_TRADES", QUALITY_MIN_RESOLVED_TRADES,
        )
        return int(raw)
    except Exception as e:
        log.warning(
            "[copy-wallet-screening] failed to read COPY_SCREEN_MIN_RESOLVED_TRADES "
            "from live config (%s) -- falling back to module default %d",
            e, QUALITY_MIN_RESOLVED_TRADES,
        )
        return QUALITY_MIN_RESOLVED_TRADES


def run(
    window: str, top: int, slippage_bps: float, min_trades: int = 0,
    flat_stake: "float | None" = None, db: "Database | None" = None,
    screened_at: "str | None" = None,
) -> int:
    screened_at = screened_at or datetime.now(timezone.utc).isoformat()

    if not flat_stake:
        log.warning(
            "[copy-wallet-screening] flat_stake is not set -- flat_dollar_pnl "
            "cannot be computed, so every wallet screened this run will fail "
            "the quality gate on flat_dollar_pnl_not_positive (see "
            "check_quality()). This is one bad invocation, not many bad wallets."
        )

    effective_top = top
    if top > MAX_WALLETS_PER_RUN:
        log.warning(
            "[copy-wallet-screening] --top=%s exceeds MAX_WALLETS_PER_RUN=%s -- "
            "clamping (rate-limit budget, see module docstring).",
            top, MAX_WALLETS_PER_RUN,
        )
        effective_top = MAX_WALLETS_PER_RUN

    leaderboard = get_leaderboard(window=window, limit=effective_top)
    addresses = [a for a in (wallet_address(e) for e in leaderboard) if a]
    # Belt-and-suspenders: enforce the cap regardless of what the leaderboard
    # endpoint actually returns for `limit` (defensive against a non-compliant
    # or future API response).
    addresses = addresses[:MAX_WALLETS_PER_RUN]
    if not addresses:
        log.error(
            "[copy-wallet-screening] leaderboard returned no usable wallet "
            "addresses (endpoint may be unavailable, or its response shape "
            "has changed -- see get_leaderboard's docstring in "
            "src/data/polymarket_traders.py)."
        )
        return 1

    db = db or Database()

    # Issue #1248: resolved once per run, not once per wallet -- mirrors
    # resolution_cache below and copy_wallet_health.py's own threshold read.
    min_resolved_trades = _min_resolved_trades_threshold(db)

    # Issue #1221: one resolution cache shared by every wallet in this run
    # (tier 1), backed by `db`'s persistent market_resolutions table (tier
    # 2) -- a resolved market costs at most one network call across the
    # whole run, and never again on a later run.
    resolution_cache = PersistentResolutionCache(db)

    n_screened = 0
    n_errors = 0
    for address in addresses:
        # Issue #1245: one wallet raising must never abort the rest of the
        # run -- mirrors copy_settle.py's per-row try/except (see that
        # module's docstring). KeyboardInterrupt/SystemExit are NOT
        # subclasses of Exception, so they still propagate and stop the run.
        try:
            # Issue #1227: batch_resolve=True makes backtest_wallet resolve
            # this wallet's distinct new markets with a handful of batched
            # Gamma API requests (repeated condition_ids keys) before its
            # own per-trade loop, instead of one sequential request per new
            # market.
            result = backtest_wallet(
                address, slippage_bps, flat_stake, cache=resolution_cache, batch_resolve=True,
            )
            if result["n_resolved"] < min_trades:
                log.info(
                    "[copy-wallet-screening] %s: n_resolved=%s below --min-trades=%s, skipping.",
                    address, result["n_resolved"], min_trades,
                )
                continue

            copier = result["copier"]

            # Issue #1245: a wallet with zero resolved trades has
            # win_rate/mean_roi/median_roi all None (_stats(), see
            # copy_trade_backtest.py) -- it carries no usable signal, so
            # screening it further (stability/quality) is meaningless.
            # min_trades defaults to 0, so `n_resolved < min_trades` above
            # does NOT catch this case -- must be checked separately.
            if copier["median_roi"] is None:
                log.info(
                    "[copy-wallet-screening] %s: n_resolved=%s but median_roi is "
                    "None (no usable signal), skipping.",
                    address, result["n_resolved"],
                )
                continue

            flat_pnl = None
            if flat_stake is not None:
                flat_pnl = result.get("copier_flat", {}).get("dollar_pnl")

            current = {
                "n_buy_trades": result["n_buy_trades"],
                "n_sell_excluded": result["n_sell_excluded"],
                "n_resolved": result["n_resolved"],
                "win_rate": copier["win_rate"],
                "mean_roi": copier["mean_roi"],
                "median_roi": copier["median_roi"],
                "flat_dollar_pnl": flat_pnl,
                # issue #1233: reported by get_wallet_trades() and threaded
                # through by backtest_wallet() -- see check_quality()'s
                # condition (d).
                "truncated": result.get("truncated", False),
            }

            previous_rows = db.get_recent_wallet_screenings(address, limit=1)
            previous = previous_rows[0] if previous_rows else None
            stable = check_stability(current, previous)
            quality_ok, quality_reason = check_quality(
                current, previous, min_resolved_trades=min_resolved_trades,
            )
            if not quality_ok:
                log.info(
                    "[copy-wallet-screening] %s: failed quality check (%s), not eligible.",
                    address, quality_reason,
                )
            eligible = stable and quality_ok

            db.insert_wallet_screening(
                address=address,
                window=window,
                screened_at=screened_at,
                n_buy_trades=current["n_buy_trades"],
                n_resolved=current["n_resolved"],
                win_rate=current["win_rate"],
                mean_roi=current["mean_roi"],
                median_roi=current["median_roi"],
                mirrored_dollar_pnl=copier["dollar_pnl"],
                flat_dollar_pnl=flat_pnl,
                flat_stake=flat_stake,
                slippage_bps=slippage_bps,
                eligible_to_follow=int(eligible),
                truncated=int(current["truncated"]),
            )
            n_screened += 1
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            n_errors += 1
            log.warning(
                "[copy-wallet-screening] %s: screening failed, skipping: %s",
                address, e,
            )

    log.info(
        "[copy-wallet-screening] screened %s/%s wallets (window=%s, top=%s, errors=%s).",
        n_screened, len(addresses), window, effective_top, n_errors,
    )
    log.info(
        "[copy-wallet-screening] resolution cache: %s hits, %s misses.",
        resolution_cache.hits, resolution_cache.misses,
    )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window", default="month", choices=sorted(["day", "week", "month", "all"]))
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS)
    ap.add_argument(
        "--flat-stake", type=float, default=5.0,
        help=(
            "Fixed $ amount spent on every trade for the flat-stake copier "
            "scenario, mirroring copy_trade_backtest.py's --flat-stake -- "
            "defaults to $5.00/trade, matching the spike's winning sizing."
        ),
    )
    ap.add_argument(
        "--min-trades", type=int, default=0,
        help=(
            "Skip a wallet below this n_resolved BEFORE persisting anything -- no "
            "auditable copy_wallet_candidates row. Not the same as check_quality()'s "
            "COPY_SCREEN_MIN_RESOLVED_TRADES condition, which records a judgement "
            "(eligible_to_follow=0) instead of skipping; see the module docstring."
        ),
    )
    args = ap.parse_args(argv)

    # Issue #1247: acquire single-instance lock before any API calls.
    # A second concurrent invocation exits immediately.
    global _LOCK
    lock_file = Path(__file__).resolve().parents[2] / "data" / ".copy_wallet_screening.lock"
    _LOCK = lock_acquired(lock_file)
    if _LOCK is None:
        log.error(
            "[copy-wallet-screening] already running (lock file %s held by another process), "
            "exiting.",
            lock_file,
        )
        return 1

    return run(
        args.window, args.top, args.slippage_bps, args.min_trades, args.flat_stake,
    )


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    raise SystemExit(main())
