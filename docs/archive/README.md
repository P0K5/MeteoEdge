# Archive — MeteoEdge (Kalshi Weather Bot)

These documents describe the original project: an autonomous trading bot for Kalshi intraday temperature bracket contracts.

## Why this is archived

The MeteoEdge strategy itself was **validated** — the observe-only spike (see `../../archive/meteoedge-spike/`) confirmed the physical-envelope edge existed in live Kalshi data. Results were aligned with the spec's expectations.

**The pivot was forced by platform risk, not strategy failure:** Kalshi ceased operations for Portuguese residents, removing access to the market. Without a venue, there is no strategy.

## What to reuse

The project pivot to **FundingEdge** (Binance funding-rate arbitrage, see `../funding-edge-spec.md`) deliberately reuses the architectural patterns proven here:

- Four-stage testing gate (spike → backtest → demo → micro-live → scale)
- LLM sanity-check pattern (provider-agnostic: Claude / DeepSeek / OpenAI)
- Risk manager framework (kill switch, position limits, daily loss floor)
- PostgreSQL + Redis data layer, systemd unit structure
- FastAPI + HTMX operator dashboard
- Backtest harness principles (time-gated queries, no future leakage, out-of-sample discipline)
- Multi-agent development workflow (Tech Lead PM, Designer, Mid Dev, Junior Dev)

## Contents

| File | What it was |
|---|---|
| `kalshi-weather-bot-spec.md` | Full technical specification (v0.1) |
| `meteoedge-mvp-spike.md` | Observe-only spike design |
| `meteoedge-backtest-harness.md` | Backtest harness design (Epic 7) |
| `Implementation-plan.md` | Epic breakdown and story estimation |

The working spike code lives at `../../archive/meteoedge-spike/`.

---

**Archived:** 2026-04-24
