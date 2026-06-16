# ⚠️ DEPRECATED: Early Spike Results (May 2026)

**Status**: DEPRECATED as of 2026-06-16

This directory contains the historical results from the original spike testing conducted in May 2026. These results are preserved for historical record only and should **NOT** be used for current decision-making or trading strategy.

## What's Here

- **OUTDATED_SIMULATION_RESULTS.md** — Paper trading simulation results (108.8% ROI on synthetic data)
- **OUTDATED_BACKTEST_SUMMARY.md** — Backtest on real May 2026 Polymarket data (88.4% win rate)

## Why Deprecated

The spike results, while historically significant for proving the core edge hypothesis, reflect:
- Early model calibration (now known to be overfitted)
- Station selection that has since been refined (Denver KBKF and Dallas KDAL excluded due to 0% win rate)
- Original implementation details that have evolved significantly
- Risk parameters that have been hardened for live trading

Current decision-making should be based on:
- Live trading results from `src/monitoring/`
- Current model in `src/model/envelope.py`
- Risk management in `src/risk/manager.py`

## Historical Reference

If you need the original backtest harness or simulation methodology:
- See `scripts/backtest_real_data.py` (marked as ARCHIVED)
- Refer to `docs/SPIKE_DOCUMENTATION.md` for the original analysis

---

**Preserved**: 2026-06-16  
**Last Updated**: 2026-05-08
