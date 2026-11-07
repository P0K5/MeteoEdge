"""MSS 1-minute observation ingestion adapter for Singapore.

Fetches real-time temperature observations from the Meteorological Service
Singapore (MSS) via the data.gov.sg API and persists them to the observations
table.

MSS API endpoint:
  https://api.data.gov.sg/v1/environment/air-temperature

Response structure:
  {
    "items": [{
      "timestamp": "2026-06-07T10:30:00+08:00",
      "readings": [
        {"station_id": "S24",  "value": 29.4},
        {"station_id": "S108", "value": 28.9}
      ]
    }]
  }

Station priority:
  1. S24  — Changi Airport (preferred; settlement anchor for Polymarket contracts)
  2. S108 — Paya Lebar (fallback when S24 is absent from the response)

Timezone: SGT is UTC+8. All timestamps are converted to UTC before storing.

CRITICAL CONSTRAINT: cadence_min = 1 minute (configurable via MSS_CADENCE_MINUTES).
A CRITICAL log is emitted when no new data has arrived for > 2 minutes.

The api.data.gov.sg domain uses the default 1.0s rate-limit interval already
configured in src.http_client._DOMAIN_INTERVALS (any unlisted domain defaults
to 1.0s). No override is added here to avoid modifying the shared client.

All HTTP calls use fetch() from src.http_client — no new HTTP client.
All DB writes use the Database instance — no raw sqlite3.

Station exclusion (issue #1500): WSSS's supplementary feed here is the
single biggest CRITICAL error class in bot.log once its preferred stations
(S24/S108/S107) drop out of the data.gov.sg response. ``poll()`` re-checks
the EXCLUDED_STATIONS live-config setting on every call (same pattern as
every other live-config flag in src/strategy/scanner.py) and idles --
no HTTP fetch, no staleness check -- while WSSS is excluded. Toggling the
setting from the dashboard Config tab takes effect on the next poll with no
restart, the same no-op-but-revivable shape as the retired amos collector
(issue #740), except re-checked live rather than retired once.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from src.config import CONFIG_DEFAULTS, get_live_config, parse_station_list
from src.data.db import Database
from src.http_client import fetch

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MSS_URL = "https://api.data.gov.sg/v1/environment/air-temperature"

# The MSS collector is Singapore-only (WSSS is the only station it serves).
# Issue #1500: while WSSS is in EXCLUDED_STATIONS, poll() idles instead of
# fetching -- this is what actually silences the CRITICAL staleness noise
# the issue reports (25/day rising to 570+/day), not just the HTTP fetch.
_STATION_METAR = "WSSS"

# Station priority list: S24 (Changi Airport) preferred, S108 (Paya Lebar) and
# S107 (East Coast Parkway) as fallbacks when airport sensors are unavailable.
_STATION_PRIORITY = ["S24", "S108", "S107"]

# SGT = UTC+8
_SGT = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Collector class
# ---------------------------------------------------------------------------

class MssCollector:
    """Poll MSS data.gov.sg API for Singapore 1-minute temperature observations.

    Usage:
        db = Database()
        collector = MssCollector(db)
        collector.poll()   # fetch one reading and insert it

    The cadence is controlled by MSS_CADENCE_MINUTES (default 1).
    A CRITICAL log is emitted when no new data has been received for
    more than 2 × cadence_min minutes (default: 2 minutes).
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cadence_min = int(os.getenv("MSS_CADENCE_MINUTES", "1"))
        self._last_obs_ts: datetime | None = None  # UTC-aware datetime of last stored obs
        self._missing_stations: set[str] = set()  # stations already warned about
        self._was_excluded = False  # tracks transition for log noise (issue #1500)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _is_excluded(self) -> bool:
        """True while WSSS is listed in the live EXCLUDED_STATIONS setting.

        Re-reads live config on every call (issue #1500) so toggling the
        dashboard Config tab setting takes effect on the very next poll --
        no restart needed, same as every other live-config flag read in
        src/strategy/scanner.py.
        """
        try:
            live = get_live_config(self._db)
        except Exception:
            log.warning("[mss] could not read live config for EXCLUDED_STATIONS; assuming not excluded", exc_info=True)
            return False
        excluded = parse_station_list(live.get("EXCLUDED_STATIONS", CONFIG_DEFAULTS["EXCLUDED_STATIONS"]))
        return _STATION_METAR in excluded

    def poll(self) -> bool:
        """Fetch the latest MSS observation and persist it.

        Returns True if a new observation was stored, False otherwise.

        No-ops entirely (no HTTP fetch, no staleness check/CRITICAL) while
        WSSS is excluded via EXCLUDED_STATIONS (issue #1500) -- this is the
        actual fix for the CRITICAL log noise the issue reports, since that
        noise comes from _check_staleness(), not from the fetch itself.
        """
        if self._is_excluded():
            if not self._was_excluded:
                log.info("[mss] WSSS excluded via EXCLUDED_STATIONS -- idling (issue #1500)")
                self._was_excluded = True
            return False
        if self._was_excluded:
            log.info("[mss] WSSS no longer excluded -- resuming polling")
            self._was_excluded = False

        reading = self._fetch_mss()

        if reading is None:
            self._check_staleness()
            return False

        ts_utc, temp_c, raw, station_id = reading
        temp_f = temp_c * 9 / 5 + 32

        self._db.insert_observation(
            ts=ts_utc.isoformat(),
            station="Singapore",
            temp_f=temp_f,
            temp_native=temp_c,
            unit="C",
            source="mss",
            cadence_min=self._cadence_min,
            is_official=1,
            raw_json=json.dumps(raw),
        )

        self._last_obs_ts = ts_utc
        log.debug("[mss] stored %.1f°C from %s at %s", temp_c, station_id, ts_utc.isoformat())
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_mss(self) -> "tuple[datetime, float, dict, str] | None":
        """Fetch and parse the MSS data.gov.sg API.

        Tries stations in priority order (S24 → S108).
        Returns (ts_utc, temp_c, raw_dict, station_id) or None if no usable reading.
        Logs a warning for each unavailable station and tries the next.
        """
        try:
            r = fetch(_MSS_URL)
            data = r.json()
        except Exception as exc:
            log.error("[mss] fetch error: %s", exc)
            return None

        try:
            items = data.get("items", [])
            if not items:
                log.warning("[mss] empty items list in response")
                return None

            item = items[0]  # MSS returns the most recent item first (or only item)

            # Parse the timestamp; it includes SGT offset (+08:00)
            ts_str = item.get("timestamp", "")
            try:
                ts_dt = datetime.fromisoformat(ts_str)
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=_SGT)
                ts_utc = ts_dt.astimezone(timezone.utc)
            except (ValueError, AttributeError) as exc:
                log.warning("[mss] could not parse timestamp '%s': %s", ts_str, exc)
                ts_utc = datetime.now(timezone.utc)

            # Build a lookup of station_id → value from readings
            readings = {r["station_id"]: r["value"] for r in item.get("readings", [])}

            # Try stations in priority order. Warn only when a station's
            # availability changes, not on every poll (avoids 1/min log spam
            # while a preferred sensor is offline).
            for station_id in _STATION_PRIORITY:
                value = readings.get(station_id)
                if value is not None:
                    if station_id in self._missing_stations:
                        self._missing_stations.discard(station_id)
                        log.info("[mss] station %s back in readings", station_id)
                    temp_c = float(value)
                    raw = {
                        "timestamp": ts_str,
                        "station_id": station_id,
                        "value": value,
                        "all_readings": item.get("readings", []),
                    }
                    return ts_utc, temp_c, raw, station_id
                elif station_id not in self._missing_stations:
                    self._missing_stations.add(station_id)
                    log.warning("[mss] station %s not in readings — trying next", station_id)

            log.error("[mss] none of the preferred stations (%s) found in readings", _STATION_PRIORITY)
            return None

        except Exception as exc:
            log.error("[mss] parse error: %s", exc)
            return None

    def _check_staleness(self) -> None:
        """Emit CRITICAL if no data for more than 2 × cadence_min minutes."""
        if self._last_obs_ts is None:
            return
        elapsed_min = (datetime.now(timezone.utc) - self._last_obs_ts).total_seconds() / 60
        threshold = 2 * self._cadence_min
        if elapsed_min > threshold:
            log.critical(
                "[mss] CRITICAL: no new data for %.0f minutes (threshold: %d min)",
                elapsed_min,
                threshold,
            )

    def run_loop(self) -> None:
        """Run the polling loop indefinitely, sleeping cadence_min between polls."""
        log.info("[mss] starting poll loop (cadence=%d min)", self._cadence_min)
        while True:
            try:
                self.poll()
            except Exception as exc:
                log.error("[mss] unexpected error in poll loop: %s", exc)
            time.sleep(self._cadence_min * 60)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = Database()
    MssCollector(db).run_loop()
