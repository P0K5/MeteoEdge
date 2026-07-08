"""Model calibration report: raw_p_yes vs actual market resolutions (issue #648).

Joins scanner snapshots (``logs/snapshots.*.jsonl[.gz]``, which carry the
pre-clamp ``raw_p_yes`` since 2026-06-18 / issue #551) to definitive market
resolutions and prints:

1. Reliability table over raw_p_yes buckets — one FINAL sample per market
   (the last snapshot before settlement; cleanest, independent samples)
2. The same over hourly samples (one per market per hour; larger n but
   autocorrelated — read as a consistency check, not headline numbers)
3. Reliability by lead-time band (final sample within each band per market)
4. The NO-entry gate check: among final samples with capped p_yes <=
   MAX_CONFIDENCE_YES_FOR_NO the observed YES frequency should be at or
   below that threshold if the model is calibrated. This is the number the
   whole NO strategy rests on.
5. Brier scores (lower is better; 0.25 = coin flip at p=0.5).

Resolution sources, in order:
- ``settlements`` rows whose stored outcome is definitive (market_final_price
  pinned at an extreme, or resolution_source gamma/gamma_repair)
- a local JSON cache (``data/resolution_cache.json``) of previous Gamma fetches
- live Gamma fetches for anything still missing (skip with ``--no-fetch``)

Read-only apart from the JSON cache. Run ad hoc or weekly:

    .venv/bin/python -m src.scripts.calibration_report [--since 2026-06-18]
"""
import argparse
import glob
import gzip
import json
import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

from src.config import LOG_DIR

RESOLUTION_CACHE = os.path.join("data", "resolution_cache.json")
RAW_P_YES_AVAILABLE_FROM = "2026-06-18"  # issue #551 deploy date

# Lead-time bands in minutes_to_settlement (label, lo, hi)
LEAD_BANDS = [("<3h", 0, 180), ("3-8h", 180, 480), (">8h", 480, 10 ** 9)]

BUCKET_EDGES = [0.0, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 1.001]


# ---------------------------------------------------------------------------
# Pure computation (unit-tested in test_calibration_report.py)
# ---------------------------------------------------------------------------

def pick_samples(snapshots: "list[dict]") -> "tuple[dict, dict]":
    """Reduce raw snapshots to (final_by_ticker, hourly_by_key).

    final_by_ticker: ticker -> the snapshot with the LOWEST
        minutes_to_settlement (closest to resolution).
    hourly_by_key: (ticker, ts[:13]) -> last snapshot in that hour.
    Snapshots without a raw_p_yes or a 0x ticker are ignored.
    """
    final: dict = {}
    hourly: dict = {}
    for s in snapshots:
        ticker = str(s.get("ticker") or "")
        if not ticker.startswith("0x") or s.get("raw_p_yes") is None:
            continue
        prev = final.get(ticker)
        if prev is None or (s.get("minutes_to_settlement") or 0) < (prev.get("minutes_to_settlement") or 0):
            final[ticker] = s
        hkey = (ticker, str(s.get("ts") or "")[:13])
        hprev = hourly.get(hkey)
        if hprev is None or str(s.get("ts") or "") > str(hprev.get("ts") or ""):
            hourly[hkey] = s
    return final, hourly


def build_reliability(samples: "list[tuple[float, bool]]",
                      edges: "list[float]" = BUCKET_EDGES) -> "list[dict]":
    """Bucket (predicted_p, yes_won) pairs and return reliability rows."""
    rows = []
    for lo, hi in zip(edges, edges[1:]):
        inb = [(p, w) for p, w in samples if lo <= p < hi]
        if not inb:
            continue
        n = len(inb)
        rows.append({
            "bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "n": n,
            "mean_pred": sum(p for p, _ in inb) / n,
            "obs_freq": sum(1 for _, w in inb if w) / n,
        })
    return rows


def brier_score(samples: "list[tuple[float, bool]]") -> "float | None":
    if not samples:
        return None
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in samples) / len(samples)


def format_reliability(rows: "list[dict]", title: str) -> str:
    out = [f"\n=== {title} ===",
           f"{'raw_p_yes':>11} {'n':>6} {'mean_pred':>9} {'obs_YES%':>9} {'gap':>7}"]
    for r in rows:
        gap = r["obs_freq"] - r["mean_pred"]
        out.append(f"{r['bucket']:>11} {r['n']:>6} {100 * r['mean_pred']:>8.1f}% "
                   f"{100 * r['obs_freq']:>8.1f}% {100 * gap:>+6.1f}%")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_snapshots(since: str) -> "list[dict]":
    snaps = []

    def read(fh):
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(s.get("ts") or "")[:10] >= since:
                snaps.append(s)

    for path in sorted(glob.glob(os.path.join(str(LOG_DIR), "snapshots*.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            read(fh)
    for path in sorted(glob.glob(os.path.join(str(LOG_DIR), "snapshots*.jsonl.gz"))):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            read(fh)
    return snaps


def load_resolutions(db, tickers: "set[str]", fetch: bool, workers: int) -> dict:
    """Return {ticker: yes_won(bool)} from settlements, cache, then Gamma."""
    res: dict = {}

    if db is not None:
        try:
            for r in db._conn.execute(
                "SELECT ticker, resolved_yes, market_final_price, resolution_source FROM settlements"
            ):
                t, ry, mfp, src = r[0], r[1], r[2], r[3]
                if t not in tickers:
                    continue
                definitive = (mfp is not None and (mfp >= 95 or mfp <= 5)) or (
                    src in ("gamma", "gamma_repair"))
                if definitive:
                    res[t] = bool(ry)
        except Exception as e:
            # A torn DB copy (e.g. snapshot taken mid-write) must not kill the
            # report — the cache + Gamma fetch path covers the same tickers.
            log.warning("[calibration] settlements read failed (%s) — "
                        "falling back to cache/Gamma", e)

    cache = {}
    if os.path.exists(RESOLUTION_CACHE):
        try:
            cache = json.load(open(RESOLUTION_CACHE))
        except (json.JSONDecodeError, OSError):
            cache = {}
    for t, v in cache.items():
        if t in tickers and t not in res and v is not None:
            res[t] = bool(v)

    def save_cache():
        try:
            os.makedirs(os.path.dirname(RESOLUTION_CACHE), exist_ok=True)
            json.dump(cache, open(RESOLUTION_CACHE, "w"))
        except OSError as e:
            log.warning("[calibration] could not write %s: %s", RESOLUTION_CACHE, e)

    missing = sorted(t for t in tickers if t not in res)
    if fetch and missing:
        from src.data.polymarket import fetch_market_resolution
        print(f"fetching {len(missing)} unresolved markets from Gamma "
              f"({workers} workers)...")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for i, (t, r) in enumerate(zip(missing, ex.map(fetch_market_resolution, missing)), 1):
                cache[t] = r
                if r is not None:
                    res[t] = r
                # Flush periodically so an interrupted run resumes from where
                # it stopped instead of refetching thousands of markets.
                if i % 500 == 0:
                    save_cache()
                    print(f"  {i}/{len(missing)} fetched...")
        save_cache()
    return res


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=RAW_P_YES_AVAILABLE_FROM,
                    help="earliest snapshot date (YYYY-MM-DD)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="offline: use settlements + cache only")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-gate-threshold", type=float, default=0.05,
                    help="capped p_yes threshold of the live NO gate")
    args = ap.parse_args()

    try:
        from src.data.db import Database
        db = Database()
    except Exception as e:
        print(f"DB unavailable ({e}) — relying on cache/Gamma only")
        db = None

    snaps = load_snapshots(args.since)
    final, hourly = pick_samples(snaps)
    print(f"snapshots since {args.since}: {len(snaps)}, "
          f"markets: {len(final)}, hourly samples: {len(hourly)}")

    res = load_resolutions(db, set(final), fetch=not args.no_fetch,
                           workers=args.workers)
    print(f"markets with definitive resolution: {len(res)}/{len(final)}")

    final_samples = [(s["raw_p_yes"], res[t]) for t, s in final.items() if t in res]
    hourly_samples = [(s["raw_p_yes"], res[t]) for (t, _h), s in hourly.items() if t in res]

    print(format_reliability(build_reliability(final_samples),
                             "Reliability — final snapshot per market"))
    print(format_reliability(build_reliability(hourly_samples),
                             "Reliability — hourly samples (autocorrelated)"))

    for label, lo, hi in LEAD_BANDS:
        band_final: dict = {}
        for (t, _h), s in hourly.items():
            m = s.get("minutes_to_settlement") or 0
            if not (lo <= m < hi) or t not in res:
                continue
            prev = band_final.get(t)
            if prev is None or m < (prev.get("minutes_to_settlement") or 0):
                band_final[t] = s
        samples = [(s["raw_p_yes"], res[t]) for t, s in band_final.items()]
        if samples:
            print(format_reliability(build_reliability(samples),
                                     f"Reliability — lead time {label} (n markets={len(samples)})"))

    # The number the NO strategy rests on: when the (capped) model says
    # "YES <= threshold", how often does YES actually happen?
    gate = [(t, s) for t, s in final.items()
            if t in res and (s.get("capped_p_yes", s.get("p_yes")) or 1) <= args.no_gate_threshold]
    if gate:
        obs = sum(1 for t, _s in gate if res[t]) / len(gate)
        print(f"\n=== NO-gate check (capped p_yes <= {args.no_gate_threshold:.2f}) ===")
        print(f"markets: {len(gate)}, observed YES frequency: {100 * obs:.1f}% "
              f"(calibrated would be <= {100 * args.no_gate_threshold:.0f}%)")
        print("Every percentage point above the threshold is unpriced risk the "
              "NO side carries at entry.")

    b_final = brier_score(final_samples)
    b_hourly = brier_score(hourly_samples)
    print(f"\nBrier score (final): {b_final:.4f}" if b_final is not None else "")
    print(f"Brier score (hourly): {b_hourly:.4f}" if b_hourly is not None else "")

    if db is not None:
        db.close()


if __name__ == "__main__":
    from src.logging_config import setup_logging
    setup_logging()
    main()
