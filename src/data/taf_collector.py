"""TAF ingestion module — polls aviationweather.gov every 30 minutes.

Fetches raw TAF JSON for each airport ICAO, parses it via TafParser, and
persists the resulting time-window records to the taf_windows table.

Standalone usage::

    python -m src.data.taf_collector

Design notes
------------
- Uses ``fetch()`` from ``src.http_client`` — never opens its own HTTP session.
- Uses ``Database`` from ``src.data.db`` — never opens raw sqlite3 connections.
- Before inserting new windows for a (city, issued_at) pair, deletes any stale
  rows with the same key to prevent duplicates.
- Per-ICAO errors are logged and swallowed so a single bad station cannot abort
  the loop for the rest.
- Cadence is configurable via the ``TAF_CADENCE_MINUTES`` env var (default 30).
"""
from __future__ import annotations

import os
import time

from src.config import STATIONS
from src.data.db import Database
from src.data.taf_parser import TafParser
from src.http_client import fetch

# ---------------------------------------------------------------------------
# Airport ICAO -> city mapping derived from STATIONS config.
# TAF stations use the settlement ICAO (index 4) mapped to the city name (index 3).
# At minimum we cover RJTT (Tokyo), RKSI (Seoul), WSSS (Singapore), but all
# configured stations with valid ICAO codes are included.
# ---------------------------------------------------------------------------

_AIRPORT_ICAOS: list[tuple[str, str]] = [
    (icao, city)
    for _, _, _, city, icao, *_ in STATIONS
]

# Additional ICAO codes not in the main STATIONS list but required by the epic.
# RJTT (Tokyo Haneda) is the TAF source for the "Tokyo" city.
_EXTRA_ICAOS: list[tuple[str, str]] = [
    ("RJTT", "Tokyo"),
]

# Build final deduplicated list: prefer STATIONS entries; add extras only if city
# not already represented.
_CITY_SEEN: set[str] = {city for _, city in _AIRPORT_ICAOS}
for icao, city in _EXTRA_ICAOS:
    if city not in _CITY_SEEN:
        _AIRPORT_ICAOS.append((icao, city))
        _CITY_SEEN.add(city)

_TAF_URL = "https://aviationweather.gov/api/data/taf?ids={icao}&format=json"

_DEFAULT_CADENCE_MIN = int(os.getenv("TAF_CADENCE_MINUTES", "30"))


class TafCollector:
    """Fetch TAF data for all configured airports and persist to the DB.

    Args:
        db: An open ``Database`` instance — the collector never creates its own.
        cadence_min: Poll interval in minutes (overridden by TAF_CADENCE_MINUTES).
        airport_icaos: List of (icao, city) pairs to poll. Defaults to the
            full list derived from ``src.config.STATIONS`` plus extra TAF-only
            airports (e.g. RJTT for Tokyo).
    """

    def __init__(
        self,
        db: Database,
        cadence_min: int = _DEFAULT_CADENCE_MIN,
        airport_icaos: list[tuple[str, str]] | None = None,
    ) -> None:
        self._db = db
        self._cadence_min = cadence_min
        self._icaos = airport_icaos if airport_icaos is not None else _AIRPORT_ICAOS
        self._parser = TafParser()

    def fetch_one(self, icao: str, city: str) -> int:
        """Fetch, parse, and persist TAF windows for *icao* / *city*.

        Returns the number of windows inserted.  Raises on HTTP or parse
        errors (caller is responsible for catching).
        """
        url = _TAF_URL.format(icao=icao)
        r = fetch(url)
        raw_json = r.text  # pass full JSON to parser (handles rawOb/rawTAF extraction)

        windows = self._parser.parse(raw_json, city)
        if not windows:
            return 0

        # All windows share the same issued_at (from TAF header).
        issued_at = windows[0]["issued_at"]

        # Delete stale rows for this (city, issued_at) before re-inserting.
        deleted = self._db.delete_stale_taf_windows(city, issued_at)
        if deleted:
            print(f"[taf] {icao}: deleted {deleted} stale windows for issued_at={issued_at}")

        for window in windows:
            self._db.insert_taf_window(window)

        n = len(windows)
        print(f"[taf] fetched {icao}: {n} windows")
        return n

    def run_loop(self) -> None:
        """Poll all configured airports in a continuous loop.

        Each iteration fetches all ICAOs, sleeping ``cadence_min`` minutes
        between full sweeps.  Errors for a single ICAO are logged and do not
        abort the loop.
        """
        print(
            f"[taf] starting loop — {len(self._icaos)} airports, "
            f"cadence={self._cadence_min}min"
        )
        while True:
            for icao, city in self._icaos:
                try:
                    self.fetch_one(icao, city)
                except Exception as exc:
                    print(f"[taf] {icao} error: {exc}")
            time.sleep(self._cadence_min * 60)


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _main() -> None:
    db = Database()
    collector = TafCollector(db)
    collector.run_loop()


if __name__ == "__main__":
    _main()
