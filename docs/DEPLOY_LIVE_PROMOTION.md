# Deploying per-wallet live copy-trading promotion

End-to-end runbook for shipping the per-wallet live promotion epic and taking a
single wallet live, while every other followed wallet keeps paper-trading.

Covers issues #1253 (per-wallet gate), #1255 (go/no-go readiness report),
#1259 (live stake independent of paper stake) and #1254 (dashboard control).

> **Read this first.** There is one step — allocating live capital, step 4 — that
> is easy to skip and fails **silently**. Without it you can complete the deploy,
> arm the global switch, promote a wallet, see the dashboard report **LIVE**, and
> place **zero orders**, with the reason visible only inside a database column.
> Nothing else in this sequence behaves that way.

---

## 0. What this epic changed

Before: `COPY_LIVE_TRADING_ENABLED` was a single global switch. With it on, **every**
`active` followed wallet placed real orders. There was no way to take one wallet
live without taking all of them live, and pausing a wallet stopped its paper
trading too — which meant partial go-live was impossible without damaging the
paper study.

After:

| Capability | Where |
|---|---|
| Per-wallet opt-in flag `live_enabled` (default `0`) | `copy_wallets_followed` |
| Gate: global switch **AND** per-wallet flag | `_handle_live_order`, `src/scripts/copy_signal_loop.py` |
| Live stake independent of the paper stake | `live_stake_per_trade`, resolved as `COALESCE(live_stake_per_trade, stake_per_trade)` |
| Pre-registered go/no-go evidence gate | `src/scripts/copy_live_readiness.py` |
| Operator control | `POST /api/copy-trading/wallets/{address}/live`, `PATCH …/live-stake`, the Followed Wallets view, and `copy_wallet_promotion.py` |

Two invariants worth knowing before you touch anything:

- **Paper execution happens first, unconditionally.** The live layer sits on top of
  an already-paper-executed signal. Promoting a wallet cannot affect the paper
  study, and a paper-only wallet writes no `copy_live_positions` row at all.
- **The global switch remains the master kill switch.** It is AND-ed with the
  per-wallet flag, so a promoted wallet still places nothing while the global
  switch is off.

---

## 1. Pre-deploy state check

Run these **before** pulling, so you can tell afterwards what actually changed.

```bash
cd /home/p0k5/MeteoEdge

# Current live posture and exposure — expect COPY_LIVE_TRADING_ENABLED=false
sqlite3 data/meteoedge.db \
  "SELECT key, value FROM bot_config WHERE key LIKE 'COPY_LIVE%' ORDER BY key;"

# How many live positions exist today (expect 0 on a first deploy)
sqlite3 data/meteoedge.db "SELECT COUNT(*) FROM copy_live_positions;"

# Followed wallets and their paper stakes
sqlite3 data/meteoedge.db \
  "SELECT address, status, stake_per_trade FROM copy_wallets_followed ORDER BY added_at;"
```

Note the current `COPY_LIVE_CAPITAL_USD` from the environment file — it is **not**
in `bot_config`, see step 4:

```bash
grep -n 'COPY_LIVE' /home/p0k5/MeteoEdge/.env || echo "(no COPY_LIVE_* keys set)"
```

---

## 2. Deploy the code

The schema change is additive and applies automatically on service start. There is
no manual migration step.

```bash
cd /home/p0k5/MeteoEdge
git pull

sudo systemctl restart meteoedge.service              # bot + embedded dashboard/API
sudo systemctl restart meteoedge-copy-signals.service # the live-order gate
```

> **`meteoedge.service` is the dashboard.** There is no `meteoedge-dashboard.service`.
> The dashboard API is embedded in the bot process:
> `meteoedge.service` → `ExecStart … -m src.scripts.run --live` → `start_dashboard`
> → `src.dashboard.api`, which serves `src/dashboard/static/index.html`.
> Restarting the wrong unit is the most likely way to conclude "the deploy didn't work".

Both units read `EnvironmentFile=/home/p0k5/MeteoEdge/.env`, which matters in step 4.

### Verify the code is live

```bash
# Both columns must exist
sqlite3 data/meteoedge.db "PRAGMA table_info(copy_wallets_followed);" \
  | grep -E 'live_enabled|live_stake_per_trade'

# Every pre-existing wallet must read live_enabled=0 and live_stake_per_trade=NULL.
# If any row shows live_enabled=1 here, STOP — nothing should be opted in by a migration.
sqlite3 data/meteoedge.db \
  "SELECT address, live_enabled, COALESCE(live_stake_per_trade,'NULL') FROM copy_wallets_followed;"

# New API fields present
curl -s localhost:8000/api/copy-trading/followed-wallets \
  | python3 -m json.tool | grep -E 'live_cap_usd|live_opted_in_count|live_stake_is_override'
```

Then hard-reload the dashboard in the browser (a static-asset change can be hidden
by a stale cache) and confirm:

- each Followed Wallets row shows a **Go live** button, between *Edit stake* and *Unfollow*
- the summary strip shows a **# opted into live** pill

**Failure looks like:** the columns missing (service did not restart, or the pull
did not land), or the button absent while the API fields are present (browser cache
— hard-reload again before investigating anything else).

---

## 3. Confirm the settlement path is running

Live positions are settled by a timer, not by the signal loop. Verify it is active
*before* you create any live exposure:

```bash
systemctl is-active meteoedge-copy-live-settle.timer   # runs every 15 min (*:0/15)
systemctl is-active meteoedge-copy-health.timer        # daily 03:45 UTC
```

---

## 4. Allocate live capital — **the step that actually enables orders**

`COPY_LIVE_CAPITAL_USD` is **env-only**. It is a module-level constant read at
import time, deliberately *not* live-editable, and it defaults to `0.0`.

`live_startup_sanity_check` refuses to start live whenever
`COPY_LIVE_MAX_TOTAL_EXPOSURE_USD > COPY_LIVE_CAPITAL_USD`. With the shipped
defaults that is `250.0 > 0.0`, so **every live order is rejected** until you fix it.

Know which knob is which — confusing them is what makes this fail quietly:

| Key | Where it lives | Change requires |
|---|---|---|
| `COPY_LIVE_CAPITAL_USD` | `.env` file, env-only | **service restart** |
| `COPY_LIVE_TRADING_ENABLED` | `bot_config` table | nothing (live-read) |
| `COPY_LIVE_MAX_TOTAL_EXPOSURE_USD` | `bot_config` table | nothing (live-read) |
| `COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD` | `bot_config` table | nothing (live-read) |
| `COPY_LIVE_DAILY_LOSS_LIMIT_USD` | `bot_config` table | nothing (live-read) |

Pick a real capital figure, then set the caps at or below it. A deliberately small
first allocation:

```bash
# 1. Env: the capital pool (restart required)
echo 'COPY_LIVE_CAPITAL_USD=50.0' >> /home/p0k5/MeteoEdge/.env
sudo systemctl restart meteoedge-copy-signals.service
sudo systemctl restart meteoedge.service

# 2. DB-backed caps, at or under the pool (no restart needed)
curl -s -X PATCH localhost:8000/api/config \
  -H 'Content-Type: application/json' \
  -d '{"key":"COPY_LIVE_MAX_TOTAL_EXPOSURE_USD","value":50.0}'

curl -s -X PATCH localhost:8000/api/config \
  -H 'Content-Type: application/json' \
  -d '{"key":"COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD","value":25.0}'
```

`PATCH /api/config` applies a cross-field capital-ceiling check (#1215). If it
refuses a cap as exceeding its capital pool, that is the guard working — raise the
pool in `.env` and restart, or lower the cap.

### Verify

```bash
grep COPY_LIVE_CAPITAL_USD /home/p0k5/MeteoEdge/.env
sqlite3 data/meteoedge.db \
  "SELECT key, value FROM bot_config WHERE key LIKE 'COPY_LIVE_MAX%';"
grep -i 'sanity\|refusing to start' logs/copy_signals.log | tail -5
```

**Failure looks like:** `logs/copy_signals.log` carrying
`COPY_LIVE_MAX_TOTAL_EXPOSURE_USD ($250.00) exceeds COPY_LIVE_CAPITAL_USD ($0.00)
-- refusing to start`. If you see that, the `.env` edit did not reach the process —
you almost certainly changed `.env` without restarting `meteoedge-copy-signals`.

---

## 5. Decide which wallet qualifies

Do not pick by eye. The readiness report is a **pre-registered** gate: its four
constants were fixed by issue #1255 *before* the numbers were looked at.

```bash
cd /home/p0k5/MeteoEdge
.venv/bin/python -m src.scripts.copy_live_readiness          # human-readable
.venv/bin/python -m src.scripts.copy_live_readiness --json   # machine-readable
```

It is read-only and writes nothing. A wallet PASSES only if **all four** hold:

1. at least **150** deduped decisions (fills on one `(market, outcome_index)` count once)
2. all-time per-decision 95% CI lower bound **> 0**
3. last-7-day mean per decision **> 0**
4. best single decision **≤ 25%** of total P&L

> **Expect every wallet to FAIL right now**, on the decision-count condition. That
> is the gate working, not a defect. The reason the bar is clustered-by-market is
> that fills on the same market are correlated: `0x9243` looked significant
> per-trade (CI [+0.73, +4.91], n=36) but clustered came out [−0.49, +7.04], with
> one market contributing +$52 of +$102.
>
> **Do not edit the four constants to obtain a pass.** They are module constants
> commented with their issue precisely so that loosening the bar requires a new
> issue and a review, rather than a quiet edit. If you change them, the gate stops
> being evidence and becomes a rubber stamp.

---

## 6. Promote one wallet

Two independent decisions: *whether* the wallet goes live, and *at what size*.

### Set the live stake first (optional but recommended)

`live_stake_per_trade` is `NULL` by default, meaning "inherit the paper stake". Set
it if you want to go live smaller than the wallet paper-trades — which keeps the
paper study's sizing, and therefore its comparability, intact.

```bash
# Live at $2/trade while paper continues at its own stake
.venv/bin/python -m src.scripts.copy_wallet_promotion --live-stake 0xADDRESS 2.0

# Revert to inheriting the paper stake
.venv/bin/python -m src.scripts.copy_wallet_promotion --live-stake 0xADDRESS none
```

> **Keep the live stake at or below `COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD`.** A
> live stake above the per-wallet cap makes the wallet **permanently untradeable
> while still displaying as LIVE** — see Known traps (#1266). The CLI does not
> currently refuse it.

### Opt the wallet in

```bash
.venv/bin/python -m src.scripts.copy_wallet_promotion --live-on  0xADDRESS
.venv/bin/python -m src.scripts.copy_wallet_promotion --live-off 0xADDRESS
```

Or from the dashboard: **Followed Wallets → Go live**, which prompts for the live
stake (pre-filled with the wallet's *current resolved live stake*) and then asks for
confirmation naming the wallet, that stake and the per-wallet cap. Reverting to
paper needs no confirmation.

Enabling live on a **paused** wallet is refused (409 from the API, non-zero exit
from the CLI).

### Arm the global switch

Per-wallet opt-in does nothing until the master switch is on:

```bash
curl -s -X PATCH localhost:8000/api/config \
  -H 'Content-Type: application/json' \
  -d '{"key":"COPY_LIVE_TRADING_ENABLED","value":true}'
```

### Verify

```bash
sqlite3 data/meteoedge.db \
  "SELECT address, status, live_enabled, stake_per_trade,
          COALESCE(live_stake_per_trade,'inherits') AS live_stake
     FROM copy_wallets_followed ORDER BY live_enabled DESC;"
```

Exactly the wallets you promoted should show `live_enabled=1`. In the dashboard the
promoted wallet's badge reads **LIVE**; everything else reads **PAPER**.

The badge has four states, and they mean different things:

| Badge | Meaning |
|---|---|
| **LIVE** | opted in, global switch on, capacity available |
| **LIVE (switch off)** | opted in, but the global switch is off — placing nothing |
| **LIVE (cap reached)** | opted in and armed, but this wallet's live exposure cap is currently consumed by open positions |
| **PAPER** | not opted in |

---

## 7. Confirm a real order

Live orders only occur when a followed wallet actually trades, so this may take a
while. Watch, don't poll aggressively:

```bash
tail -f logs/copy_signals.log | grep -i 'live'
```

```bash
# Every live attempt, executed or rejected, newest first
sqlite3 -header -column data/meteoedge.db \
  "SELECT id, address, status, stake_usd, COALESCE(filled_stake_usd,'-') AS filled,
          COALESCE(rejected_reason,'-') AS reason, entry_ts
     FROM copy_live_positions ORDER BY id DESC LIMIT 10;"
```

Interpreting `rejected_reason`:

| Reason | Meaning |
|---|---|
| *(no rows at all)* | no promoted wallet has traded yet — or nothing is opted in |
| `live_wallet_exposure_limit` | this wallet's cap would be breached. If it fires on the **first** attempt with no open positions, your live stake exceeds the per-wallet cap (#1266) |
| `live_total_exposure_limit` | the portfolio-wide cap would be breached |
| `live_missing_token_id` | the copied trade carried no token id; paper-executed only, never placeable |
| a sanity-check message | capital/cap misconfiguration — go back to step 4 |

A first `filled` or `partial` row means the whole path works end to end.

---

## 8. Rollback and the kill switch

**To stop all live trading immediately:**

```bash
.venv/bin/python -m src.scripts.halt_live_copy_trading --db data/meteoedge.db
# add --dry-run first to see what it would change
```

This flips `COPY_LIVE_TRADING_ENABLED` to false and touches nothing else. Paper
trading continues.

> **Per-wallet opt-ins persist through a halt.** The halt script only moves the
> global switch, so **re-arming returns every previously-promoted wallet to live at
> once**. If you halted because one wallet misbehaved, `--live-off` that wallet
> before re-arming, or you will silently restore it.

**Open live positions are unaffected by demotion.** `copy_live_settle.py` settles
from `copy_live_positions` rows and has no coupling to `copy_wallets_followed`, so
turning a wallet's flag off never strands its open positions — they settle normally.

**To roll back the code**, redeploy the previous commit and restart both services.
The added columns are additive and harmless to leave in place; older code ignores
them. Do **not** drop them while any `copy_live_positions` rows reference the
wallets.

---

## Known traps

### #1264 — pausing does not clear a wallet's live opt-in

A promoted wallet that is paused keeps `live_enabled=1`, so **resuming it restores
real-money exposure with no confirmation**. This matters most because pausing is
often *automatic*: the daily health job pauses a wallet on negative realized ROI —
exactly the signal saying it should not be spending money. Resume is deliberately
prompt-free because it is normally risk-reducing, which is not true for a promoted
wallet.

Until #1264 is decided: after any pause of a promoted wallet, check `live_enabled`
before resuming.

### #1266 — a live stake above the per-wallet cap silently disables the wallet

Set a live stake above `COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD` and the wallet
reports **LIVE** — eligibility sees zero open exposure and concludes there is
capacity — while every order is rejected as `live_wallet_exposure_limit`. Lowering
the cap beneath an existing live stake does the same to a wallet that was trading
fine, and the cap is editable from the config tab.

Symptom: a wallet that looks LIVE and never fills, with
`live_wallet_exposure_limit` rows against zero open positions.

### The readiness gate will fail everything

See step 5. This is expected, and the constants must not be tuned.

---

## Reference: which unit does what

| Unit | Role | Log |
|---|---|---|
| `meteoedge.service` | trading bot **and** the embedded dashboard/API | `logs/bot.log` |
| `meteoedge-copy-signals.service` | copy-signal detection and the live-order gate | `logs/copy_signals.log` |
| `meteoedge-copy-live-settle.timer` | live settlement and reconciliation, every 15 min | — |
| `meteoedge-copy-settle.timer` | paper settlement | — |
| `meteoedge-copy-health.timer` | wallet health / auto-pause, 03:45 UTC | — |
| `meteoedge-copy-screening.timer` | candidate wallet screening | — |

Both services read `EnvironmentFile=/home/p0k5/MeteoEdge/.env`. The database
defaults to `data/meteoedge.db`, overridable with `DB_PATH`.

## Related documents

- `docs/design/copy-trading-architecture.md` — architecture and the go/no-go gate
- `docs/design/copy-trading-live-views.md` — the Followed Wallets UX contract
- `docs/OPERATIONS.md` — general operations
- `docs/DB_SCHEMA.md` — `copy_wallets_followed`, `copy_live_positions`
