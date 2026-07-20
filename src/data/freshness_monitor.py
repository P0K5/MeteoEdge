"""Monitor freshness of observations by source and station (issue #745).

Thresholds are derived from each feed's own configured cadence rather than a
global constant, so a 30-minute METAR feed is not flagged stale at 14 minutes.
Alarms are de-duplicated to **one CRITICAL per outage** (a fresh->stale state
transition); while an outage persists the check logs at DEBUG (a heartbeat, not
a repeat CRITICAL), and a single INFO recovery line is emitted when data
resumes. This keeps genuinely-actionable failures from drowning in expected
noise (the reason the Tokyo JMA 404s went unnoticed for weeks -- issue #745).
"""
import logging
from datetime import datetime, timezone

from src.data.db import Database
from src import config

_log = logging.getLogger(__name__)

# Per-(source, station) outage state. A key is ABSENT while the feed is healthy
# and PRESENT (value = UTC datetime the outage was first flagged) while an
# outage is active. This is the whole de-dup state machine: the transition
# absent -> present is the single CRITICAL; present -> absent is the recovery.
_outage_since: dict[str, datetime] = {}


def cadence_staleness_threshold_min(cadence_min: float) -> float:
    """Staleness threshold (minutes) derived from a feed's cadence (issue #745).

    A feed is only "stale" once it has missed enough of its own cadence to be
    actionable: ``max(3 x cadence, cadence + 15min)``. The additive floor keeps
    fast feeds (cadence of a minute or two) from alarming on a single skipped
    tick, while the multiplicative term scales the threshold with slow feeds.
    """
    c = max(1.0, float(cadence_min))
    return max(3.0 * c, c + 15.0)


class FreshnessMonitor:
    """Monitor observation staleness per source and station."""

    def check(
        self,
        db: Database,
        source: str,
        station: str,
        cadence_min: int,
    ) -> bool:
        """Check whether the latest observation for source/station is fresh.

        Returns True if data is within the cadence-derived threshold, False if
        it is stale or missing. Logging is state-transition de-duplicated:
        - fresh (and no active outage): silent
        - fresh (ending an outage): one INFO recovery line
        - stale/missing (new outage): one CRITICAL
        - stale/missing (outage continuing): DEBUG heartbeat only
        """
        obs = db.get_latest_observation(source, station)
        now = datetime.now(timezone.utc)
        key = f"{source}/{station}"
        threshold_min = cadence_staleness_threshold_min(cadence_min)

        if obs is None:
            self._flag_outage(key, now, f"no observation data (threshold={threshold_min:.1f}min)")
            return False

        obs_ts = datetime.fromisoformat(obs["ts"].replace("Z", "+00:00"))
        age_min = (now - obs_ts).total_seconds() / 60.0

        if age_min <= threshold_min:
            if key in _outage_since:
                _log.info(
                    "[freshness] recovered %s: age=%.1fmin <= threshold=%.1fmin",
                    key, age_min, threshold_min,
                )
                _outage_since.pop(key, None)
            return True

        self._flag_outage(
            key, now,
            f"age={age_min:.1f}min > threshold={threshold_min:.1f}min",
        )
        return False

    @staticmethod
    def _flag_outage(key: str, now: datetime, reason: str) -> None:
        """Emit one CRITICAL on the fresh->stale transition, DEBUG thereafter."""
        if key not in _outage_since:
            _outage_since[key] = now
            _log.critical("[freshness] CRITICAL %s stale: %s", key, reason)
        else:
            _log.debug("[freshness] %s still stale: %s", key, reason)

    def check_all(
        self,
        db: Database,
        sources: list[dict],
    ) -> dict[str, bool]:
        """Check freshness for all sources in a list.

        Skips chronically-dead / delisted METAR stations listed in
        ``config.METAR_SKIP_STATIONS`` (issue #744, e.g. ZSJN): their fetch is
        short-circuited, so their absence is expected and must not raise a
        freshness outage.

        Returns a dict mapping ``"{source}/{station}"`` -> bool (fresh?).
        """
        results = {}
        for source_config in sources:
            source = source_config["source"]
            station = source_config["station"]

            if source == "metar" and station in config.METAR_SKIP_STATIONS:
                continue

            cadence_min = source_config.get(
                "cadence_min", config.FRESHNESS_THRESHOLD_DEFAULT_MIN
            )
            key = f"{source}/{station}"
            results[key] = self.check(db, source, station, cadence_min)

        return results
