"""Retroactive EMOS-shadow probability reconstruction (issue #1041).

Reconstructs the probability EMOS shadow's CURRENT coefficients would have
assigned to a historical ``bracket_evals`` row -- a read-only, offline
companion to the M3 decision gate (``bss_market_vs_model_report.py``), never
part of the live/shadow serving path.

**Read-only, always.** Every DB access in this module goes through
``ReadOnlyDatabase``, which opens the SQLite file with ``mode=ro`` AND
installs a SQLite authorizer that denies every write/DDL statement outright
(belt-and-braces: ``mode=ro`` alone can still be bypassed by, e.g., an
in-memory temp table backing a write attempt against the connection object).
The live bot is actively writing to ``data/meteoedge.db`` through the M3
window and this module must never block it or risk a write.

**Reuses production code, never re-derives it** (issue #1041's explicit
constraint -- this is exactly the shape of bug #810/#657):

- ``src.model.emos_mode.apply_emos`` / ``resolve_sigma_raw`` /
  ``_nearest_lead_hours`` / ``_active_stack_models`` for the EMOS mu/sigma
  correction.
- ``src.model.envelope.true_probability_yes`` for the bracket-probability
  integration (the ``max_env``-aware conditional integration #917/#920 fixed).

**Scope (documented limitation, not an oversight).** Reconstruction only
covers same-day, high-direction rows -- the population
``src.strategy.scanner``'s ``true_probability_yes`` call and this issue's
guidance both cover. Next-day rows (``next_day_probability_yes``, a distinct
probability path) and low-direction rows are out of scope; callers get
``None`` for those and must exclude them (never guess a probability for a
path this module was never asked to reconstruct).

**What's a genuine reconstruction, and what's a documented gap:**

- ``mu_raw``: the equal-weight mean of the ACTIVE FORECAST_STACK's serving
  members, sourced from ``model_forecast_log`` at the lead bin nearest to the
  row's ``minutes_to_settlement`` -- the same construction
  ``emos_serving_mu`` uses at scan time (issue #658/#760).
- ``sigma_raw``: always resolves to ``FORECAST_STDDEV_F`` in this window
  (``resolve_sigma_raw`` called with ``ensemble_sigma_f=None`` since
  ``bracket_evals`` never captured it and it was never populated live either
  -- see the module docstring of ``emos_shadow_vs_market_report.py`` for the
  caveat this implies for the report).
- Intraday delta: ``delta_f * decay_factor`` from the ``intraday_corrections``
  row nearest to (at or before) the bracket's ``poll_ts`` -- algebraically the
  same quantity ``src.model.intraday_correction.compute_correction`` computes
  as ``corrected_mu_f - deb_mu_f`` (both components, ``delta_f`` and
  ``decay_factor``, are stored columns; this is not a new formula).
- ``obs_bias_offset_f``: NOT reconstructed (bracket_evals never logged it) --
  left ``None``, the "no signal" default, never a guessed value.

Usage (library only -- this module has no CLI entry point of its own; see
``src.scripts.emos_shadow_vs_market_report`` for the report that drives it)::

    from pathlib import Path
    from src.scripts.emos_shadow_reconstruction import (
        ReadOnlyDatabase, reconstruct_bracket_row,
    )

    ro_db = ReadOnlyDatabase(Path("data/meteoedge.db"))
    try:
        # row is one bracket_evals row (see load_bracket_eval_rows in
        # bss_market_vs_model_report.py for the exact shape).
        p_emos_shadow = reconstruct_bracket_row(ro_db, row)  # float | None
    finally:
        ro_db._conn.close()
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date as _date, datetime, timezone
from pathlib import Path

from dateutil import parser as dtparse

from src.config import FORECAST_STDDEV_F, MODEL_STATE_ATTRS, STATION_TZ, get_live_config
from src.data.db import Database
from src.model.emos_mode import (
    _active_stack_models,
    _nearest_lead_hours,
    apply_emos,
    resolve_sigma_raw,
)
from src.model.envelope import Bracket, WeatherState, true_probability_yes
from src.strategy.scanner import STATION_TO_CITY

log = logging.getLogger(__name__)

# Write opcodes a SQLite authorizer callback can be asked to approve. Denying
# all of these turns "read-only connection" from "the file is opened with
# mode=ro" (already true, but relies on nothing upstream working around it)
# into "the driver itself refuses to even attempt the statement" -- a second,
# independent guard, and the one the standing test in this issue exercises
# directly (SQLITE_DENY raises before SQLite ever touches the file).
_DENIED_ACTIONS = frozenset({
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_ATTACH,
})


def _deny_writes(action: int, arg1, arg2, dbname, source) -> int:
    """SQLite authorizer callback: deny every write/DDL opcode, allow the rest.

    Installed on ``ReadOnlyDatabase``'s connection via ``set_authorizer``.
    Signature is dictated by the ``sqlite3`` C API (action code plus four
    positional args whose meaning varies by *action* -- unused here, since
    the decision only depends on *action* itself). Returning
    ``sqlite3.SQLITE_DENY`` makes SQLite raise before the statement runs;
    returning ``sqlite3.SQLITE_OK`` lets every read-only statement through
    unchanged.
    """
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class ReadOnlyDatabase(Database):
    """Read-only ``Database`` for offline reconstruction (issue #1041).

    Overrides ``__init__`` to open the SQLite file with ``mode=ro`` and
    installs a write-denying authorizer -- it deliberately skips the parent
    class's DDL/``_migrate()`` calls, which would otherwise create tables or
    run schema migrations against the live production file. Every other
    method (``get_emos_coefficients``, ``get_emos_coefficients_by_lead``,
    ``get_config``, ``get_all_config``, ``get_forecast_log_for_date``,
    ``get_intraday_corrections``, ...) is inherited from ``Database``
    UNCHANGED -- this module calls production SQL, verbatim, just through a
    connection that cannot write.
    """

    def __init__(self, path: "str | Path") -> None:  # noqa: D107 - see class docstring
        resolved = Path(path)
        if not resolved.exists():
            raise FileNotFoundError(f"ReadOnlyDatabase: no such file: {resolved}")
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            f"file:{resolved.as_posix()}?mode=ro", uri=True, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.set_authorizer(_deny_writes)
        # Deliberately NO DDL execution and NO self._migrate() call here --
        # both would attempt writes against a file this class promises never
        # to write to, even before the authorizer denies them.


def _to_utc(ts: "str | None") -> "datetime | None":
    """Parse *ts* (any ISO-8601-ish string) to a tz-aware UTC ``datetime``.

    Naive input is assumed to already be UTC (matches the convention every
    ``ts``/``poll_ts``/``obs_time`` column in this repo uses). Returns
    ``None`` for an empty/unparseable string -- never raises, so callers can
    treat a bad timestamp as "no signal" rather than a crash.
    """
    if not ts:
        return None
    try:
        t = dtparse.parse(ts)
    except (ValueError, OverflowError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def reconstruct_mu_raw(
    db: Database, station: str, date_str: str, minutes_to_settlement: float,
) -> "float | None":
    """Equal-weight mean of the active stack's serving members for (station, date).

    Sources per-model values from ``model_forecast_log`` at the lead bin
    nearest to ``minutes_to_settlement`` (issue #665's
    ``_nearest_lead_hours``, reused directly) -- the same construction
    ``emos_serving_mu`` performs at scan time from live ``WeatherState``
    attributes, here read back from the persisted forecast log instead.

    Returns ``None`` when no stack member has a row for this (station, date)
    -- mirrors ``emos_serving_mu``'s own "unservable" return.
    """
    active_stack = _active_stack_models(db)
    models = sorted(m for m in active_stack if m in MODEL_STATE_ATTRS)
    if not models:
        return None

    rows = db.get_forecast_log_for_date(station, date_str)
    by_model: "dict[str, list[dict]]" = {}
    for row in rows:
        if row.get("lead_hours") is None:
            continue  # legacy nowcast rows carry no lead bin -- not comparable
        by_model.setdefault(row["model"], []).append(row)

    lead_target = minutes_to_settlement / 60.0
    values: "list[float]" = []
    for model in models:
        candidates = by_model.get(model)
        if not candidates:
            continue
        leads = [c["lead_hours"] for c in candidates]
        nearest = _nearest_lead_hours(lead_target, leads)
        match = next(c for c in candidates if c["lead_hours"] == nearest)
        values.append(match["forecast_high_f"])

    if not values:
        return None
    return sum(values) / len(values)


def reconstruct_intraday_delta(
    db: Database, city: str, date_str: str, poll_ts: str,
) -> float:
    """Decayed intraday delta (``delta_f * decay_factor``) as of *poll_ts*.

    Picks the ``intraday_corrections`` row for (city, date) with the latest
    ``obs_time`` at or before ``poll_ts`` -- the same-window row
    ``emos_serving_mu`` would have layered on top of the calibrated mean at
    that scan. Returns 0.0 (the ``or 0.0`` default ``emos_serving_mu`` itself
    falls back to) when no such row exists, never a guessed nonzero value.
    """
    poll_dt = _to_utc(poll_ts)
    if poll_dt is None:
        return 0.0
    rows = db.get_intraday_corrections(city, date_str)
    best: "tuple[datetime, dict] | None" = None
    for row in rows:
        obs_dt = _to_utc(row.get("obs_time"))
        if obs_dt is None or obs_dt > poll_dt:
            continue
        if best is None or obs_dt > best[0]:
            best = (obs_dt, row)
    if best is None:
        return 0.0
    row = best[1]
    return float(row["delta_f"]) * float(row["decay_factor"])


def reconstruct_emos_mu_sigma(
    db: Database, station: str, city: str, date_str: str, poll_ts: str,
    minutes_to_settlement: float,
) -> "tuple[float, float] | None":
    """Return (mu_final, sigma_cal) EMOS shadow's current coefficients imply.

    Same #658 layer contract ``emos_serving_mu`` implements at scan time --
    mu_raw -> apply_emos -> + decayed intraday delta -- reconstructed here
    from persisted tables instead of a live ``WeatherState``. Returns
    ``None`` when mu_raw is unreconstructable (no stack member forecast
    logged for this station/date).
    """
    mu_raw = reconstruct_mu_raw(db, station, date_str, minutes_to_settlement)
    if mu_raw is None:
        return None

    live_config = get_live_config(db)
    use_ensemble_sigma = live_config.get("USE_ENSEMBLE_SIGMA", True)
    # bracket_evals never logged ensemble_sigma_f, and it was never populated
    # live during this window either (issue #1041 context) -- a stub state
    # with ensemble_sigma_f=None makes resolve_sigma_raw fall through to
    # FORECAST_STDDEV_F exactly as it does live today. Calling the real
    # function (rather than hardcoding FORECAST_STDDEV_F here) keeps this
    # module honest if that ever stops being true.
    stub_state = _StubSigmaState()
    sigma_raw = resolve_sigma_raw(stub_state, use_ensemble_sigma, FORECAST_STDDEV_F)

    mu_cal, sigma_cal = apply_emos(
        mu_raw, sigma_raw, city, db, minutes_to_settlement=minutes_to_settlement
    )
    intraday_delta = reconstruct_intraday_delta(db, city, date_str, poll_ts)
    mu_final = mu_cal + intraday_delta
    return mu_final, sigma_cal


class _StubSigmaState:
    """Minimal stand-in for ``resolve_sigma_raw``'s ``state`` argument.

    Only ``ensemble_sigma_f`` is read (via ``getattr(state, "ensemble_sigma_f",
    None)``); it is always ``None`` here (see ``reconstruct_emos_mu_sigma``
    docstring), which is exactly the "GEFS unavailable" branch
    ``resolve_sigma_raw`` already handles.
    """

    ensemble_sigma_f = None


def _station_local_now(station: str, poll_ts: str) -> "datetime | None":
    """Station-local datetime for *poll_ts*, or None if undeterminable."""
    tz_name = STATION_TZ.get(station)
    poll_dt = _to_utc(poll_ts)
    if tz_name is None or poll_dt is None:
        return None
    import pytz
    try:
        tz = pytz.timezone(tz_name)
    except pytz.UnknownTimeZoneError:
        return None
    return poll_dt.astimezone(tz)


def reconstruct_bracket_row(
    db: Database, row: dict,
) -> "float | None":
    """Return the EMOS-shadow-implied P(YES) for one ``bracket_evals`` row.

    ``row`` is a dict in the shape ``bss_market_vs_model_report.
    load_bracket_eval_rows`` produces. Returns ``None`` (out of scope, never
    a guess) for:

    - next-day rows (``is_next_day_flag`` truthy) -- a distinct probability
      path (``next_day_probability_yes``) this module does not reconstruct.
    - non-"high"-direction rows -- ``true_probability_yes`` is the high-side
      integration; a "low" market scored through it would be scored against
      the wrong physical quantity.
    - rows missing ``current_high``/``latest_temp``/``station``/``end_date``/
      ``minutes_to_settlement`` -- required WeatherState/lead inputs
      ``bracket_evals`` did not carry for this row.
    - rows where ``mu_raw`` cannot be reconstructed (no stack-member forecast
      logged for that station/date in ``model_forecast_log``).
    """
    if row.get("is_next_day_flag"):
        try:
            if int(row["is_next_day_flag"]):
                return None
        except (TypeError, ValueError):
            pass
    direction = row.get("direction") or "high"
    if direction != "high":
        return None

    station = row.get("station")
    end_date = (row.get("end_date") or "")[:10]
    mins_left = row.get("minutes_to_settlement")
    current_high = row.get("current_high")
    latest_temp = row.get("latest_temp")
    bracket_low = row.get("bracket_low")
    bracket_high = row.get("bracket_high")
    yes_ask = row.get("yes_ask")
    no_ask = row.get("no_ask")
    if not station or not end_date or mins_left is None:
        return None
    if current_high is None or latest_temp is None:
        return None
    if bracket_low is None or bracket_high is None or yes_ask is None or no_ask is None:
        return None

    city = STATION_TO_CITY.get(station, station)
    resolved = reconstruct_emos_mu_sigma(
        db, station, city, end_date, row.get("ts", ""), mins_left,
    )
    if resolved is None:
        return None
    mu_final, sigma_cal = resolved

    now_local = _station_local_now(station, row.get("ts", ""))
    if now_local is None:
        return None

    bracket = Bracket(
        ticker=row.get("ticker", ""),
        low_f=bracket_low,
        high_f=bracket_high,
        yes_ask_cents=int(yes_ask),
        yes_ask_size=0,
        no_ask_cents=int(no_ask),
        no_ask_size=0,
    )
    state = WeatherState(
        station=station,
        now_local=now_local,
        sunset_local=now_local,  # unused by compute_envelope/true_probability_yes
        current_high_f=float(current_high),
        current_high_time=now_local,  # unused
        latest_temp_f=float(latest_temp),
        latest_temp_time=now_local,  # unused
        forecast_high_f=None,  # unused -- corrected_mu_f (below) takes priority
        corrected_mu_f=mu_final,
    )

    live_config = get_live_config(db)
    try:
        settlement_date = _date.fromisoformat(end_date)
    except ValueError:
        return None

    return true_probability_yes(
        bracket, state, minutes_to_settlement=mins_left,
        forecast_stddev=sigma_cal,
        deb_enabled=live_config.get("DEB_ENABLED", False),
        sigma_climb_fraction=live_config.get("ENVELOPE_SIGMA_CLIMB_FRACTION", 0.5),
        use_ensemble_sigma=False,
        settlement_date=settlement_date,
    )
