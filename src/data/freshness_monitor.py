"""Monitor freshness of observations by source and station."""
import logging
from datetime import datetime, timezone

from src.data.db import Database
from src import config

_log = logging.getLogger(__name__)

# De-duplication state: tracks the last time we logged a warning/critical for each (source, station) pair.
# Maps "{source}/{station}" -> datetime(last_log_time_utc)
_last_log_time: dict[str, datetime] = {}


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

        Uses source-specific thresholds from config to determine staleness levels:
        - Silent: within freshness threshold
        - WARNING: 1x–3x threshold
        - CRITICAL: >3x threshold OR silent for >24h
        - Recovery INFO: returns to fresh after WARNING/CRITICAL

        De-duplicates logs: at most one log per (source, station) per 15 minutes.

        Args:
            db: Database instance
            source: Observation source name (e.g., 'jma_ameidas', 'metar')
            station: Station identifier
            cadence_min: Expected observation cadence in minutes (unused with config thresholds)

        Returns:
            True if data is fresh (within threshold).
            False if no data exists or data is stale.
        """
        obs = db.get_latest_observation(source, station)
        now = datetime.now(timezone.utc)
        key = f"{source}/{station}"

        if obs is None:
            # No data at all — log CRITICAL (no de-duplication cap)
            _log.critical(f"No observation data for {key}")
            # Reset de-dup state since we have no data
            _last_log_time.pop(key, None)
            return False

        # Parse observation timestamp (ISO 8601 UTC)
        obs_ts = datetime.fromisoformat(obs["ts"].replace("Z", "+00:00"))
        age_seconds = (now - obs_ts).total_seconds()

        # Get threshold for this source (already in seconds)
        threshold_seconds = config.FRESHNESS_THRESHOLDS_MIN.get(
            source, config.FRESHNESS_THRESHOLD_DEFAULT_MIN
        )

        # Check freshness
        if age_seconds <= threshold_seconds:
            # Data is fresh — silent (no log)
            # If we previously logged WARNING/CRITICAL, emit recovery INFO
            if key in _last_log_time:
                _log.info(f"Observation recovered for {key}")
                _last_log_time.pop(key)
            return True

        # Data is stale — check if we should log (de-duplication window: 15 minutes = 900 seconds)
        dedup_window_seconds = 15 * 60
        last_log = _last_log_time.get(key)
        time_since_last_log = (now - last_log).total_seconds() if last_log else float('inf')

        # Check if we should emit a log (outside dedup window)
        should_log = time_since_last_log >= dedup_window_seconds

        if should_log:
            age_minutes = age_seconds / 60
            threshold_minutes = threshold_seconds / 60
            if age_seconds > 24 * 3600:
                # CRITICAL: silent for >24h
                _log.critical(
                    f"Observation silent for {key}: "
                    f"age={age_minutes:.1f}min > 24h"
                )
            elif age_seconds > 3 * threshold_seconds:
                # CRITICAL: >3x threshold
                _log.critical(
                    f"Stale observation for {key}: "
                    f"age={age_minutes:.1f}min > 3x threshold={threshold_minutes:.1f}min"
                )
            else:
                # WARNING: 1x–3x threshold
                _log.warning(
                    f"Stale observation for {key}: "
                    f"age={age_minutes:.1f}min > threshold={threshold_minutes:.1f}min"
                )
            _last_log_time[key] = now

        return False

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
