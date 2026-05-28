# MeteoEdge — Polymarket Shadow Loop

Paper-trading shadow process for Polymarket daily-temperature markets. Runs **in parallel** to the live spike, **never executes orders**, and logs predicted probability + market prices per (city × bracket × poll) so that:

1. **Live cohort comparison.** US-city snapshots can be diffed against the live spike's trade log to detect model drift or execution differences.
2. **New-city data gathering.** 39 non-US cities (Europe / Asia / Latam / MENA / Africa / Oceania) are tracked without risking capital. After 3–4 weeks of settled snapshots you'll have per-region hit-rate data to decide which cohorts deserve real money.

## What it covers

- **50 cities** total — 11 US (matching live) + 39 non-US
- **One omission:** Hong Kong (resolves against `weather.gov.hk`, not an ICAO METAR site — would need a custom scraper)
- **°F natively for US**, **°C natively for non-US** — model is unit-aware (`envelope.py`)
- **Forecasts:** NWS for US (matches live), Open-Meteo for non-US (global, free, no auth)
- **Daily lows skipped.** Polymarket has "lowest temperature in <city>" markets too, but the envelope is built around the daily high. Mirroring it for lows is a separate feature.

## What it deliberately does NOT have

- No Polymarket trading client. No API keys. No CLOB order calls.
- No live-CLOB orderbook enrichment (Gamma `outcomePrices` only — keeps polls fast across 50 cities).
- No settlement reconciler yet (offline batch job to come).

## Outputs

```
logs_shadow/
  snapshots.jsonl    one row per (city, bracket, poll)
  candidates.csv     subset where edge >= MIN_EDGE_CENTS and confidence threshold met
```

Each snapshot row carries `region` and `unit` so cohort analysis is trivial in jq / pandas.

## Local development

```bash
cd archive/polymarket-shadow
python -m venv .venv
.venv\Scripts\activate          # PowerShell
pip install -r requirements.txt
python shadow.py                # prints to stdout, writes logs_shadow/
```

The first poll takes ~2–3 minutes (sequential METAR + forecast per city). Subsequent polls are similar — there's no caching yet because Polymarket's daily markets rotate at midnight UTC and we want fresh data each loop.

## Production deployment (Ubuntu)

Layout on the host — two **sibling** repo checkouts, fully independent:

```
/home/p0k5/MeteoEdge/                            ← live trading (existing checkout, untouched)
  └── src/scripts/run.py                         ← live entry point

/home/p0k5/MeteoEdge-Shadow/                     ← NEW: separate clone of the same repo
  └── archive/polymarket-shadow/                 ← shadow entry lives here
      ├── shadow.py
      ├── config.py
      ├── envelope.py
      ├── forecast.py
      ├── polymarket_client.py
      ├── logs_shadow/                           ← created at first poll
      └── .venv/  ← NO, see below — venv goes at the checkout root
  └── .venv/                                     ← shared venv for the shadow checkout
```

The shadow lives inside `archive/polymarket-shadow/` because that's where it sits in the repo — but it has its own venv at the **checkout root** (`/home/p0k5/MeteoEdge-Shadow/.venv/`) and writes logs alongside its source. The live checkout has its own venv at its own root. Nothing is shared between the two.

### One-time setup on the host

```bash
# 1. Clone a second copy of the repo as a sibling to the live one
cd /home/p0k5
git clone <REPO_URL> MeteoEdge-Shadow

# 2. Create the venv and install shadow deps
cd /home/p0k5/MeteoEdge-Shadow
python3 -m venv .venv
.venv/bin/pip install -r archive/polymarket-shadow/requirements.txt

# 3. Sanity check — one manual poll, Ctrl-C after the first scan completes
cd archive/polymarket-shadow
mkdir -p logs_shadow
../../.venv/bin/python shadow.py

# 4. Install the systemd unit
sudo cp systemd/meteoedge-shadow.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meteoedge-shadow.service
sudo systemctl status meteoedge-shadow.service

# 5. Tail the logs to confirm it's polling
sudo journalctl -u meteoedge-shadow.service -f
# or:
tail -f /home/p0k5/MeteoEdge-Shadow/archive/polymarket-shadow/logs_shadow/shadow.log
```

### Subsequent updates

```bash
# On the host — pull and restart
cd /home/p0k5/MeteoEdge-Shadow
git pull
sudo systemctl restart meteoedge-shadow.service

# The live checkout is untouched — pull it separately when you want:
cd /home/p0k5/MeteoEdge
git pull
sudo systemctl restart meteoedge.service
```

Restarting the shadow unit has **zero effect** on the live trading bot — they share no files, no venv, no process. You can also `git checkout <branch>` independently in each tree if you want to test something in shadow without affecting live.

## What to watch in the first week

- **METAR availability per city** — `[icao] no METAR data` is the most common failure. Cities with persistent gaps (likely candidates: Lucknow, Karachi, some China stations during data delays) should be flagged and possibly dropped.
- **Bracket parsing for °C labels** — Polymarket may use slightly different label formats for non-US markets. Watch `n_brackets` vs `n_temp` in the scan summary; if a city's brackets aren't parsing, eyeball one of its raw `groupItemTitle` values and extend the regex.
- **Open-Meteo accuracy** — compare its predicted highs against realized highs on the same day. Open-Meteo's "today's max" is generally good but can lag in fast-moving fronts.

## Graduating a city to live

When a non-US city shows a stable edge over ≥500 settled brackets:

1. Copy its tuple from `polymarket-shadow/config.py:STATIONS` into `polymarket-spike/config.py:STATIONS`.
2. Add its IANA timezone to the spike's `STATION_TZ` dict (already in the shadow's station tuple).
3. Confirm the live spike's bracket parser handles °C (it currently does not — needs the unit-aware changes from the shadow's `shadow.py`).
4. Deploy to live spike, watch for 1 week before increasing position size.

## Notes on the model

`envelope.py` is the same physical-envelope + Gaussian-around-forecast model as the live spike, but generalized to either unit. **The climb-rate lookup table (`DEFAULT_CLIMB_LOOKUP_F` in `config.py`) is still US-tuned and converted on the fly for °C stations.** This is intentional — the whole point of the shadow loop is to gather data to fit climate-zone-specific climb tables. Expect non-US predictions to be systematically miscalibrated until that recalibration happens.
