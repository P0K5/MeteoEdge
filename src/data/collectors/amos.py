"""AMOS collector — RETIRED (issue #740).

Historical context: this module used to poll two sources for Seoul (RKSI)
and Busan (RKPK) runway-level temperature readings, persisted under
source='amos':

  1. KMA ASOS API (``AsosHourlyInfoService``) — never actually produced a
     single row in production. The endpoint is archive-only by design
     (issue #694): every same-day request returns HTTP 200 with
     ``resultCode == "99"`` ("data up to the previous day only"), so
     ``_fetch_kma()`` always fell through to the fallback below.
  2. Open-Meteo (api.open-meteo.com) model fallback — the *only* path that
     ever actually wrote a row. Every ``source='amos'`` observation
     persisted since 2026-06-08 was Open-Meteo modelled data, not a real
     AMOS sensor reading (``is_official=0``).

Issue #740: RKSI/RKPK METAR (30-min cadence, official, already collected
via the shared METAR path in ``src/weather/builder.py``) is real official
data for these same two physical stations. Continuing to persist
Open-Meteo model output under ``source='amos'`` served no purpose --
worse, it actively contaminated the daily-high TRUTH queries used for
EMOS/DEB training whenever the modelled series peaked above the real
METAR reading (see issue #741 / ``Database.get_daily_obs_high()`` and
``Database.get_obs_highs_range()`` in ``src/data/db.py``), and it produced
spurious CRITICAL freshness alerts once ``KMA_API_KEY`` was never
provisioned (the KMA path was unobtainable by design, not a transient
outage).

Resolution (issue #740): the KMA path and the Open-Meteo fallback are both
removed. METAR (RKSI/RKPK) is now the sole Korea observation truth feed --
see ``config/source_priority.yaml``, where the ``amos`` entries for Seoul
and Busan have been removed and the ``metar`` entries are now primary.

This module is retired but kept in place (rather than deleted) so the
existing import and collector-thread wiring in ``src/scripts/run.py``
keeps working without a scheduler change. ``AmosCollector.poll()`` and
``run_loop()`` are now no-ops: they make no HTTP calls, write no DB rows,
and simply log once that the collector is retired.

Historical ``source='amos'`` rows already in the DB are left untouched --
they are ``is_official=0`` and are excluded from truth queries by issue
#741. This module does not (and must not) delete them.
"""

import logging

log = logging.getLogger(__name__)


class AmosCollector:
    """Retired (issue #740). See module docstring for background.

    Kept as a no-op class so existing imports/wiring (``src/scripts/run.py``)
    continue to work without a scheduler change. ``poll()`` and
    ``run_loop()`` do nothing but log a single retirement notice -- no HTTP
    calls are made and no rows are written.
    """

    def __init__(self, db) -> None:
        self._db = db

    def poll(self) -> dict[str, bool]:
        """No-op. Returns False for both stations -- nothing is fetched or stored."""
        log.info(
            "[amos] retired (issue #740) -- METAR RKSI/RKPK is now the sole "
            "Korea observation truth feed; amos collector is a no-op."
        )
        return {"Seoul": False, "Busan": False}

    def run_loop(self) -> None:
        """No-op. Logs once and returns immediately (thread exits)."""
        log.info(
            "[amos] retired (issue #740) -- METAR RKSI/RKPK is now the sole "
            "Korea observation truth feed; amos collector thread is exiting (no-op)."
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    log.info("[amos] retired (issue #740) -- nothing to run.")
