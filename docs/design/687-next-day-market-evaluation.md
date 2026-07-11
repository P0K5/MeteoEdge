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

Practically: `compute_envelope()` and `true_probability_yes()` must not be
called with a `WeatherState` carrying `current_high_f`/`latest_temp_f`-derived
signal for next-day brackets. The cleanest implementation is a distinct
next-day `WeatherState` construction path (or a `for_next_day=True` flag on
the existing builder) that only ever populates `forecast_high_f`,
`secondary_forecast_f`, `deb_mu_f`, and `ensemble_sigma_f` from tomorrow's
forecast cycle, leaving all today-anchored fields `None`/unset so any
accidental read is `None` rather than stale today-data.

This also has a direct interaction with the EMOS serving layer (#658/#664):
`intraday_delta_f` is explicitly the layer that lets EMOS serving apply the
nowcast on top of its calibrated mean (see `envelope.py:34-37`). Next-day
markets structurally have no nowcast yet, so EMOS serving for a next-day
candidate must resolve as if `intraday_delta_f` were never set — no special
casing needed if the field is simply left `None`, but the EMOS serving path
must not fall back to reading `corrected_mu_f`/`current_high_f` from a stale
today `WeatherState` for the same station.

## Sigma / lead_hours

Next-day markets are evaluated at roughly 12-36h lead (from "just past
today's `MIN_MINUTES_TO_SETTLEMENT`" through to their own close). The #665
`_nearest_lead_hours` bin-selection machinery (`src/model/emos_mode.py:147`)
already picks the nearest fitted `lead_hours` bin to a target lead time, so
no new selection logic is needed — the scanner just needs to pass the
next-day market's actual lead time (hours to its own close) instead of
assuming `lead_hours=24`. Where no calibration row is fitted at any nearby
bin for a city, the existing `FORECAST_STDDEV_F` (2.0°F) fallback applies
unchanged — same fallback as any other lead-time gap today.

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
