# Pre-registration — does a model beat the PRICE on Polymarket crypto up/down?

**Status: DRAFT, awaiting sign-off. Nothing below may change after the first
powered run.**

**Written 2026-09-05, with 1,182 usable BTC-5m windows collected and NO
outcome-conditional analysis performed.** No win rate, no EV, and no
encompassing coefficient has been computed on this data by anyone. That is what
makes the bars below meaningful, and it is the only reason to write this now
rather than later.

---

## Why this document exists

The weather thesis died of a measurement error, not a modelling one. For months
it measured skill against a **50/50 prior** instead of **against the market
price**, and only the encompassing test settled it:

> `logit P(YES) = a + b·logit(p_market) + c·logit(p_model)`
> → `c = +0.0125`, 95% CI `[−0.0875, +0.1281]`, and out of sample the combined
> forecast was **worse** than the market alone.

A logistic model on Binance order-flow predicts the **TWAP target** with
**+6.9σ** out-of-sample skill (51.9% overall, 55.4% on the top decile). That is
skill against a 50/50 prior. It is the same number that misled the weather work,
and it is **not** evidence of a tradable edge.

This document fixes what would count as evidence, before looking.

---

## The question

At the moment you could actually commit — window open, real book on screen —
does a model carry information the price does not?

Nothing else. Not profitability at scale, not execution quality, not whether a
better model could exist.

---

## Population — fixed now

- **Series:** BTC, 5-minute windows. **BTC 5m only.** ETH and the 15m series are
  held out entirely as a replication set (see "Replication").
- **Decision point:** the quote nearest `seconds_to_settlement = 300` (window
  open), tolerance ±30s, one row per window, nearest wins.
  *Rationale: entry at window open is the honest test. Later entries see
  information the price has already absorbed — the market demonstrably moves
  intra-window (0.47–0.59 observed at entry; 0.79/0.80 observed mid-flight).*
- **Required fields:** `best_bid` and `best_ask` both non-NULL, and
  `0 < bid ≤ ask ≤ 1`.
- **Outcome:** `resolutions.resolved_up` non-NULL, from Gamma's settled
  `outcomePrices`. Ambiguous settlements stay NULL and are **dropped, never
  guessed**.
- **Excluded:** windows overlapping the 2026-09-01 02:00 → 2026-09-02 07:00 UTC
  outage; and any window whose entry quote came from a poll with `ok=0`
  (verified zero, stated as a rule regardless).

## Power bar

**≥ 1,000 resolved windows in the evaluation half.**

Each window is one independent outcome — unlike the weather gate, where ~11
brackets shared a single daily high. At ~288 windows/day this is quickly met.

## Fit / holdout split — fixed by date, now

- **Fit:** windows with `window_end` **before 2026-09-06 00:00 UTC**
- **Evaluate:** windows **on or after 2026-09-06 00:00 UTC**, to the run date

The split is a **future date at the time of writing**, so no fitting choice can
be informed by evaluation data. The model is fit **once** on the fit half, and
every number that governs the verdict comes from the evaluation half.

## The model

`p_model` = the logistic model on the corrected TWAP target: lagged window
returns, order-flow imbalance (taker-buy share), volume z-score, realized range,
and hour-of-day — built **only** from Binance bars strictly before the window
opens. Fit on the fit half. **No feature may be added, removed, or retuned after
the evaluation half is scored.**

Binance is a **proxy** for the Chainlink 60s-TWAP oracle these markets settle on;
the two disagree on a minority of windows. `p_model` is a forecast, not a
reconstruction of the oracle.

## The market probability

`p_market` = `(best_bid + best_ask) / 2`, from the **book**.

Never `outcomePrices` — verified unusable on live windows (a book of 0.47/0.48
carried `outcomePrices` 0.735; a book of 0.79/0.80 carried 0.935).

---

## The primary test

```
logit P(Up) = a + b·logit(p_market) + c·logit(p_model)
```

**`c` is the verdict.** 95% CI by bootstrap resampled over **windows** (the
independent unit), 2,000 replicates.

| result | verdict |
|---|---|
| `c` CI excludes 0 **AND** the combined forecast beats market-alone on out-of-sample Brier | **FINDING** — proceed to the EV gate |
| anything else | **NULL** — the price already contains the model. Stop. |

## The EV gate — evaluated only if the primary test is a FINDING

A positive `c` is a statistical statement. This is the economic one.

Costs from the published schedule (`docs.polymarket.com/trading/fees`), **crypto
rate 0.07** — not weather's 0.05:

```
fee_per_contract = 0.07 · p · (1 − p)      # 1.75¢ at 50¢, the curve's maximum
```

Entry is the **ask you would pay as a taker**, never the mid:

- Buying **Up**:   `cost = best_ask`
- Buying **Down**: `cost = 1 − best_bid`  *(Down is the complement; the stored
  book is the Up token's)*

```
EV_per_contract = p_true · 1.00 − cost − fee(cost)
```

with `p_true` the **realized** rate of the selected group — never `p_model`.

| result | verdict |
|---|---|
| EV ≥ **+1.0¢/contract** at the **95% lower bound**, on ≥1,000 evaluation windows, **and** positive in both directions (Up-side and Down-side scored separately) | **TRADABLE FINDING** |
| anything else | **NULL** |

**Why the lower bound, not the point estimate.** The weather rail edge was
`+0.86¢` at the point estimate and vanished entirely once the true sub-penny
price was known. A bar that a measurement error can clear is not a bar.

**Why +1.0¢ and not breakeven.** The observed entry spread is ~1¢
(`best_bid 0.50 / best_ask 0.51`). An edge smaller than one tick of spread is
indistinguishable from execution noise.

## Capacity bar

Median `liquidity` at the decision point on selected windows must be
**≥ $5,000**. Observed to date is ~$10,150, so this should pass — it is stated so
that a thin-book edge cannot later be presented as a product. An edge you can
trade $200 of is not one.

## Selection thresholds — fixed now

If the primary test is a FINDING, the EV gate is evaluated at exactly three
pre-declared selections, and **all three are reported**:

1. **all** windows
2. top **25%** by `|p_model − 0.5|`
3. top **10%** by `|p_model − 0.5|`

No other threshold may be introduced. The weather work showed how easily a
post-hoc slice manufactures a result: M3b's "top decile" was chosen after seeing
the numbers, and its top-1% bucket was non-monotonic — which is what noise looks
like.

## One look

The test runs **once**, at the power bar. Progress checks read the **window count
only** (`healthcheck.sh`), never a coefficient or a win rate. `encompass.py`
enforces this — it refuses to print a coefficient below `--min-windows`.

An underpowered look is not a verdict in either direction and is not argued from
in either direction. Looking twice inflates the false-positive rate whatever the
first look appeared to show.

## Replication — what a FINDING must survive

A FINDING on BTC 5m is re-run **unchanged** on the held-out series: **ETH 5m**,
**BTC 15m**, **ETH 15m**. Same model family, same bars, refit per series.

A result that appears on BTC 5m and none of the other three is reported as **not
replicated**, and licenses nothing further.

## Stated prior

**This is expected to return NULL.**

Recorded so that a negative result is not a surprise, and a positive one must
clear a bar set before it was seen:

- the market is well calibrated everywhere it has been measured in this project;
- order-flow imbalance is public data, visible to every participant;
- the entry spread (~1¢) is the same order as the entire modelled edge;
- the last two theses (weather M3, and the rail) both died precisely here.

The value of this test is that it is **cheap and decisive**, not that it is
likely to pay.

## What a FINDING does and does not license

- ✅ A written follow-up proposal, and the replication run above.
- ❌ **Not** live trading. Not paper trading with real orders. Not a size
  decision. #1053/#1054's halt is unconditional and out of scope here; this
  package places no orders and is structurally incapable of doing so
  (`tests/test_isolation.py`).

## What this does not reopen

M3 (`BSS = −0.4123`), M3b (FAIL), M3c (NULL) and the forecast-stack pivot
(`ΔBSS = −0.0031`) are final. This is a different market, a different thesis and
a different population. It is not a rescue of any of them.

---

## Sign-off

| | |
|---|---|
| Drafted | 2026-09-05, at 1,182 usable windows, no outcome-conditional analysis performed |
| Approved by | _pending_ |
| Date fixed | _pending — nothing above may change after this date_ |
