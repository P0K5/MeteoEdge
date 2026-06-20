# Operations Reference

## Environment Variables

### Data Retention

| Variable | Default | Component | Effect |
|---|---|---|---|
| `SNAPSHOT_RETAIN_DAYS` | `365` | `src/utils/log_rotation.py` | How many days of snapshot JSONL files (`snapshots.jsonl`, `position_snapshots.jsonl`) to retain before deletion. Overrides `LOG_ROTATION_RETAIN_DAYS` for snapshot and position-snapshot log files only. |
| `ARCHIVE_DB_PATH` | `data/analytics.db` | `src/data/archive_db.py` | Path to the analytics SQLite database used by the archive pipeline. |
