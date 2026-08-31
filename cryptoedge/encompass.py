"""The decision test: does a model add anything the MARKET PRICE does not?

This is the test the weather thesis needed and did not run for months. M3 asked
"is the model better than the market" (BSS) and answered no; but a forecaster can
be worse standalone and still be worth combining. The Fair-Shiller /
Granger-Ramanathan encompassing regression is what settles it:

    logit P(Up) = a + b*logit(p_market) + c*logit(p_model)

    c ~ 0                -> the price already contains the model. Stop.
    c materially > 0     -> the model carries orthogonal information.

For weather the answer was c = +0.0125, 95% CI [-0.0875, +0.1281] -- nothing.
The point of this module is to ask the same question of crypto up/down markets
BEFORE building anything on a signal measured against a 50/50 prior.

Why that matters here specifically: a logistic model on Binance order-flow shows
+4.4 sigma directional skill against a 50/50 prior. That number is meaningless
for trading if the market price already reflects it -- which is exactly the trap
the weather work fell into.

INFERENCE: 95% bootstrap CI resampled over WINDOWS, not quote-rows. Many quotes
share one window and one outcome; row-level SEs would be badly overstated.

ONE LOOK: the test refuses to report below --min-windows and prints the count
only. An underpowered look is not a verdict in either direction, and looking
twice inflates the false-positive rate.
"""
from __future__ import annotations

import argparse
import logging
import sys

import numpy as np

from cryptoedge import db as cdb

log = logging.getLogger("cryptoedge.encompass")
EPS = 1e-3


def logit(p):
    return np.log(np.clip(p, EPS, 1 - EPS) / (1 - np.clip(p, EPS, 1 - EPS)))


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -500, 500)))


def fit_logistic(X, y, ridge=1e-6, iters=200):
    beta = np.zeros(X.shape[1])
    for _ in range(iters):
        mu = sigmoid(X @ beta)
        W = np.maximum(mu * (1 - mu), 1e-10)
        H = (X.T * W) @ X + ridge * np.eye(X.shape[1])
        step = np.linalg.solve(H, X.T @ (y - mu) - ridge * beta)
        beta += step
        if np.max(np.abs(step)) < 1e-10:
            break
    return beta


def market_p_up(row) -> "float | None":
    """Executable market P(Up), from the BOOK -- never ``outcomePrices``.

    ``outcomePrices`` is not a usable mid on a live window: observed
    2026-08-31, a window quoting bid 0.47 / ask 0.48 carried
    ``outcomePrices`` of 0.735, and another at bid 0.79 / ask 0.80 read 0.935.
    It lags or is derived differently. The book is what you can trade against.
    """
    bid, ask = row["best_bid"], row["best_ask"]
    if bid is None or ask is None or not (0 < bid < 1) or not (0 < ask <= 1):
        return None
    if ask < bid:
        return None
    return (bid + ask) / 2.0


def load_population(con, asset: str, window_min: int, at_seconds: float,
                    tol: float = 30.0):
    """One row per window: the quote nearest *at_seconds* before settlement.

    *at_seconds* is the decision point -- the moment you would have to commit.
    Entry at window START (at_seconds ~= window_min*60) is the honest test;
    later entries see information the price has already absorbed.
    """
    rows = con.execute(
        "SELECT q.*, r.resolved_up FROM quotes q"
        " JOIN resolutions r ON r.slug = q.slug"
        " WHERE q.asset=? AND q.window_min=? AND r.resolved_up IS NOT NULL"
        "   AND q.seconds_to_settlement IS NOT NULL"
        "   AND abs(q.seconds_to_settlement - ?) <= ?",
        (asset, window_min, at_seconds, tol)).fetchall()
    best = {}
    for r in rows:
        d = abs(r["seconds_to_settlement"] - at_seconds)
        if r["slug"] not in best or d < best[r["slug"]][0]:
            best[r["slug"]] = (d, r)
    return [v[1] for v in best.values()]


def run(con, asset="btc", window_min=5, at_seconds=None, min_windows=500,
        model_probs=None):
    at_seconds = window_min * 60 if at_seconds is None else at_seconds
    pop = load_population(con, asset, window_min, at_seconds)
    usable = [(r, market_p_up(r)) for r in pop]
    usable = [(r, p) for r, p in usable if p is not None]
    n = len(usable)
    print(f"population: {asset} {window_min}m at t-{at_seconds:.0f}s -> "
          f"{n} windows with a usable book and a settled outcome")
    if n < min_windows:
        print(f"UNDERPOWERED: {n} < {min_windows} windows. Reporting the COUNT "
              f"ONLY -- no coefficient is printed, by design. Keep collecting.")
        return None

    y = np.array([float(r["resolved_up"]) for r, _ in usable])
    pm = np.array([p for _, p in usable])
    print(f"  base rate P(Up) = {y.mean():.4f}")
    print(f"  mean market P(Up) = {pm.mean():.4f}   "
          f"mean spread = {np.mean([r['spread'] or np.nan for r, _ in usable]):.4f}")

    if model_probs is None:
        print("\nNo model probabilities supplied (--model none).")
        print("Market-only calibration check:")
        X = np.column_stack([np.ones(n), logit(pm)])
        b = fit_logistic(X, y)
        print(f"  a = {b[0]:+.4f}   b = {b[1]:+.4f}")
        print("  b ~ 1 and a ~ 0 means the market needs no recalibration --")
        print("  i.e. there is no free edge in simply re-scaling the price.")
        return {"a": b[0], "b": b[1], "n": n}

    pmod = np.asarray([model_probs[r["slug"]] for r, _ in usable], dtype=float)
    X_full = np.column_stack([np.ones(n), logit(pm), logit(pmod)])
    X_mkt = np.column_stack([np.ones(n), logit(pm)])
    bf, bm = fit_logistic(X_full, y), fit_logistic(X_mkt, y)
    print("\n=== ENCOMPASSING REGRESSION ===")
    print(f"  a = {bf[0]:+.4f}")
    print(f"  b = {bf[1]:+.4f}   (market)")
    print(f"  c = {bf[2]:+.4f}   (model)  <-- THE TEST")

    rng = np.random.default_rng(20260831)
    cs = []
    for _ in range(2000):
        idx = rng.integers(0, n, n)          # windows are the resample unit
        try:
            cs.append(fit_logistic(X_full[idx], y[idx])[2])
        except np.linalg.LinAlgError:
            pass
    cs = np.array(cs)
    lo, hi = np.percentile(cs, [2.5, 97.5])
    print(f"  c 95% window-bootstrap CI: [{lo:+.4f}, {hi:+.4f}]")
    print(f"  P(c > 0) = {np.mean(cs > 0):.4f}")
    bs_m = np.mean((sigmoid(X_mkt @ bm) - y) ** 2)
    bs_f = np.mean((sigmoid(X_full @ bf) - y) ** 2)
    print(f"  Brier: market-only {bs_m:.5f} -> combined {bs_f:.5f} "
          f"({bs_m - bs_f:+.5f})")
    return {"c": bf[2], "ci": (lo, hi), "n": n}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="data/cryptoedge.db")
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--window", type=int, default=5, help="window minutes")
    ap.add_argument("--at-seconds", type=float, default=None,
                    help="decision point, seconds before settlement "
                         "(default: window start)")
    ap.add_argument("--min-windows", type=int, default=500)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    con = cdb.connect(a.db)
    run(con, a.asset, a.window, a.at_seconds, a.min_windows)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
