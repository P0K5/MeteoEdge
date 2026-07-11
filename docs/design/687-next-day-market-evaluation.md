# Design: closing the daily ~12h evaluation blackout (#687)

## Mechanism recap

`src/strategy/scanner.py:440-441` (`wrong_date` gate) rejects any market whose
`endDate` (UTC calendar date) is not `today_utc`. Non-US weather markets settle
once daily at a shared ~12:00 UTC instant, so from ~11:45 UTC (once today's
market falls under `MIN_MINUTES_TO_SETTLEMENT`) until ~00:00-00:04 UTC
(tomorrow's market becomes "today" by calendar date), the scanner has no
station-market pair it will evaluate at all: today's market is gated by
`outside_window`, tomorrow's is gated by `wrong_date`. That is a permanent
daily ~12h blackout, confirmed in 30/30 days of `bot.log` (#686).

`STATION_ACTIVE_HOURS` (`src/config.py:136`) is a second, independent gate on
local hours per station. For US stations the local active window already
overlaps this UTC dead zone, so the two gates compound: US stations lose
roughly three quarters of their configured local active window to the UTC
blackout, not just the raw ~12h.

The two gates are orthogonal and both must pass. This design only changes the
`wrong_date` gate's date-equality condition; `STATION_ACTIVE_HOURS` is
untouched.

## Proposal

Once today's market for a station is past `MIN_MINUTES_TO_SETTLEMENT` (i.e.
`outside_window` would fire on it), allow the scanner to evaluate the *next*
market for that station (selected by closest `endDate`/close time), instead
of hard-requiring `end_dt.date() == today_utc`. Concretely, in
`scanner.py`'s per-market loop:

- If `end_dt.date() == today_utc`: evaluate as today (unchanged path).
- If `end_dt.date() == today_utc + 1 day` **and** the station's *own* current
  ("today") market is past `MIN_MINUTES_TO_SETTLEMENT` (or absent from the
  fetched set): evaluate as a **next-day candidate**, forecast-only (see
  safety property below).
- Any other date: `wrong_date`, unchanged.

This is per-station, not global — a station whose today-market is still live
never has its next-day market evaluated early, preserving one-active-bracket-
set-per-station-per-day semantics.

## Safety property that must survive

**A bracket is only ever evaluated against weather state for its own
settlement date.** Today's METAR/current-observation trail says nothing
about what tomorrow's high will be. For a next-day market this means
**forecast-only evaluation**: no METAR-anchored intraday delta, no
`obs_bias`, no climb-table `max_env` derived from today's observations.

`WeatherState` field disposition for a next-day candidate:

| Field | Valid for next-day? | Why |
|---|---|---|
| `forecast_high_f` | **Yes** | Model forecast for tomorrow's date, not an observation |
| `secondary_forecast_f` | **Yes** | Same — forecast source, date-scoped |
| `deb_mu_f` | **Yes, if the DEB weighting was computed for tomorrow's forecast run** | DEB blends forecast sources, not observations — but the caller must confirm the DEB inputs are the next-day forecast cycle, not today's |
| `ensemble_sigma_f` | **Yes, if computed from tomorrow's ensemble spread** | Same caveat as `deb_mu_f` |
| `now_local` / `sunset_local` | Informational only, not used in the forecast-only path | N/A |
| `current_high_f` | **No** | Today's running observed high — has no bearing on tomorrow's bracket |
| `current_high_time` | **No** | Same |
| `latest_temp_f` / `latest_temp_time` | **No** | Latest METAR reading — today's observation |
| `obs_bias_offset_f` | **No** | Intraday bias vs. *today's* model hourly temp — not defined for a day with zero observations yet |
| `corrected_mu_f` | **No** | By construction, today's intraday-corrected mean; never valid for tomorrow |
| `intraday_delta_f` | **No** | Same — a today-only nowcast correction |

### Revision (post-review): this is a distinct probability path, not a degenerate WeatherState

The original sketch — build a next-day `WeatherState` with today-anchored
fields left `None` so accidental reads "fail soft" — does not survive contact
with `envelope.py`. `current_high_f` is a **required, non-optional
`WeatherState` field**, and `compute_envelope()`/`true_probability_yes()` are
built directly around it:

- `min_high = state.current_high_f` (`envelope.py:85`)
- hard bracket elimination against `state.current_high_f` (`envelope.py:171`
  region — brackets whose high is below the current running high are
  eliminated)
- `remaining_rise = max_env - current_high_f`-style climb math (`envelope.py`
  `compute_envelope`/climb-fraction logic, ~line 191 region)

`None` crashes these paths outright; a sentinel value (e.g. 0.0) would run
without crashing but silently corrupt the climb/elimination logic with a
fabricated "current high." Neither is acceptable.

**Corrected design: next-day evaluation is its own explicit probability
path**, implemented as a new branch (e.g. `next_day: bool = False` parameter
on `true_probability_yes`, or a sibling function
`next_day_probability_yes(bracket, mu, sigma) -> float`), not a variant call
into the same-day path with a doctored state. Specification:

- **μ**: the forecast mean for tomorrow's date — the stack mean
  (`ensemble_forecast`/`FORECAST_STACK` combination) or, when available, the
  lead-appropriate DEB-weighted mean for tomorrow's forecast cycle. Never
  derived from today's observations.
- **σ**: resolved via the lead-bin machinery below (see Sigma / lead_hours).
- **Bracket integration**: plain Gaussian CDF over `[bracket.low_f,
  bracket.high_f]` against `N(mu, sigma)` (i.e. `p_normal_between`,
  `envelope.py:60`, called directly) — **with neither the observed-high floor
  (`min_high = current_high_f`) nor the `max_env` climb ceiling applied**.
  Both are same-day concepts: the floor encodes "the high can't retroactively
  drop below what's already been observed," and the ceiling encodes "the high
  can't exceed today's current high plus today's remaining possible climb" —
  neither has meaning before today's observation window for that date has
  started. Stated explicitly so this isn't missed: dropping `max_env` removes
  the *upper* truncation as well as the floor, so next-day probability mass is
  shaped differently (wider, more symmetric around μ) than a same-day
  evaluation of an equivalent bracket. This is intentional and correct, not
  an oversight — the whole point of forecast-only evaluation is that there is
  no partial-day information to truncate against yet.
- `time_to_settlement_boost` (`envelope.py:74`) does not apply either — it
  assumes "close observation of a controlled process," which again is a
  same-day property.

Implementation lands in `envelope.py` as an explicit, separately-testable
function/branch — never by constructing a same-day `WeatherState` with holes
poked in it and hoping downstream code degrades safely.

This also has a direct interaction with the EMOS serving layer (#658/#664):
`intraday_delta_f` is explicitly the layer that lets EMOS serving apply the
nowcast on top of its calibrated mean (see `envelope.py:34-37`). Next-day
markets structurally have no nowcast yet, so the next-day path simply never
reads or constructs `intraday_delta_f` — it isn't a matter of leaving a field
`None` on a shared state object, since next-day evaluation doesn't go through
the same-day `WeatherState`/EMOS-serving call path at all.

## Sigma / lead_hours

Next-day markets are evaluated at roughly 12-36h lead (from "just past
today's `MIN_MINUTES_TO_SETTLEMENT`" through to their own close). The #665
`_nearest_lead_hours` bin-selection machinery (`src/model/emos_mode.py:147`)
already picks the nearest fitted `lead_hours` bin to a target lead time, so
no new selection logic is needed — the scanner just needs to pass the
next-day market's actual lead time (hours to its own close) instead of
assuming `lead_hours=24`.

### Fallback sigma when no calibrated bin covers the lead

Where no calibration row is fitted at any nearby bin for a city, same-day
evaluation falls back to the constant `FORECAST_STDDEV_F` (2.0°F). That
constant was tuned against same-day forecast error; at 12-36h lead, real
forecast uncertainty is meaningfully wider, so reusing 2.0°F unchanged for
the fallback next-day case would make next-day `p_yes` too extreme —
overstating confidence and inflating apparent edges exactly where the model
is weakest (a lead range with no fitted calibration at all).

Add a dashboard-tunable multiplier, `NEXT_DAY_SIGMA_MULTIPLIER` (float,
default `1.5`), applied only to the *fallback* path (no calibrated bin
covers the lead): `fallback_sigma = FORECAST_STDDEV_F *
NEXT_DAY_SIGMA_MULTIPLIER`. Wire via `CONFIG_DEFAULTS`/`_CONFIG_META`
(group `"forecast"`) alongside `NEXT_DAY_EVALUATION`. The 1.5x default is a
starting estimate, not a fitted value — shadow data should be used to
sanity-check it before any live-entry decision (see Amendment 3 below). When
a calibrated bin *does* cover the lead (i.e. `_nearest_lead_hours` found a
fitted row), that row's own sigma is used unchanged — the multiplier only
guards the "we have nothing fitted at all" case.

## Segmentation: next-day rows must be discriminable downstream

Next-day evaluations flow into the same `candidates` table and
`snapshots`/`snapshot_archive` rows as same-day evaluations — the same
populations that feed the prob-cap shadow report, saturation baselines, and
future calibration analysis. Mixing 12-36h-lead rows into these tables
without a marker silently shifts every downstream aggregate (mean edge, win
rate, saturation counts) the moment the flag goes on, with no way to filter
them back out.

Add an explicit discriminator column, `is_next_day INTEGER NOT NULL DEFAULT
0`, to:

- `candidates` (`db.insert_candidate()` — new keyword arg, defaults to 0 so
  all existing/same-day call sites are unaffected)
- `snapshots`/`snapshot_archive` write paths (same pattern)

`minutes_to_settlement` technically makes same-day-vs-next-day derivable
after the fact (next-day rows sit at 12-36h vs. same-day's much shorter
window), but analytics code must not have to infer it from a numeric
threshold that could silently drift — an explicit boolean is the honest
contract. This is a small, additive schema change (new column with a
default), not a migration of existing data.

## Rollout: shadow-first

Ship behind a new default-off flag, `NEXT_DAY_EVALUATION`:

- `CONFIG_DEFAULTS["NEXT_DAY_EVALUATION"] = False`
- `_CONFIG_META["NEXT_DAY_EVALUATION"]` entry (dashboard-tunable bool, group
  `"strategy"`), description referencing #687.
- While off: scanner behavior is byte-for-byte identical to today (next-day
  candidates still hit `wrong_date` exactly as now).
- While on: next-day candidates are evaluated and **logged as shadow
  candidates only** (via the existing `log_candidate()` path from #684/#697)
  — no live entry from next-day evaluation until a separate, later decision
  enables it. NO-side protection rules (`SHADOW_STATIONS`, confidence/edge
  gates) apply unchanged to any next-day candidate that would otherwise
  qualify.
- Minimum shadow period: **>= 7 days** of next-day candidate logging before
  any live-entry enablement is even proposed. Enabling live entries from
  next-day evaluation is explicitly out of scope for this issue/PR and
  requires its own follow-up decision.

## Expected effect and verification

- `guardrail_events` zero-eval-tick events (the #686 watchdog) should stop
  firing during the former ~11:45-00:04 UTC blackout window once
  `NEXT_DAY_EVALUATION` is live (shadow or later promoted).
- `snapshot_archive` should show non-empty evaluations resuming across
  11:45-00:04 UTC for stations with an active next-day market.
- No change expected to live trade counts/PnL while the flag is off (default)
  or on-but-shadow-only (candidates logged, no orders placed).

## Explicitly out of scope for this design/PR

- Enabling live entries from next-day evaluation (separate decision after
  >=7 days of shadow data).
- Any change to `MODEL_PROB_CAP`, entry gates, or other live serving
  behavior beyond the `wrong_date` date-equality relaxation described above.
- `STATION_ACTIVE_HOURS` tuning.

## Questions the later live-entry decision must answer (named now, so shadow data can answer them)

Live enablement stays a separate, later decision, but the shadow period
should be designed to produce the evidence that decision will need. Naming
these now so the shadow data collected isn't missing what's required later:

1. **Cross-day exposure policy.** A station can hold an open position on
   today's market while its next-day market simultaneously becomes
   evaluable (and, once live, potentially enterable) — this is a form of
   exposure the current one-market-per-station-per-day model was never
   designed around. Per-station concurrent-exposure limits need an explicit
   rule (e.g. cap combined today+next-day position size, or forbid opening a
   next-day position while a same-station today position is open) before any
   live enablement. Shadow logging should record whether a same-station
   today position was open at next-day-evaluation time, so the later
   decision can quantify how often this would actually occur.
2. **Liquidity/spread check.** Next-day order books in the 12:00-00:00 UTC
   window (i.e. right after a market opens, well before its own settlement
   approaches) may be thin or wide relative to same-day books close to
   settlement. Shadow-recorded prices must be spread-checked (bid/ask width,
   size at touch) before any win-rate or edge conclusion is drawn from them —
   a next-day "edge" computed against an illiquid quote is not comparable to
   a same-day edge against a tight, liquid one.
3. **Next-day-specific entry thresholds.** Same-day `MIN_EDGE_CENTS`/
   `MIN_PRICE_CENTS`/confidence gates were tuned against same-day error
   characteristics; next-day markets likely need their own (probably
   stricter) thresholds rather than inheriting the same-day gates unchanged.
   The shadow analysis should report what next-day-specific thresholds would
   have been needed to match same-day's realized win rate, as direct input
   to that later decision — not just raw next-day CRPS/edge numbers.
