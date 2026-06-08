"""Monitor freshness of observations by source and station."""
import logging
from datetime import datetime, timezone

from src.data.db import Database

_log = logging.getLogger(__name__)


class FreshnessMonitor:
    """Monitor observation staleness per source and station."""

    def check(
        self,
        db: Database,
        source: str,
        station: str,
        cadence_min: int,
    ) -> bool:
        """Check if the most recent observation for source/station is fresh.

        Args:
            db: Database instance
            source: Observation source name (e.g., 'jma_ameidas', 'metar')
            station: Station identifier
            cadence_min: Expected observation cadence in minutes

        Returns:
            True if data is fresh (exists and is within 2*cadence_min minutes).
            False if no data exists or if data is stale (older than 2*cadence_min minutes).
            Logs CRITICAL if data is stale.
        """
        obs = db.get_latest_observation(source, station)

        if obs is None:
            _log.critical(
                f"No observation data for {source}/{station}"
            )
            return False

        # Parse observation timestamp (ISO 8601 UTC)
        obs_ts = datetime.fromisoformat(obs["ts"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age_minutes = (now - obs_ts).total_seconds() / 60

        stale_threshold = 2 * cadence_min
        if age_minutes > stale_threshold:
            _log.critical(
                f"Stale observation for {source}/{station}: "
                f"age={age_minutes:.1f}min > threshold={stale_threshold}min"
            )
            return False

        return True

    def check_all(
        self,
        db: Database,
        sources: list[dict],
    ) -> dict[str, bool]:
        """Check freshness for all sources in a list.

        Args:
            db: Database instance
            sources: List of dicts with keys: source, station, cadence_min

        Returns:
            Dict mapping "{source}/{station}" -> bool (True if fresh, False if stale/missing)
        """
        results = {}
        for source_config in sources:
            source = source_config["source"]
            station = source_config["station"]
            cadence_min = source_config["cadence_min"]

            key = f"{source}/{station}"
            results[key] = self.check(db, source, station, cadence_min)

        return results
