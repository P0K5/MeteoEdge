"""JMA AMeDAS 10-minute observation adapter for Tokyo Haneda.

Fetches real-time temperature from the JMA AMeDAS public JSON feed and
persists it to the observations table.

JMA AMeDAS endpoint:
  https://www.jma.go.jp/bosai/amedas/data/point/{station_code}/{YYYYMMDD_HH}.json

Response structure (one measurement per 10-minute slot within the hour):
  {
    "HHMMss": {
      "temp": [22.4, 0],      # [value, quality_flag]  0 = good
      "wind": [3.2, 0],
      "windDirection": [5, 0]
    },
    ...
  }

Station: 44132 — Tokyo/Haneda amedas sensor.

Fallback: If the JMA endpoint is inaccessible (404, network error), the adapter
falls back to the Open-Meteo hourly API for Haneda coordinates
(lat=35.5494, lon=139.7798). This provides modelled data at ~60-min effective
cadence and is clearly flagged in a comment below.

All HTTP calls use fetch() from src.http_client — no new HTTP client.
All DB writes use the Database instance — no raw sqlite3.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from src.data.db import Database
from src.http_client import fetch

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JMA_STATION = os.getenv("JMA_STATION_CODE", "44132")  # Tokyo Haneda
_JST = timezone(timedelta(hours=9))  # Japan Standard Time = UTC+9

_JMA_URL_TEMPLATE = (
    "https://www.jma.go.jp/bosai/amedas/data/point/{station}/{date}{hour}.json"
)

# Open-Meteo fallback: Haneda airport coordinates
_OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=35.5494&longitude=139.7798"
    "&hourly=temperature_2m&temperature_unit=celsius&timezone=Asia%2FTokyo"
)


# ---------------------------------------------------------------------------
# Collector class
# ---------------------------------------------------------------------------

class JmaAmedasCollector:
    """Poll JMA AMeDAS for Tokyo Haneda 10-minute temperature observations.

    Usage:
        db = Database()
        collector = JmaAmedasCollector(db)
        collector.poll()   # fetch latest reading and insert it

    The cadence is controlled by JMA_CADENCE_MINUTES (default 10).
    A CRITICAL log is emitted when no new data has been received for
    more than 2 × cadence_min minutes.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cadence_min = int(os.getenv("JMA_CADENCE_MINUTES", "10"))
        self._last_obs_ts: datetime | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll(self) -> bool:
        """Fetch and persist the latest JMA AMeDAS reading for Tokyo Haneda.

        Returns True if a row was inserted, False otherwise.
        """
        reading = self._fetch_jma()
        is_official = 1
        if reading is None:
            # FALLBACK: Open-Meteo hourly modelled data (not official AMeDAS).
            reading = self._fetch_open_meteo()
            is_official = 0

        if reading is None:
            self._check_staleness()
            return False

        ts_utc, temp_c, raw = reading
        temp_f = temp_c * 9 / 5 + 32

        self._db.insert_observation(
            ts=ts_utc.isoformat(),
            station="Tokyo",
            temp_f=temp_f,
            temp_native=temp_c,
            unit="C",
            source="jma_ameidas",
            cadence_min=self._cadence_min,
            is_official=is_official,
            raw_json=json.dumps(raw),
        )

        self._last_obs_ts = ts_utc
        log.debug("[jma] stored %.1f°C at %s", temp_c, ts_utc.isoformat())
        return True

    # ------------------------------------------------------------------
    # JMA AMeDAS fetch
    # ------------------------------------------------------------------

    def _fetch_jma(self) -> "tuple[datetime, float, dict] | None":
        """Fetch the most recent 10-minute reading from the JMA AMeDAS API.

        JMA serves one JSON file per hour, keyed by 6-digit time strings (HHMMss).
        We request the current JST hour; in the first minutes of an hour the
        file may not be published yet (404), so we retry with the previous
        hour before giving up.

        Returns (ts_utc, temp_c, raw_dict) or None on error.
        """
        now_jst = datetime.now(_JST)
        reading = self._fetch_jma_hour(now_jst)
        if reading is None:
            reading = self._fetch_jma_hour(now_jst - timedelta(hours=1))
        return reading

    def _fetch_jma_hour(self, base_jst: datetime) -> "tuple[datetime, float, dict] | None":
        """Fetch and parse the AMeDAS hourly file for *base_jst*'s date and hour."""
        url = _JMA_URL_TEMPLATE.format(
            station=_JMA_STATION,
            date=base_jst.strftime("%Y%m%d"),
            hour=base_jst.strftime("%H"),
        )

        try:
            r = fetch(url)
        except Exception as exc:
            log.error("[jma] fetch error: %s", exc)
            return None

        if r.status_code == 404:
            log.warning(
                "[jma] AMeDAS file not yet published for %s hour %s (station %s)",
                base_jst.strftime("%Y%m%d"), base_jst.strftime("%H"), _JMA_STATION,
            )
            return None
        if r.status_code != 200:
            log.error("[jma] unexpected HTTP %d for %s", r.status_code, url)
            return None

        try:
            data = r.json()
        except Exception as exc:
            log.error("[jma] JSON parse error: %s", exc)
            return None

        # Data is a dict keyed by time strings like "090000", "091000", etc.
        # Iterate in sorted order to find the most recent good reading.
        best_ts: datetime | None = None
        best_temp: float | None = None
        best_raw: dict | None = None

        for time_key in sorted(data.keys()):
            slot = data[time_key]
            temp_entry = slot.get("temp")
            if not isinstance(temp_entry, list) or len(temp_entry) < 2:
                continue
            value, quality = temp_entry[0], temp_entry[1]
            if quality != 0 or value is None:
                continue
            try:
                hour = int(time_key[:2])
                minute = int(time_key[2:4])
                obs_jst = base_jst.replace(hour=hour, minute=minute, second=0, microsecond=0)
                obs_utc = obs_jst.astimezone(timezone.utc)
            except (ValueError, OverflowError):
                continue
            best_ts = obs_utc
            best_temp = float(value)
            best_raw = {"time_key": time_key, "slot": slot}

        if best_ts is None or best_temp is None:
            log.warning("[jma] no valid temperature readings in hour data")
            return None

        return best_ts, best_temp, best_raw

    # ------------------------------------------------------------------
    # Open-Meteo fallback fetch
    # ------------------------------------------------------------------

    def _fetch_open_meteo(self) -> "tuple[datetime, float, dict] | None":
        """FALLBACK: Fetch current temperature from Open-Meteo for Haneda.

        Used when the JMA AMeDAS endpoint is unreachable or returns errors.
        Open-Meteo provides hourly modelled data — NOT official AMeDAS readings.
        Effective cadence in fallback mode is ~60 minutes.

        Returns (ts_utc, temp_c, raw_dict) or None on failure.
        """
        try:
            r = fetch(_OPEN_METEO_URL)
            data = r.json()
        except Exception as exc:
            log.error("[jma] Open-Meteo fallback failed: %s", exc)
            return None

        try:
            times = data["hourly"]["time"]
            temps = data["hourly"]["temperature_2m"]
            if not times or not temps:
                return None
            now_utc = datetime.now(timezone.utc)
            best_ts: datetime | None = None
            best_temp: float | None = None
            for t_str, t_val in zip(times, temps):
                try:
                    t_dt = datetime.fromisoformat(t_str)
                    if t_dt.tzinfo is None:
                        t_dt = t_dt.replace(tzinfo=_JST)
                    t_utc = t_dt.astimezone(timezone.utc)
                except ValueError:
                    continue
                if t_utc <= now_utc and t_val is not None:
                    best_ts = t_utc
                    best_temp = float(t_val)
            if best_ts is None or best_temp is None:
                return None
            return best_ts, best_temp, {"source_fallback": "open-meteo", "station": "Tokyo"}
        except Exception as exc:
            log.error("[jma] Open-Meteo parse error: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Staleness check
    # ------------------------------------------------------------------

    def _check_staleness(self) -> None:
        """Emit CRITICAL if no data for more than 2 × cadence_min minutes."""
        if self._last_obs_ts is None:
            return
        elapsed_min = (datetime.now(timezone.utc) - self._last_obs_ts).total_seconds() / 60
        threshold = 2 * self._cadence_min
        if elapsed_min > threshold:
            log.critical(
                "[jma] CRITICAL: no new data for %.0f minutes (threshold: %d min)",
                elapsed_min,
                threshold,
            )

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run_loop(self) -> None:
        """Run the polling loop indefinitely."""
        log.info("[jma] starting poll loop (cadence=%d min)", self._cadence_min)
        while True:
            try:
                self.poll()
            except Exception as exc:
                log.error("[jma] unexpected error in poll loop: %s", exc)
            time.sleep(self._cadence_min * 60)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = Database()
    JmaAmedasCollector(db).run_loop()
