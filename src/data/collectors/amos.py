"""AMOS runway sensor ingestion adapter for Seoul/Busan airports.

Fetches Automated Meteorological Observation System (AMOS) runway-level
temperature readings for:
  - Seoul Incheon (RKSI)  → station="Seoul",  KMA station ID 112
  - Busan Gimhae (RKPK)   → station="Busan",  KMA station ID 159

Data source priority:
  1. KMA ASOS API (https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList)
     Requires KMA_API_KEY env var (serviceKey). When present this is used as the
     primary source.
  2. Open-Meteo (api.open-meteo.com) — FALLBACK when KMA_API_KEY is absent or
     the KMA endpoint returns an error. Open-Meteo provides hourly modelled data;
     cadence is effectively 60 minutes in fallback mode. This is clearly noted
     because it changes the effective cadence and is not an official AMOS reading.

DELIBERATE DESIGN DECISION (issue #694): the KMA ASOS API
(``AsosHourlyInfoService``) is an **archive-only** service — it only serves
data up to the previous day. Every request for the current KST hour returns
HTTP 200 with ``{"response":{"header":{"resultCode":"99", ...}}}`` ("data up
to the previous day only, please check the date range"). This is expected,
permanent behavior of this endpoint, not a transient failure, so
``_fetch_kma()`` detects ``resultCode == "99"`` explicitly and logs it once at
DEBUG (not ERROR) before falling back to Open-Meteo — see ``_fetch_kma()`` for
details.

Why we did NOT switch to a real-time replacement in this change: a prior
investigation comment on #694 proposed the KMA API Hub "AMOS minute data" feed
(``https://apihub.kma.go.kr/api/typ01/url/amos.php``) as the real-time
replacement. That endpoint requires a brand-new ``KMA_APIHUB_KEY`` credential
(a separate KMA API Hub account with mandatory Korean-mobile-number
verification) that does not exist anywhere in this repo/environment, and
outbound HTTPS to both ``apis.data.go.kr`` and ``apihub.kma.go.kr`` is blocked
in this sandbox, so the replacement could not be verified here. Rather than
wire up an unverified integration against an endpoint nobody has credentials
for, this change only hardens the existing (verified, working) fallback path.
The API Hub migration remains the recommended follow-up once a human operator
provisions ``KMA_APIHUB_KEY`` — see #694 for the full setup guide.

CRITICAL CONSTRAINT: Errors in one station (Seoul or Busan) do NOT abort
processing of the other station. Each station is polled independently within
a single poll() call.

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

_KMA_ENDPOINT = (
    "https://apis.data.go.kr/1360000/AsosHourlyInfoService/getWthrDataList"
)

# KMA station IDs for AMOS sensors
_KMA_STATIONS = {
    "Seoul": "112",   # Seoul Incheon (RKSI)
    "Busan": "159",   # Busan Gimhae (RKPK)
}

# Open-Meteo fallback coordinates
# Seoul Incheon: lat=37.4602, lon=126.4407
# Busan Gimhae:  lat=35.1796, lon=129.0756
_OPEN_METEO_COORDS = {
    "Seoul": (37.4602, 126.4407),
    "Busan": (35.1796, 129.0756),
}

_KST = timezone(timedelta(hours=9))  # Korea Standard Time = UTC+9


# ---------------------------------------------------------------------------
# Collector class
# ---------------------------------------------------------------------------

class AmosCollector:
    """Poll KMA ASOS API (or Open-Meteo fallback) for Seoul/Busan temperature.

    Usage:
        db = Database()
        collector = AmosCollector(db)
        collector.poll()   # fetch readings for both stations and insert them

    The cadence is controlled by AMOS_CADENCE_MINUTES (default 10).
    A CRITICAL log is emitted for each station when no new data has been
    received for more than 2 × cadence_min minutes.

    Errors in one station do NOT abort the other.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cadence_min = int(os.getenv("AMOS_CADENCE_MINUTES", "10"))
        self._api_key: str | None = os.getenv("KMA_API_KEY")
        # Per-station last observation timestamp (UTC-aware)
        self._last_obs_ts: dict[str, datetime | None] = {s: None for s in _KMA_STATIONS}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll(self) -> dict[str, bool]:
        """Fetch readings for both Seoul and Busan and persist them.

        Returns a dict mapping station name → True if a reading was stored.
        Errors in one station do NOT affect the other.
        """
        results: dict[str, bool] = {}
        for station in _KMA_STATIONS:
            try:
                results[station] = self._poll_station(station)
            except Exception as exc:
                log.error("[amos] unexpected error for %s: %s", station, exc)
                results[station] = False
        return results

    # ------------------------------------------------------------------
    # Per-station logic
    # ------------------------------------------------------------------

    def _poll_station(self, station: str) -> bool:
        """Fetch and persist one reading for *station*.

        Tries KMA API first (if API key available), then Open-Meteo fallback.
        Returns True if a row was inserted.
        """
        reading = None
        is_official = 1

        if self._api_key:
            reading = self._fetch_kma(station)
            if reading is None:
                # NOTE: this fires on every poll when KMA_API_KEY is set, because
                # AsosHourlyInfoService is archive-only and always declines
                # same-day requests (resultCode=99) -- see _fetch_kma() and the
                # module docstring. That specific, expected case is already
                # logged once at DEBUG inside _fetch_kma(); this is a quiet,
                # generic breadcrumb for the "trying fallback" transition and is
                # intentionally NOT at WARNING/ERROR so it doesn't spam logs.
                log.debug(
                    "[amos] KMA API unavailable for %s, using Open-Meteo fallback", station
                )

        if reading is None:
            # FALLBACK: Open-Meteo provides hourly modelled data (not official AMOS).
            # This fallback is used when KMA_API_KEY is absent or the KMA endpoint fails.
            # Cadence is effectively hourly in this mode.
            reading = self._fetch_open_meteo(station)
            is_official = 0

        if reading is None:
            self._check_staleness(station)
            return False

        ts_utc, temp_c, raw = reading
        temp_f = temp_c * 9 / 5 + 32

        self._db.insert_observation(
            ts=ts_utc.isoformat(),
            station=station,
            temp_f=temp_f,
            temp_native=temp_c,
            unit="C",
            source="amos",
            cadence_min=self._cadence_min,
            is_official=is_official,
            raw_json=json.dumps(raw),
        )

        self._last_obs_ts[station] = ts_utc
        return True

    # ------------------------------------------------------------------
    # KMA API fetch
    # ------------------------------------------------------------------

    def _fetch_kma(self, station: str) -> "tuple[datetime, float, dict] | None":
        """Fetch hourly data from the KMA ASOS API for *station*.

        Returns (ts_utc, temp_c, raw_dict) or None on error.

        KMA API notes:
        - serviceKey must be URL-encoded in query params
        - dataCd=ASOS, dateCd=HR for hourly airport data
        - Response is XML or JSON depending on _type param; we request JSON

        Known, permanent, by-design response (issue #694): this endpoint only
        serves data up to the previous day. A request for the current KST hour
        always returns HTTP 200 with ``resultCode == "99"`` and a Korean
        "previous day only" message instead of an items list. This is detected
        explicitly below and logged once at DEBUG — it is NOT treated as an
        unexpected parse failure (that used to KeyError and log at ERROR on
        every single poll). See the module docstring for why this is the
        deliberate fallback path rather than a bug to "fix" further.
        """
        station_id = _KMA_STATIONS[station]
        now_kst = datetime.now(_KST)
        date_str = now_kst.strftime("%Y%m%d")
        hour_str = now_kst.strftime("%H")

        url = (
            f"{_KMA_ENDPOINT}"
            f"?serviceKey={self._api_key}"
            f"&pageNo=1&numOfRows=10&dataType=JSON"
            f"&dataCd=ASOS&dateCd=HR"
            f"&startDt={date_str}&startHh={hour_str}"
            f"&endDt={date_str}&endHh={hour_str}"
            f"&stnIds={station_id}"
        )
        try:
            r = fetch(url)
            data = r.json()
        except Exception as exc:
            log.error("[amos] KMA fetch error for %s: %s", station, exc)
            return None

        result_code = (
            data.get("response", {}).get("header", {}).get("resultCode")
            if isinstance(data, dict) else None
        )
        if result_code == "99":
            # Expected, permanent behavior of AsosHourlyInfoService — not an error.
            log.debug(
                "[amos] KMA ASOS is historical-only, falling back to Open-Meteo by "
                "design (resultCode=99, station=%s) — see issue #694",
                station,
            )
            return None
        if result_code is not None and result_code != "00":
            # Any other non-success resultCode is unexpected — keep logging it
            # loudly so a genuine new failure mode isn't silently swallowed.
            log.warning(
                "[amos] KMA ASOS returned resultCode=%s for %s: %s",
                result_code, station,
                data.get("response", {}).get("header", {}).get("resultMsg"),
            )
            return None

        try:
            items = data["response"]["body"]["items"]["item"]
            if not items:
                log.warning("[amos] KMA returned empty items for %s", station)
                return None
            item = items[0]
            temp_c = float(item["ta"])  # 'ta' = air temperature in °C

            # KMA timestamps are in KST; convert to UTC
            obs_str = f"{item['tm']}"  # e.g. "2026-06-07 09:00"
            try:
                obs_dt = datetime.strptime(obs_str, "%Y-%m-%d %H:%M").replace(tzinfo=_KST)
            except ValueError:
                obs_dt = now_kst.replace(minute=0, second=0, microsecond=0)
            ts_utc = obs_dt.astimezone(timezone.utc)

            return ts_utc, temp_c, item
        except (KeyError, TypeError, ValueError) as exc:
            log.error("[amos] KMA parse error for %s: %s", station, exc)
            return None

    # ------------------------------------------------------------------
    # Open-Meteo fallback fetch
    # ------------------------------------------------------------------

    def _fetch_open_meteo(self, station: str) -> "tuple[datetime, float, dict] | None":
        """FALLBACK: Fetch current temperature from Open-Meteo for *station*.

        Used when KMA_API_KEY is absent or the KMA endpoint fails.
        Open-Meteo provides hourly modelled data — NOT official AMOS readings.
        Cadence is effectively 60 minutes in this mode.

        Returns (ts_utc, temp_c, raw_dict) or None on failure.
        """
        lat, lon = _OPEN_METEO_COORDS[station]
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m&temperature_unit=celsius&timezone=Asia%2FSeoul"
        )
        try:
            r = fetch(url)
            data = r.json()
        except Exception as exc:
            log.error("[amos] Open-Meteo fallback failed for %s: %s", station, exc)
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
                        t_dt = t_dt.replace(tzinfo=_KST)
                    t_utc = t_dt.astimezone(timezone.utc)
                except ValueError:
                    continue
                if t_utc <= now_utc and t_val is not None:
                    best_ts = t_utc
                    best_temp = float(t_val)
            if best_ts is None or best_temp is None:
                return None
            return best_ts, best_temp, {"source_fallback": "open-meteo", "station": station}
        except Exception as exc:
            log.error("[amos] Open-Meteo parse error for %s: %s", station, exc)
            return None

    # ------------------------------------------------------------------
    # Staleness check
    # ------------------------------------------------------------------

    def _check_staleness(self, station: str) -> None:
        """Emit CRITICAL if no data for *station* for more than 2× cadence_min."""
        last = self._last_obs_ts.get(station)
        if last is None:
            return
        elapsed_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
        threshold = 2 * self._cadence_min
        if elapsed_min > threshold:
            log.critical(
                "[amos] CRITICAL: %s no new data for %.0f minutes (threshold: %d min)",
                station,
                elapsed_min,
                threshold,
            )

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run_loop(self) -> None:
        """Run the polling loop indefinitely."""
        log.info("[amos] starting poll loop (cadence=%d min)", self._cadence_min)
        while True:
            try:
                self.poll()
            except Exception as exc:
                log.error("[amos] unexpected error in poll loop: %s", exc)
            time.sleep(self._cadence_min * 60)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    db = Database()
    AmosCollector(db).run_loop()
