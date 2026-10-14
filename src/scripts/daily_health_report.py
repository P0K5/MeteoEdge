"""Daily health report email for MeteoEdge.

Gathers a snapshot of bot health, trading activity, data pipeline status,
guardrail events, EMOS progress, and the impact of the 2026-10-11 entry-gate
widening experiment (MAX_CONFIDENCE_YES_FOR_NO 0.05->0.10 + the first
emos_primary promotions) -- overall and broken down per station -- then
emails it as a plain-text report via SMTP.

The M3 remediation-plan metrics and their open-blocker checklist (#885/#897/
#893) are retired from this report now that the weather-trading thesis is
closed (M3/M3d, 2026-08-28/2026-10-03) -- their functions and tests remain in
this module for now, just unused here, rather than deleted outright.

Usage::

    python -m src.scripts.daily_health_report           # send email
    python -m src.scripts.daily_health_report --dry-run # print to stdout

Schedule: systemd timer ``meteoedge-health-report.timer`` fires daily at
14:00 UTC (after settlement, resolve-outcomes, and prob-cap report).

Configuration (all from environment / ``.env``):

    SMTP_HOST        — SMTP server (default: smtp.gmail.com)
    SMTP_PORT        — SMTP port (default: 587)
    SMTP_USER        — SMTP sender address / login
    SMTP_PASS        — SMTP password or app password
    ALERT_EMAIL_TO   — recipient address (required)

Design constraints:
- Runs in ~seconds; all queries are lightweight aggregates.
- Degrades gracefully: if a section's data is unavailable it reports
  "(unavailable)" rather than crashing.
- All SMTP config comes from environment variables -- nothing committed.
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup -- make `src` importable from this script's location.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE.parents[1]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[1]))

from src.logging_config import setup_logging  # noqa: E402
from src.data.copy_skip_reasons import (  # noqa: E402
    BASELINE_DAYS as _SKIP_BASELINE_DAYS,
    WINDOW_HOURS as _SKIP_WINDOW_HOURS,
)
from src.data.log_error_classes import section as _build_log_errors  # noqa: E402

#: Start of the M3 clean-data collection window, and the date station-days are
#: counted from. This has moved TWICE as probability defects landed mid-window
#: -- #917 (degF half-width ladders) went live 2026-08-01, #920 (truncation
#: without renormalisation) on 2026-08-05 -- so it is a constant rather than a
#: literal buried in a query. Both moves invalidated every station-day
#: collected before them; leaving the old date in place is how this report came
#: to print "362/300 (121%)" on 2026-08-04 against a window that was entirely
#: contaminated. Keep in step with docs/REMEDIATION_PLAN.md's M3 section.
M3_CLEAN_DATA_CLOCK_START = "2026-08-06"

#: Date the entry-gate widening experiment landed: MAX_CONFIDENCE_YES_FOR_NO
#: raised 0.05 -> 0.10 and the first batch of cities promoted to emos_primary
#: (2026-10-10/11, same change). "Since the change" figures in this report
#: anchor here rather than a rolling window, so the sample grows every day
#: this report runs instead of resetting to "last 24h" each time. Settlement
#: lags entry by ~1-2 days (settle.py runs as a daily batch), so a trade's
#: absence from "settled" counts here is expected for the last day or two,
#: not evidence of a stuck pipeline.
PARAM_CHANGE_DATE = "2026-10-11T00:00:00"

#: Cities promoted to emos_primary as part of the same change -- used to
#: split "is the promotion itself paying off" from the gate-widening effect,
#: which otherwise land in the same window and would be impossible to tell
#: apart.
EMOS_PRIMARY_PROMOTED_CITIES = (
    "Miami", "Seoul", "Kuala Lumpur", "Singapore",
    "Panama City", "London", "Paris",
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SMTP / email constants (mirrors src/monitoring/alerts.py)
# ---------------------------------------------------------------------------
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO", "")
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
POLL_WARN_MINUTES = 20
POLL_STALE_MINUTES = 60
# issue #914: warn if fewer than this fraction of expected polls landed in
# the last 24h, even if the most recent single poll was fine.
POLL_COUNT_WARN_RATIO = 0.7
WIN_RATE_N = 20
EMOS_MIN_SAMPLES_PROMOTION = 60

# issue #1237: `scan_decisions` stopped receiving writes on 2026-08-28 and
# nothing here noticed for a month, because every existing check reads a
# VALUE from a table (a count, a mean, a max) without first asking whether
# the table is still LIVE -- the same blind spot as #910-#914's unfireable
# rail metric, one layer up. `_build_table_liveness` below is the generic
# fix: assert liveness for every table this report actually depends on,
# rather than adding a fifth bespoke "is X stale" check.
#
# Each entry is (table, timestamp_column, warn_after_minutes).
# `warn_after_minutes` is "a few times" the table's normal write cadence --
# see the per-table comment -- never the cadence itself, so a single missed
# cycle does not WARN. Cadences are the ones observed in production and
# documented on issue #1237:
TABLE_LIVENESS_SPECS: "list[tuple[str, str, int]]" = [
    # ~200-255 rows/day (~6 min/row) via the unconditional per-poll
    # heartbeat -- already covered by `_build_bot_health`'s own gap/count
    # logic, but included here too so the generic mechanism is provably
    # able to catch it independently (issue #1237 test requirement).
    ("poll_runs", "poll_ts", 30),
    # ~200-255 rows/day, same order as poll_runs, but conditional on a
    # bracket actually clearing the candidate bar -- looser multiplier than
    # poll_runs to tolerate quiet stretches without a real fault.
    ("candidates", "ts", 60),
    # The table that actually died. ~319 distinct brackets/day when healthy;
    # it upserts on (station, ticker, date), but MAX(poll_ts) still advances
    # on every write regardless, so a stopped write path reads as its
    # newest row aging in real time. 6h catches the incident on day one
    # (age ~24h by day two), not a month later.
    ("scan_decisions", "poll_ts", 360),
    # Hourly per station.
    ("observations", "ts", 180),
    # Written once/day per station/model; 48h tolerates one missed day.
    ("model_forecast_log", "logged_at", 2880),
]

#: Tables that legitimately go quiet -- `copy_live_positions` is empty until
#: copy-trading goes live, `risk_state` is written only on days the bot
#: actually trades -- and must therefore never appear in
#: ``TABLE_LIVENESS_SPECS``. This is an explicit allow-list, not a sweep of
#: every table (issue #1237 explicitly rules out the sweep: an unconditional
#: check would WARN on these two every single day and train everyone to
#: ignore the report). Kept as a real constant, and asserted disjoint from
#: ``TABLE_LIVENESS_SPECS`` by a test, rather than left as an implicit
#: "just don't add them" convention nobody notices breaking.
TABLE_LIVENESS_QUIET_OK: "set[str]" = {"copy_live_positions", "risk_state"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: "datetime | None") -> str:
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_pnl(eur: "float | None") -> str:
    if eur is None:
        return "N/A"
    sign = "+" if eur >= 0 else "-"
    return f"{sign}EUR{abs(eur):,.2f}"


def _fmt_pct(value: "float | None", digits: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.{digits}f}%"


def _fmt_age(minutes: float) -> str:
    """Render an age in minutes as the most readable unit for its size."""
    if minutes < 120:
        return f"{minutes:.0f} min"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def _send_email(subject: str, body: str) -> bool:
    """Deliver an email via SMTP.  Returns True on success."""
    if not SMTP_USER or not SMTP_PASS or not ALERT_EMAIL_TO:
        log.warning("SMTP not configured -- email not sent")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL_TO
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)
        log.info("Health report email sent to %s", ALERT_EMAIL_TO)
        return True
    except Exception:
        log.exception("Failed to send health report email")
        return False


# ---------------------------------------------------------------------------
# Section builders -- each returns a list of lines (str).
# ---------------------------------------------------------------------------

def _build_header(today: datetime) -> list[str]:
    return [
        f"MeteoEdge Daily Health -- {today.strftime('%Y-%m-%d')}",
        "=" * 50,
        f"Generated: {_iso(today)}",
        "",
    ]


def _build_bot_health(db, today: datetime) -> "tuple[list[str], dict]":
    """Poll recency, poll count, and systemd status.

    Poll count and gap are derived from ``poll_runs`` -- an unconditional
    heartbeat written once per poll cycle by ``poll_once()`` (issue #914).
    ``scan_decisions`` is deliberately NOT used for this: it is only written
    when brackets are actually evaluated, which happens far less often than
    the bot polls, so counting it against a /288 denominator produced
    permanent false "5/288" alarms. The scan_decisions cadence is still
    surfaced below as a separate "Evaluated" line, since it is genuinely
    useful, just not a poll count.

    Returns ``(lines, flags)``. ``flags["status"]`` is one of
    ``OK / WARN / STALE / CRIT / UNKNOWN`` and is what ``_build_verdict``
    consumes -- never the rendered text (issue #914 defect 3).
    """
    lines = ["Bot Pulse", "-" * 10]
    flags: dict = {"status": "UNKNOWN"}
    try:
        row = db._conn.execute(
            "SELECT MAX(poll_ts) FROM poll_runs"
        ).fetchone()
        last_poll_str = row[0] if row and row[0] else None
        if last_poll_str:
            last_poll = datetime.fromisoformat(last_poll_str)
            gap_min = (_utc_now() - last_poll).total_seconds() / 60
            lines.append(f"  Last poll:   {_iso(last_poll)} ({gap_min:.0f} min ago)")

            # Polls in last 24h -- from the unconditional heartbeat, not
            # scan_decisions (issue #914 defect 1).
            cutoff = (today - timedelta(hours=24)).isoformat()
            count_row = db._conn.execute(
                "SELECT COUNT(*) FROM poll_runs WHERE poll_ts >= ?",
                (cutoff,),
            ).fetchone()
            poll_count = count_row[0] if count_row else 0
            expected = int(24 * 60 / 5)  # 288 polls/day at 5-min interval
            lines.append(f"  Polls 24h:   {poll_count}/{expected}")

            # Evaluated brackets (scan_decisions) -- a separate, genuinely
            # useful metric, but NOT a poll count: it is only written when
            # brackets are actually evaluated.
            # NOT COUNT(DISTINCT poll_ts): scan_decisions upserts on
            # (station, ticker, date), so each market keeps only its most
            # recent write and the surviving timestamps collapse to roughly one
            # per market's last scan. That query returned "4" on 2026-08-04 --
            # which reads as a dead pipeline and is in fact the expected steady
            # state of an upsert table. Count the live rows instead: it is
            # monotonic in coverage and means what the label says.
            eval_row = db._conn.execute(
                "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts >= ?",
                (cutoff,),
            ).fetchone()
            eval_count = eval_row[0] if eval_row else 0
            lines.append(f"  Brackets live (scan_decisions, upserted): {eval_count}")

            # Check for gaps between heartbeats
            gaps = db._conn.execute(
                "SELECT poll_ts FROM poll_runs "
                "WHERE poll_ts >= ? ORDER BY poll_ts",
                (cutoff,),
            ).fetchall()
            max_gap = 0.0
            for i in range(1, len(gaps)):
                dt1 = datetime.fromisoformat(gaps[i - 1][0])
                dt2 = datetime.fromisoformat(gaps[i][0])
                gap = (dt2 - dt1).total_seconds() / 60
                if gap > max_gap:
                    max_gap = gap
            gap_warn = len(gaps) >= 2 and max_gap > POLL_WARN_MINUTES
            if gap_warn:
                lines.append(f"  [WARN] Max gap:    {max_gap:.0f} min (threshold: {POLL_WARN_MINUTES})")

            count_warn = poll_count < expected * POLL_COUNT_WARN_RATIO

            # issue #914 defect 2: fold the 24h poll-count shortfall and the
            # max gap into the status, not just the single most-recent gap.
            if gap_min > POLL_STALE_MINUTES:
                status = "STALE"
                detail = f"{gap_min:.0f} min since last poll"
            elif gap_warn or count_warn:
                status = "WARN"
                reasons = []
                if count_warn:
                    reasons.append(f"only {poll_count}/{expected} polls in 24h")
                if gap_warn:
                    reasons.append(f"max gap {max_gap:.0f} min")
                detail = ", ".join(reasons)
            elif gap_min > POLL_WARN_MINUTES:
                status = "WARN"
                detail = f"{gap_min:.0f} min since last poll"
            else:
                status = "OK"
                detail = "Healthy"

            tag = {"OK": "[OK]", "WARN": "[WARN]", "STALE": "[STALE]", "CRIT": "[CRIT]"}[status]
            lines.append(f"  Status:      {tag} {detail}")
            flags = {
                "status": status,
                "detail": detail,
                "poll_count_24h": poll_count,
                "expected_polls_24h": expected,
                "max_gap_min": max_gap,
            }
        else:
            lines.append("  Status:      [CRIT] No polls found in poll_runs")
            flags = {"status": "CRIT", "detail": "No polls found in poll_runs"}
    except Exception:
        log.exception("Bot health query failed")
        lines.append("  (unavailable -- query error)")
        flags = {"status": "UNKNOWN", "detail": "query error"}
    lines.append("")
    return lines, flags


def _build_table_liveness(db, today: datetime) -> "tuple[list[str], dict]":
    """WARN when a table this report depends on has stopped receiving writes.

    **The fix for issue #1237.** ``scan_decisions`` stopped receiving writes
    on 2026-08-28 and every section that reads it (bot pulse's "Brackets
    live" line, M3's leading indicators via ``bracket_evals`` -- a different
    table, but the same class of gap) kept reporting a VALUE computed from
    whatever was already there, with nothing asserting the table was still
    LIVE. This section is the generic version of that assertion, run against
    ``TABLE_LIVENESS_SPECS`` -- an explicit, hand-picked list of tables that
    must always be live, not a sweep of every table (``TABLE_LIVENESS_QUIET_OK``
    exists precisely because a sweep would false-WARN on tables that
    legitimately go quiet).

    Each table's ``MAX(timestamp_column)`` is compared against ``today``
    (mirroring ``_build_bot_health``'s own gap arithmetic). A table with zero
    rows reports as "no rows ever" rather than crashing on
    ``datetime.fromisoformat(None)`` -- absent data must WARN, not be
    silently skipped (same principle as ``_ladder_mass`` returning ``None``
    rather than reading as a healthy ladder).

    Returns ``(lines, flags)``. ``flags["status"]`` is ``OK`` / ``WARN`` and
    ``flags["details"]`` carries one human-readable string per stale table
    for ``_build_verdict`` to surface -- there can be more than one stale
    table at once, unlike the single-status sections.
    """
    lines = ["Table Liveness", "-" * 14]
    flags: dict = {"status": "OK", "stale_tables": [], "details": []}
    for table, ts_col, warn_after_min in TABLE_LIVENESS_SPECS:
        try:
            row = db._conn.execute(f"SELECT MAX({ts_col}) FROM {table}").fetchone()
            newest_str = row[0] if row else None
        except Exception:
            log.exception("Table liveness query failed for %s", table)
            lines.append(f"  {table:<20} (unavailable -- query error)")
            continue

        if not newest_str:
            lines.append(f"  {table:<20} no rows ever                [WARN]")
            flags["stale_tables"].append(table)
            flags["details"].append(f"{table} has never been written to")
            continue

        newest = datetime.fromisoformat(newest_str)
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=timezone.utc)
        # Uses `today` (the moment `build_report` started), not a fresh
        # `_utc_now()` call, so this section's ages are deterministic given
        # its inputs and reproducible in tests -- unlike `_build_bot_health`,
        # which intentionally re-samples the wall clock for its single most
        # recent poll gap.
        age_min = (today - newest).total_seconds() / 60
        stale = age_min > warn_after_min
        age_str = _fmt_age(age_min)
        tag = "[WARN]" if stale else "[OK]"
        lines.append(f"  {table:<20} newest row {age_str:<10} old   {tag}")
        if stale:
            flags["stale_tables"].append(table)
            flags["details"].append(
                f"{table} stalled -- newest row {age_str} old "
                f"(warn threshold {_fmt_age(warn_after_min)})"
            )

    if flags["stale_tables"]:
        flags["status"] = "WARN"
    lines.append("")
    return lines, flags


def _build_trading(db, today: datetime) -> list[str]:
    """Live/shadow trades, P&L, open positions, win rate, daily PnL."""
    lines = ["Trading (last 24h UTC)", "-" * 20]
    try:
        since = (today - timedelta(hours=24)).isoformat()

        # Live trades settled in last 24h
        live_row = db._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE mode='live' AND settled_at IS NOT NULL AND pnl IS NOT NULL "
            "AND settled_at >= ?",
            (since,),
        ).fetchone()
        live_count, live_pnl = (live_row[0], live_row[1]) if live_row else (0, 0)
        lines.append(f"  Live settled:  {live_count} trades, {_fmt_pnl(live_pnl)}")

        # Shadow trades settled in last 24h (high direction only)
        shadow_row = db._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE mode='shadow' AND direction='high' "
            "AND settled_at IS NOT NULL AND pnl IS NOT NULL "
            "AND settled_at >= ?",
            (since,),
        ).fetchone()
        shadow_count, shadow_pnl = (shadow_row[0], shadow_row[1]) if shadow_row else (0, 0)
        lines.append(f"  Shadow settled: {shadow_count} trades, {_fmt_pnl(shadow_pnl)}")

        # Open positions
        pos_row = db._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(shares * entry_price / 100.0), 0) "
            "FROM open_positions"
        ).fetchone()
        open_count, exposure = (pos_row[0], pos_row[1]) if pos_row else (0, 0)
        lines.append(f"  Open positions: {open_count}, exposure {_fmt_pnl(exposure)}")

        # Win rate over last N settled live trades
        wr_rows = db._conn.execute(
            "SELECT pnl FROM trades "
            "WHERE mode='live' AND settled_at IS NOT NULL AND pnl IS NOT NULL "
            "ORDER BY settled_at DESC LIMIT ?",
            (WIN_RATE_N,),
        ).fetchall()
        if wr_rows and len(wr_rows) >= 5:
            wins = sum(1 for (pnl_val,) in wr_rows if pnl_val > 0)
            wr = wins / len(wr_rows)
            # issue #914 defect 4: this query has no time filter -- it's the
            # last N settled live trades all-time, not "last 24h". Label it
            # accordingly so it doesn't read as today's win rate.
            lines.append(f"  Win rate (last {len(wr_rows)} settled, all-time): {_fmt_pct(wr)} ({wins}/{len(wr_rows)})")
        else:
            lines.append(f"  Win rate:      <5 settled trades -- N/A")

        # Daily PnL
        today_str = today.strftime("%Y-%m-%d")
        daily_row = db._conn.execute(
            "SELECT daily_pnl FROM risk_state WHERE trade_date = ?",
            (today_str,),
        ).fetchone()
        daily_pnl = daily_row[0] if daily_row else None
        lines.append(f"  Daily PnL:     {_fmt_pnl(daily_pnl)}")

    except Exception:
        log.exception("Trading health query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


def _build_pipeline(db, today: datetime) -> list[str]:
    """Forecasts, observations, settlements recorded today."""
    lines = ["Data Pipeline (today UTC)", "-" * 18]
    try:
        today_str = today.strftime("%Y-%m-%d")

        # Observations
        obs = db._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE date(ts) = ?",
            (today_str,),
        ).fetchone()[0]
        lines.append(f"  Observations:  {obs:,}")

        # Forecasts captured
        fc = db._conn.execute(
            "SELECT COUNT(*) FROM model_forecast_log WHERE date = ?",
            (today_str,),
        ).fetchone()[0]
        lines.append(f"  Forecasts:     {fc:,}")

        # Settlements resolved today
        settle_count = db._conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE date(ts) = ?",
            (today_str,),
        ).fetchone()[0]
        lines.append(f"  Settlements:   {settle_count:,} resolved today")

        # Gamma vs METAR split
        gamma_count = db._conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE date(ts) = ? "
            "AND resolution_source = 'gamma'",
            (today_str,),
        ).fetchone()[0]
        metar_count = db._conn.execute(
            "SELECT COUNT(*) FROM settlements WHERE date(ts) = ? "
            "AND resolution_source = 'metar'",
            (today_str,),
        ).fetchone()[0]
        if gamma_count + metar_count > 0:
            lines.append(f"    Gamma: {gamma_count}, METAR: {metar_count}")

    except Exception:
        log.exception("Pipeline health query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


def _build_guardrails(db, today: datetime) -> list[str]:
    """Cap, correction, forced-exit, entry-guard event counts."""
    lines = ["Guardrails (last 24h)", "-" * 14]
    try:
        since = (today - timedelta(hours=24)).isoformat()

        cap_cnt = db._conn.execute(
            "SELECT COUNT(*) FROM guardrail_events "
            "WHERE event_type='cap_applied' AND ts >= ?",
            (since,),
        ).fetchone()[0]
        corr_cnt = db._conn.execute(
            "SELECT COUNT(*) FROM guardrail_events "
            "WHERE event_type='correction_applied' AND ts >= ?",
            (since,),
        ).fetchone()[0]
        forced_cnt = db._conn.execute(
            "SELECT COUNT(*) FROM trades "
            "WHERE close_reason IN ('forced_exit','stop_loss') AND ts >= ?",
            (since,),
        ).fetchone()[0]
        entry_cnt = db._conn.execute(
            "SELECT COUNT(*) FROM guardrail_events "
            "WHERE event_type='entry_guard_block' AND ts >= ?",
            (since,),
        ).fetchone()[0]

        lines.append(
            f"  Cap: {cap_cnt:>5}   |  Correction: {corr_cnt:>5}   |  "
            f"Forced exits: {forced_cnt:>5}   |  Entry blocks: {entry_cnt:>5}"
        )
    except Exception:
        log.exception("Guardrail query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


def _ladder_mass(since_date: str) -> "dict[str, float] | None":
    """Ladder mass conservation over ``bracket_evals`` -- the gate's own source.

    **The check whose absence cost two weeks.** #917 (degF ladders integrated
    at half width, sum 0.53) and #920 (truncation without renormalisation, sum
    0.80) both reached production and survived for weeks. Both were visible in
    a single number -- SUM(p_yes_raw) over a ladder -- from the day they
    landed, and nothing was watching it.

    **Reads ``bracket_evals``, deliberately not ``scan_decisions``.** Three
    successive attempts to make the upsert table work all failed, and the
    reason is structural rather than a query bug: ``scan_decisions`` keeps one
    row per (station, ticker, date), so once a market stops being written to,
    its row sits there indefinitely carrying whatever it last held. On
    2026-08-06 it froze KORD's 08-06 ladder at the 11:39 poll while
    ``bracket_evals`` had 12:00 through 15:00, all conserving. No grouping,
    coherence guard or clock filter recovers a snapshot the table no longer
    holds.

    ``bracket_evals`` is append-only and hourly-deduped (#826): one coherent
    ladder per snapshot, never overwritten. It is also **what the M3 gate
    scores**, so a monitor built on it measures the population the verdict will
    actually be computed over -- which is the property that matters, and the
    one the previous versions kept failing to have.

    Ladders are keyed by (station, poll_ts, settlement_date, is_next_day),
    matching ``m3_window_diagnostics``. ``is_next_day`` belongs in the key
    because a station can carry a same-day and a next-day ladder in the same
    poll; pooling them sums two distributions and manufactures a mass excess
    that is not there.

    Returns ``None`` when there is nothing to measure -- absent data must not
    read as a healthy ladder.
    """
    try:
        from src.scripts.bss_market_vs_model_report import load_bracket_eval_rows
        rows = load_bracket_eval_rows()
    except Exception:
        log.exception("bracket_evals read failed")
        return None

    groups: "dict[tuple, list[dict]]" = {}
    for r in rows:
        ts = r.get("ts") or ""
        if ts[:10] < since_date:
            continue
        if r.get("p_yes_raw") is None:
            continue
        key = (r.get("station"), ts, (r.get("end_date") or "")[:10],
               r.get("is_next_day_flag"))
        groups.setdefault(key, []).append(r)

    # A ladder missing an open-ended tail bracket cannot reach 1.0 however
    # correct the arithmetic -- the market offered nowhere for that mass to go.
    # Excusing it is not optional politeness: next-day markets are listed
    # incrementally, so judging them fires most mornings, and a daily email
    # that cries wolf is a daily email nobody reads. Same rule and the same
    # implementation as m3_window_diagnostics, so the two agree by construction
    # rather than by two people remembering to keep them in step.
    #
    # Lazy import: m3_window_diagnostics imports M3_CLEAN_DATA_CLOCK_START from
    # this module, so a top-level import here would be circular.
    from src.scripts.m3_window_diagnostics import MASS_LOW, ladder_stats

    totals, n_excused = [], 0
    for brackets in groups.values():
        # A part-listed ladder is not evidence of leaking mass; require enough
        # brackets that the sum means something.
        if len(brackets) < 5:
            continue
        stats = ladder_stats(brackets)
        if stats["mass"] is None:
            continue
        if stats["coverage_limited"] and stats["mass"] < MASS_LOW:
            n_excused += 1
            continue
        totals.append(stats["mass"])
    if not totals:
        return None
    return {
        "mean": sum(totals) / len(totals),
        "n_excused": n_excused,
        # The worst ladder matters more than the mean: a defect confined to one
        # station or ladder shape is exactly what a mean hides, which is how
        # #917 stayed invisible while degC ladders looked fine.
        "worst": min(totals),
        "n_ladders": len(totals),
        "n_deficient": sum(1 for t in totals if t < 0.90),
    }


#: WARN when the high rail sits at or above this fraction of its structural
#: ceiling (``1 / ladder_size`` -- see ``post_fix_model_health
#: .structural_high_rail_ceiling``). 50% is a judgment call, not a measured
#: baseline -- unlike the ceiling itself, no population gives a "correct"
#: cutoff between "far below" and "at" the ceiling. It is deliberately loose:
#: the ceiling comparison is the population-robust half of the old metric, so
#: this only needs to catch a share that is clearly *trending toward* maximal
#: overconfidence, not pin an exact number. Verified against production
#: (2026-08-10, clean window): actual 1.1% / ceiling 9.1% = 12% -> [OK].
HIGH_RAIL_WARN_RATIO = 0.5


def _rail_and_artifact(since_date: str) -> "dict | None":
    """High-rail-vs-structural-ceiling and interior-zero-gap indicators.

    Reuses ``post_fix_model_health``'s own computation over ``bracket_evals``
    -- the gate's population, same source as ``_ladder_mass`` above -- rather
    than a second implementation (issue #969). The version this replaces
    queried ``scan_decisions`` and thresholded a FULL bracket ladder (mostly
    far-out-of-the-money brackets that legitimately sit near a rail) against
    ``rail_pct < 20`` / ``zero_pct < 10`` -- Pass-1's GATE-SELECTED baselines.
    A full ladder can essentially never clear ``rail_pct < 20``: an 11-bracket
    ladder has at most one bracket that can resolve YES, so a ~58% low-rail
    share is the CORRECT behaviour, not a fault. That produced an
    unconditional daily ``[WARN]`` that disagreed with ``post_fix_model_health``
    on the same data. See that module's docstring and ``BASELINE_*``
    constants for the full population-mismatch account.

    Also conflates the two rails: ``post_fix_model_health`` is explicit that
    only the HIGH rail is population-robust (it has a computable ceiling,
    ``1 / ladder_size``, because a station-day has exactly one daily high);
    the low rail is population-driven and not comparable across ladders. This
    reports the high rail against its ceiling and drops the low-rail sum.

    The raw ``p_yes_raw == 0.0`` share is likewise population-confounded --
    most exact zeros on a full ladder are the envelope correctly pricing an
    unreachable bracket (see ``post_fix_model_health.zero_artifact_rate``'s
    docstring for its own "two earlier, wrong versions" account). The
    structural invariant that replaced it -- an exact zero with non-zero
    brackets on BOTH sides in sorted bracket order, which no finite envelope
    can produce -- needs no baseline and no population assumption, so that is
    what is surfaced here instead.

    Returns ``None`` when there is nothing to measure -- absent data must not
    read as healthy (mirrors ``_ladder_mass``).
    """
    try:
        from src.scripts.bss_market_vs_model_report import load_bracket_eval_rows
        raw_rows = load_bracket_eval_rows()
    except Exception:
        log.exception("bracket_evals read failed")
        return None

    # Poll time, not settlement date -- matching `_ladder_mass` above.
    # Contamination is a property of when the probability was computed, not
    # when the market resolved (#941); a bracket polled before the clock
    # start but settling after it must still be excluded.
    rows = [r for r in raw_rows if (r.get("ts") or "")[:10] >= since_date]
    if not rows:
        return None

    from src.scripts.post_fix_model_health import (
        interior_zero_violations, rail_concentration, structural_high_rail_ceiling)

    # post_fix_model_health's checks read `settlement_date` / `poll_ts`;
    # `bss_market_vs_model_report.load_bracket_eval_rows` renames those to
    # `end_date` / `ts` (see its own docstring). Map the field names rather
    # than forking the computation to match this caller.
    mapped = [{**r, "settlement_date": r.get("end_date"), "poll_ts": r.get("ts")}
              for r in rows]

    rails = rail_concentration(mapped)
    if not rails["n"]:
        return None
    ceiling = structural_high_rail_ceiling(mapped)
    inv = interior_zero_violations(mapped)
    return {
        "n": rails["n"],
        "high_rail_share": rails["high_rail_share"],
        "middle_share": rails["middle_share"],
        "ceiling": ceiling,
        "n_violations": inv["n_violations"],
        "n_ladders_checked": inv["n_ladders_checked"],
        "violation_rate": inv["violation_rate"],
    }


def _scoreable_station_days(since_date: str) -> int:
    """Count of distinct scoreable station-days -- see ``_scoreable_pairs``."""
    return len(_scoreable_pairs(since_date))


def _marginal_accrual(pairs: "set[tuple[str, str]]",
                      today: "str | None" = None) -> int:
    """Station-days gained per additional COLLECTING DAY, from SETTLED dates.

    Not ``total / elapsed``: the first day opens two settlement dates at once
    (same-day and next-day together), so an average is inflated by it forever
    and only decays toward the truth.

    And not ``max`` over settlement dates either, which is what this did until
    2026-08-12. A settlement date's contribution SHRINKS as it settles -- its
    final polls are its most certain ones, so more brackets land on zero or the
    rail and are excluded, and de-duplication keeps the last poll. Measured:

        2026-08-06  16    2026-08-10  15
        2026-08-07  16    2026-08-11  15
        2026-08-08  16    2026-08-12  16
        2026-08-09  18    2026-08-13  28   <- not settled; max picked this
                          2026-08-14   1

    ``max`` locks onto the newest, least mature date and reported +28/day
    against a steady state of 16 -- projecting the 300 bar at 2026-08-18
    instead of ~2026-08-24, i.e. inviting the gate to run a week early on
    roughly 230 station-days.

    So: the MEDIAN over settlement dates that have finished collecting, which
    is every date before today. Median rather than mean because a single
    outage-thinned date should not drag the estimate (2026-08-11 collected 15
    against a normal 16). Dates from today onward are still accumulating polls
    and are excluded -- the same reasoning that excluded the partial current
    day before, applied to the right axis.
    """
    if not pairs:
        return 0
    if today is None:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    per_settlement: "dict[str, int]" = {}
    for _station, settle in pairs:
        per_settlement[settle] = per_settlement.get(settle, 0) + 1

    settled = sorted(n for d, n in per_settlement.items() if d < today)
    if not settled:
        # Nothing has finished collecting yet -- fall back to the widest date
        # rather than reporting zero, but this is an early-window estimate.
        return max(per_settlement.values())
    mid = len(settled) // 2
    return (settled[mid] if len(settled) % 2
            else (settled[mid - 1] + settled[mid]) // 2)


def _scoreable_pairs(since_date: str,
                     rows: "list[dict] | None" = None) -> "set[tuple[str, str]]":
    """Distinct scoreable ``(station, settlement_date)`` pairs -- the 300 bar.

    **The exclusion set is the gate's, verbatim** -- shared predicates, not a
    re-matched list (see below). The UNIT counted is not verbatim: the gate
    (``apply_exclusions``) filters and counts ROWS; this counts distinct
    ``(station, settlement_date)`` PAIRS after de-duplicating each pair to its
    last poll. That is::

        {(station, settlement_date) for r in last_poll_per_pair
         if not excluded(r)}

    Distinct ``(station, settlement_date)`` pairs across the WHOLE window, not
    per poll day. That distinction is not pedantry. A settlement date is polled
    both as next-day and as same-day, so a per-poll-day count summed over days
    double-counts it -- which produced a "113/300, gate on 08-11" reading
    against a true 85/300 and a mid-August date.

    Reads ``bracket_evals`` for the same reason ``_ladder_mass`` does: it is
    append-only and is what the gate scores, while ``scan_decisions`` upserts
    on (station, ticker, date) and holds whatever a market last wrote. This
    count used to read ``scan_decisions`` even after #943 moved the mass check
    off it -- the two sat eight lines apart, disagreeing.

    Mirrors the gate's row exclusions -- missing prices, and every predicate
    in ``bss_market_vs_model_report.PRICE_EXCLUSION_PREDICATES`` (currently:
    exact-zero model probabilities, the fabricated-50/50 placeholder, and
    rail-clipped market prices) -- by iterating that SAME tuple
    ``apply_exclusions`` builds its cascade from, rather than naming
    individual predicates here. A predicate added to that tuple is picked up
    by this function automatically; this count cannot silently re-diverge
    from the gate's cascade the way it did the three times documented above
    (#1030), and a fourth time when #1028 added the fabricated-50/50
    exclusion to ``apply_exclusions`` only (#1035). The residual risk: a
    predicate added to ``apply_exclusions``'s inline cascade WITHOUT also
    being added to ``PRICE_EXCLUSION_PREDICATES`` still diverges silently --
    sharing the enumeration only closes the gap for predicates that go
    through it.

    One legitimate difference from ``apply_exclusions`` remains, and it is
    NOT a divergence to fix: this function counts distinct ``(station,
    settlement_date)`` PAIRS after de-duplicating to each pair's last poll,
    while ``apply_exclusions`` filters ROWS (every poll of every bracket).
    The exclusion PREDICATES are shared; the unit counted is not, because the
    300 bar and the gate's scored population answer different questions.
    """
    from src.scripts.bss_market_vs_model_report import (
        PRICE_EXCLUSION_PREDICATES, load_bracket_eval_rows)
    if rows is None:
        try:
            rows = load_bracket_eval_rows()
        except Exception:
            log.exception("bracket_evals read failed")
            return set()

    # DE-DUPLICATE FIRST, THEN EXCLUDE -- the gate's order, and the order is
    # the whole answer. `apply_exclusions` runs after de-duplication, so a
    # bracket-day is judged on the model's FINAL word about it: a bracket that
    # was contested at 09:00 and exact-zero by the last poll is excluded.
    #
    # Counting the other way round -- exclude every row, then take distinct
    # pairs -- keeps any bracket-day that was ever contested at any poll, and
    # on 2026-08-10 that read 173 against a true 111. Over half the count was
    # bracket-days the gate would drop.
    #
    # This is the third correction to this number. It first read scan_decisions
    # (an upsert table), then bracket_evals with the right unit but no
    # de-duplication, and now the gate's actual sequence. Each pass matched one
    # property of the funnel and missed another, so the invariant worth holding
    # on to is: mirror `bss_market_vs_model_report`'s pipeline, in its order.
    last_poll: "dict[tuple, tuple[str, dict]]" = {}
    for r in rows:
        ts = r.get("ts") or ""
        if ts[:10] < since_date:
            continue
        key = (r.get("station"), (r.get("end_date") or "")[:10], r.get("ticker"))
        prev = last_poll.get(key)
        if prev is None or ts > prev[0]:
            last_poll[key] = (ts, r)

    pairs: "set[tuple[str, str]]" = set()
    for (station, settle, _ticker), (_ts, r) in last_poll.items():
        if r.get("p_yes_raw") is None or r.get("yes_ask") is None or r.get("no_ask") is None:
            continue
        if any(pred(r) for pred in PRICE_EXCLUSION_PREDICATES):
            continue
        if station and settle:
            pairs.add((station, settle))
    return pairs


def _build_m3_progress(db, today: datetime) -> "tuple[list[str], dict]":
    """M3 station-day accrual and leading indicators.

    Returns ``(lines, flags)``. The section carries THREE independently
    WARN-able indicators -- ladder mass, high rail vs. structural ceiling,
    and interior-zero gaps -- so ``flags`` reports each one under its own
    key (``mass_status`` / ``rail_status`` / ``gaps_status``, each
    ``OK / WARN / UNKNOWN``) instead of a single section-wide marker.
    ``_build_verdict`` reads these directly so a WARN on one indicator is
    never misattributed to another (issue #972: a bare ``"[WARN]" in
    m3_text`` scan named every WARN in this section "artifact rate high",
    even when it was actually the mass or rail indicator that tripped).
    """
    lines = ["M3 Progress", "-" * 10]
    flags: dict = {
        "mass_status": "UNKNOWN",
        "rail_status": "UNKNOWN",
        "gaps_status": "UNKNOWN",
    }
    try:
        since = (today - timedelta(hours=24)).isoformat()

        # Mass conservation FIRST: it qualifies everything below it. A
        # station-day counted while ladders are leaking mass is a contaminated
        # station-day, and reporting the count above the health check is what
        # let "121% -- power bar met" mean nothing on 2026-08-04.
        # Never earlier than the clean-data clock: ladders polled before it
        # came from a pre-#920 model and would warn forever.
        mass = _ladder_mass(max(since[:10], M3_CLEAN_DATA_CLOCK_START))
        if mass is None:
            lines.append("  Ladder mass:   (no ladders in 24h -- cannot assess)")
            mass_ok = False
            flags["mass_status"] = "UNKNOWN"
        else:
            mass_ok = 0.95 <= mass["mean"] <= 1.05 and mass["n_deficient"] == 0
            flags["mass_status"] = "OK" if mass_ok else "WARN"
            lines.append(
                f"  Ladder mass:   {mass['mean']:.3f} mean, {mass['worst']:.3f} worst "
                f"({mass['n_deficient']}/{mass['n_ladders']} deficient) "
                f"{'[OK]' if mass_ok else '[WARN]'}"
            )
            if mass.get("n_excused"):
                lines.append(f"                 ({mass['n_excused']} short ladder(s) "
                             "excused -- no open-ended tail bracket)")
            if not mass_ok:
                lines.append("                 ^ probabilities are leaking mass "
                             "(cf. #917, #920) -- station-days below are NOT clean")

        # Station-days accrue from the CLEAN-DATA CLOCK, which has moved twice
        # as probability defects landed mid-window (#917 on 2026-08-01, #920 on
        # 2026-08-05). Counting from an out-of-date start is not a cosmetic
        # error: on 2026-08-04 this reported "362/300 (121%)" -- i.e. run the
        # gate -- against a window that was 100% contaminated.
        clock_start = M3_CLEAN_DATA_CLOCK_START
        pairs = _scoreable_pairs(clock_start)
        station_days = len(pairs)

        pct = station_days / 300 * 100
        # MARGINAL, not average. An average is inflated forever by the opening
        # day, which opens two settlement dates at once -- it read 42/day and
        # then 38/day against a measured 28, which is three days of optimism
        # on the date the gate gets planned around. Erring early is the bad
        # direction: it invites running the gate before it can decide anything.
        rate = _marginal_accrual(pairs)
        days_to_bar = (max(0, -(-(300 - station_days) // rate)) if rate > 0
                       else None)

        lines.append(f"  Station-days:  {station_days}/300 ({pct:.0f}%) "
                     f"scoreable, since {clock_start}")
        # Outcome resolution is not done here -- it needs Gamma/METAR and this
        # runs daily by email. The gate counts only station-days whose outcomes
        # RESOLVED, which on 2026-08-10 was 81 against 111 here, so this is an
        # upper bound and the bar is reached later than it implies.
        lines.append("                 ^ pre-resolution upper bound; the gate "
                     "scores only resolved station-days (~25-30% fewer)")
        if days_to_bar is None:
            lines.append("  Accrual rate:  no scoreable rows yet -- bar date unknown")
        else:
            lines.append(f"  Accrual rate:  ~{rate:.0f}/day -> power bar "
                         f"~{today.strftime('%b %d')} + {days_to_bar}d")
        if station_days >= 300 and not mass_ok:
            lines.append("                 ^ bar met on COUNT only -- ladder mass is "
                         "not clean, so the gate must NOT run")

        # Leading indicators: high rail vs. its structural ceiling, and
        # interior-zero ladder-gap violations -- both computed from
        # `bracket_evals` (the gate's population, same source and clean-data
        # clock as the ladder-mass check above) by reusing
        # `post_fix_model_health`'s own functions. See `_rail_and_artifact`
        # for why this replaced a `scan_decisions` query thresholded against
        # gate-selected Pass-1 baselines (issue #969).
        indicators = _rail_and_artifact(clock_start)

        if indicators is None:
            # No data reading as perfect health is the same failure the
            # ladder-mass check above exists to prevent -- say there is
            # nothing to measure instead.
            lines.append("  Rail / artifact: (no brackets in 24h -- cannot assess)")
            lines.append("")
            return lines, flags

        ceiling = indicators["ceiling"]
        high_rail = indicators["high_rail_share"]
        if ceiling:
            ratio = (high_rail / ceiling) if high_rail is not None else None
            rail_ok = ratio is not None and ratio < HIGH_RAIL_WARN_RATIO
            flags["rail_status"] = "OK" if rail_ok else "WARN"
            lines.append(
                f"  High rail (bracket_evals, >= 0.95): {_fmt_pct(high_rail)} vs "
                f"{_fmt_pct(ceiling)} structural ceiling "
                f"({ratio:.0%} of it) {'[OK]' if rail_ok else '[WARN]'}"
            )
        else:
            # Ladder size undeterminable -- no ceiling to compare against.
            # Cannot judge, so do not print a pass/fail tag either, and do
            # not treat it as OK for the verdict -- it was simply not
            # evaluated.
            flags["rail_status"] = "UNKNOWN"
            lines.append(f"  High rail (bracket_evals, >= 0.95): {_fmt_pct(high_rail)} "
                         f"(ladder size unknown -- no ceiling to compare against)")

        n_violations = indicators["n_violations"]
        n_ladders_checked = indicators["n_ladders_checked"]
        zero_ok = n_violations == 0
        flags["gaps_status"] = "OK" if zero_ok else "WARN"
        lines.append(
            f"  Interior-zero gaps: {n_violations} of {n_ladders_checked} ladders "
            f"({_fmt_pct(indicators['violation_rate'])}) {'[OK]' if zero_ok else '[WARN]'}"
        )

    except Exception:
        log.exception("M3 progress query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines, flags


def _build_emos_progress(db, today: datetime) -> list[str]:
    """EMOS shadow status, CRPS comparison, promotion readiness."""
    lines = ["EMOS Status", "-" * 10]
    try:
        # Get list of cities with EMOS shadow coefficients
        city_rows = db._conn.execute(
            "SELECT DISTINCT city FROM emos_calibration WHERE model_mode='emos_shadow'"
        ).fetchall()
        cities = [r[0] for r in city_rows]

        if not cities:
            lines.append("  No cities in emos_shadow yet")
            lines.append("")
            return lines

        # Aggregate CRPS comparison (emos_shadow vs legacy model_mode)
        shadow_row = db._conn.execute(
            "SELECT AVG(crps_score), COUNT(*) "
            "FROM emos_crps_log WHERE model_mode='emos_shadow'"
        ).fetchone()
        legacy_row = db._conn.execute(
            "SELECT AVG(crps_score), COUNT(*) "
            "FROM emos_crps_log WHERE model_mode='legacy'"
        ).fetchone()
        if shadow_row and shadow_row[0] is not None:
            shadow_crps, n_shadow = shadow_row
            legacy_crps, n_legacy = legacy_row if legacy_row else (None, 0)
            delta = (legacy_crps or 0) - (shadow_crps or 0)  # positive = EMOS better
            arrow = "OK" if delta > 0 else "watch"
            lines.append(f"  Shadow CRPS:    {shadow_crps:.2f} vs legacy {legacy_crps:.2f} ({arrow} d={delta:+.2f})")

        # Top 5 cities by CRPS sample count (closest to promotion)
        sample_rows = []
        for (city,) in city_rows:
            cnt = db.get_emos_crps_count(city)
            sample_rows.append((city, cnt))
        sample_rows.sort(key=lambda x: -x[1])

        lines.append(f"  Cities:         {len(cities)} in shadow")
        lines.append("  Top 5 by samples:")
        for city, cnt in sample_rows[:5]:
            remaining = max(0, EMOS_MIN_SAMPLES_PROMOTION - cnt)
            status = "READY" if remaining == 0 else f"{remaining}d to promo"
            lines.append(f"    {city:<20} {cnt:>3}/{EMOS_MIN_SAMPLES_PROMOTION}  {status}")

    except Exception:
        log.exception("EMOS progress query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


def _build_copy_stuck(db, today: datetime) -> "tuple[list[str], dict]":
    """Copy-trading paper positions escalated by ``copy_settle`` (issue #1301):
    PAST_END_UNRESOLVED, CLOSED_AMBIGUOUS, UNRETRIEVABLE. Healthy long-dated
    open markets (NOT_DUE) are only counted, never called stuck. Lists every
    escalated position with id, wallet, market, age and entry price."""
    from src.data.copy_stuck import LEVEL_LABELS, STUCK_HOURS, age_hours
    lines = ["Copy-trading stuck positions", "-" * 28]
    flags: dict = {"n_stuck": 0, "n_urgent": 0}
    try:
        open_rows = db.get_open_copy_positions()
        stuck = sorted(
            (r for r in open_rows if int(r.get("stuck_level") or 0) >= 1),
            key=lambda r: (-int(r["stuck_level"]), r["id"]),
        )
        n_not_due = 0
        trading: list = []  # PAST_END_TRADING (#1388): level 0, listed, never WARNs
        for r in open_rows:
            try:
                if int(r.get("stuck_level") or 0) == 0 and r.get("stuck_category") == "PAST_END_TRADING":
                    trading.append(r)
                elif int(r.get("stuck_level") or 0) == 0 and age_hours(r["entry_ts"], today) > STUCK_HOURS:
                    n_not_due += 1
            except (ValueError, KeyError, TypeError):
                pass
        flags["n_stuck"] = len(stuck)
        flags["n_urgent"] = sum(1 for r in stuck if int(r["stuck_level"]) >= 2)
        stake = sum(float(r["stake_usd"]) for r in stuck)
        lines.append(
            f"  Escalated: {len(stuck)} (urgent: {flags['n_urgent']})  |  "
            f"stake unresolved: ${stake:.2f}  |  "
            f"open >48h but not due (long-dated, not stuck): {n_not_due}  |  "
            f"past endDate but still trading (not escalated): {len(trading)}"
        )
        for r in sorted(trading, key=lambda r: r["id"]):
            lines.append(
                f"  [PAST_END_TRADING] id={r['id']} wallet={r['address']} market={r['market']} "
                f"entry={float(r['entry_price']):.3f} stake=${float(r['stake_usd']):.2f}"
            )
        if stuck:
            lines.append(
                "  [WARN] escalated positions are EXCLUDED from ROI/win-rate; operator: "
                "python -m src.scripts.copy_abandon (--settle-at-price 0.5 for a verified 50/50 void)"
            )
        for r in stuck:
            lines.append(
                f"  [{r.get('stuck_category') or '?'}/{LEVEL_LABELS[int(r['stuck_level'])]}] "
                f"id={r['id']} wallet={r['address']} market={r['market']} "
                f"age={age_hours(r['entry_ts'], today) / 24:.1f}d "
                f"entry={float(r['entry_price']):.3f} stake=${float(r['stake_usd']):.2f}"
            )
    except Exception:
        log.exception("Copy stuck-position query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines, flags


#: A live ``pending`` row older than this is stale (issue #1393): a healthy
#: order resolves to filled/rejected within the executor's fill-wait (seconds
#: to minutes) plus one settle cycle, so 2h means a crash mid-cycle.
LIVE_PENDING_STALE_HOURS = 2


def _build_copy_live_stuck(db, today: datetime) -> "tuple[list[str], dict]":
    """Live copy-trading stuck positions (issue #1393, design on #1365).

    READ-ONLY. Reads ``stuck_level``/``stuck_category``/``stuck_first_seen_at``
    written by ``copy_live_settle`` (#1392). Lists escalated ``filled``/
    ``partial`` rows; ``pending`` rows older than ``LIVE_PENDING_STALE_HOURS``
    and unresolved ``cancel_failed_ghost`` rows are separate alert-only
    categories (no status/P&L/exposure change). The exposure line reuses
    ``get_open_copy_live_positions`` exactly as ``copy_signal_loop`` does.
    """
    from src.config import get_live_config
    from src.data.copy_stuck import LEVEL_LABELS, age_hours
    lines = ["Live copy-trading stuck positions", "-" * 33]
    flags: dict = {"n_stuck": 0, "n_urgent": 0, "n_pending_stale": 0, "n_ghost": 0}
    try:
        open_rows = db.get_open_copy_live_positions()
        stuck = sorted(
            (r for r in open_rows if r["status"] in ("filled", "partial")
             and int(r.get("stuck_level") or 0) >= 1),
            key=lambda r: (-int(r["stuck_level"]), r["id"]),
        )
        stale_pending = []
        for r in open_rows:
            try:
                if r["status"] == "pending" and age_hours(r["entry_ts"], today) > LIVE_PENDING_STALE_HOURS:
                    stale_pending.append(r)
            except (ValueError, KeyError, TypeError):
                pass
        ghosts = sorted(db.get_ghost_order_positions(), key=lambda r: r["id"])
        flags["n_stuck"] = len(stuck)
        flags["n_urgent"] = sum(1 for r in stuck if int(r["stuck_level"]) >= 2)
        flags["n_pending_stale"] = len(stale_pending)
        flags["n_ghost"] = len(ghosts)
        stake = sum(float(r["stake_usd"]) for r in stuck)
        lines.append(
            f"  Unresolved: {len(stuck)} (urgent: {flags['n_urgent']})  |  "
            f"stake unresolved: ${stake:.2f}  |  "
            f"stale pending (>{LIVE_PENDING_STALE_HOURS}h): {len(stale_pending)}  |  "
            f"unresolved cancel_failed_ghost: {len(ghosts)}"
        )
        exposure = sum(float(p["stake_usd"]) for p in open_rows)
        cap = float(get_live_config(db)["COPY_LIVE_MAX_TOTAL_EXPOSURE_USD"])
        lines.append(
            f"  Unsettled live exposure counted: ${exposure:.2f} of cap ${cap:.2f} "
            f"({len(open_rows)} open row(s); ghosts are NOT counted)"
        )
        if exposure > cap:
            lines.append("  [WARN] counted live exposure is above COPY_LIVE_MAX_TOTAL_EXPOSURE_USD")
        if stuck:
            lines.append(
                "  [WARN] stuck live rows still count as exposure and are invisible to "
                "the breaker; alert-only -- no live abandon path exists (#1365)"
            )
        for r in stuck:
            lines.append(
                f"  [{r.get('stuck_category') or '?'}/{LEVEL_LABELS.get(int(r['stuck_level']), '?')}] "
                f"id={r['id']} wallet={r['address']} market={r['market']} "
                f"age={age_hours(r['entry_ts'], today) / 24:.1f}d "
                f"status={r['status']} stake=${float(r['stake_usd']):.2f}"
            )
        for r in stale_pending:
            lines.append(
                f"  [STALE_PENDING] id={r['id']} wallet={r['address']} market={r['market']} "
                f"age={age_hours(r['entry_ts'], today):.1f}h stake=${float(r['stake_usd']):.2f}"
            )
        for r in ghosts:
            lines.append(
                f"  [GHOST_UNRESOLVED] id={r['id']} wallet={r['address']} market={r['market']} "
                f"order={r['order_id']} age={age_hours(r['entry_ts'], today):.1f}h "
                f"stake=${float(r['stake_usd']):.2f} (may be a real fill)"
            )
    except Exception:
        log.exception("Live copy stuck-position query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines, flags


def _copy_skip_counts(db, now: datetime) -> "dict":
    """Per-wallet counts: ``{address: {"placed": n, "skipped": {reason: (window, baseline_total)}}}``.

    Lives here, not in src/data, because the db-lock guard forbids `_conn`
    reads outside db.py/scripts. Read-only; one grouped query over ``idx_copy_signals_address_detected``.
    """
    win_start = (now - timedelta(hours=_SKIP_WINDOW_HOURS)).isoformat()
    base_start = (now - timedelta(hours=_SKIP_WINDOW_HOURS, days=_SKIP_BASELINE_DAYS)).isoformat()
    with db._lock:
        rows = db._conn.execute(
            "SELECT address, order_placed, skip_reason, "
            "SUM(detected_at >= ?) AS n_win, SUM(detected_at < ?) AS n_base "
            "FROM copy_signals WHERE detected_at >= ? "
            "GROUP BY address, order_placed, skip_reason",
            (win_start, win_start, base_start),
        ).fetchall()
    out: dict = {}
    for r in rows:
        w = out.setdefault(r["address"], {"placed": 0, "skipped": {}})
        if r["order_placed"]:
            w["placed"] += int(r["n_win"] or 0)
        elif r["skip_reason"]:
            w["skipped"][r["skip_reason"]] = (int(r["n_win"] or 0), int(r["n_base"] or 0))
    return out


def _build_copy_skips(db, today: datetime) -> "tuple[list[str], dict]":
    """Copy-signal skip reasons per wallet over the last 24h (issue #1314).

    Counts only. Trend basis: 24h count vs the prior 7-day daily average
    (NEW / SPIKE / steady). STRUCTURAL reasons (e.g. non_binary_outcome_index:
    the wallet trades markets this engine cannot represent) are separated from
    routine gate rejections. Each wallet line states placed vs skipped so the
    paper-performance denominator's incompleteness is visible."""
    from src.data.copy_skip_reasons import (
        STRUCTURAL, UNKNOWN, accepted_ref, classify, trend,
    )
    lines = ["Copy-trading skipped signals (last 24h)", "-" * 38]
    flags: dict = {"n_structural": 0, "n_unknown": 0, "n_spike": 0, "n_accepted": 0}
    try:
        data = _copy_skip_counts(db, today)
        shown = False
        for addr in sorted(data):
            w = data[addr]
            window = {k: v[0] for k, v in w["skipped"].items() if v[0] > 0}
            if not window:
                continue
            shown = True
            lines.append(f"  {addr}: placed={w['placed']} skipped={sum(window.values())}")
            for reason, n in sorted(window.items(), key=lambda kv: (-kv[1], kv[0])):
                cls = classify(reason)
                t = trend(n, w["skipped"][reason][1])
                avg = w["skipped"][reason][1] / _SKIP_BASELINE_DAYS
                # Reviewed allow-list (#1390): accepted only while STEADY.
                ref = accepted_ref(addr, reason) if t == "STEADY" else None
                warn = cls in (STRUCTURAL, UNKNOWN) or t != "STEADY"
                mark = "[WARN] " if warn and not ref else ""
                note = f" accepted ({ref})" if ref else ""
                lines.append(
                    f"    {mark}{reason:<28} {n:>4}  {cls:<10} {t} (prior {_SKIP_BASELINE_DAYS}d avg {avg:.1f}/day){note}"
                )
                if ref:
                    flags["n_accepted"] += n
                elif cls == STRUCTURAL:
                    flags["n_structural"] += n
                if cls == UNKNOWN:
                    flags["n_unknown"] += n
                if t != "STEADY":
                    flags["n_spike"] += 1
        if not shown:
            lines.append("  no skipped signals in the last 24h")
        elif flags["n_structural"]:
            lines.append(
                "  [WARN] STRUCTURAL skips = wallet trades markets this engine cannot "
                "represent; its paper record excludes them (see docs/OPERATIONS.md)"
            )
    except Exception:
        log.exception("Copy skip-reason query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines, flags


def _build_param_change_impact(db, today: datetime) -> list[str]:
    """Shadow PnL/win-rate since the entry-gate widening experiment landed.

    Three cuts, in order: the whole shadow book since the change (context),
    the specific NO/p_yes-0.05-0.10 pocket the gate change itself opened up
    (the thing actually being tested), and the 7 emos_primary-promoted
    cities (a *different* change that landed the same day) -- kept separate
    so a swing in one is never misread as evidence about the other.
    """
    lines = ["Entry-Gate Widening Experiment (since 2026-10-11)", "-" * 50]
    try:
        since = PARAM_CHANGE_DATE

        overall = db._conn.execute(
            "SELECT side, COUNT(*), "
            "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), "
            "COALESCE(SUM(pnl), 0) "
            "FROM trades WHERE mode='shadow' AND direction='high' AND ts >= ? "
            "GROUP BY side",
            (since,),
        ).fetchall()
        if not overall:
            lines.append("  No shadow trades since the change yet.")
            lines.append("")
            return lines

        total_trades = total_settled = 0
        total_pnl = 0.0
        for side, n, settled, wins, losses, pnl in overall:
            total_trades += n
            total_settled += settled or 0
            total_pnl += pnl or 0
            wr = (wins / (wins + losses)) if (wins or losses) else None
            pnl_display = pnl if settled else None
            lines.append(
                f"  {side:<4} {n:>4} trades, {settled or 0:>4} settled, "
                f"win% {_fmt_pct(wr)} ({wins or 0}W/{losses or 0}L), {_fmt_pnl(pnl_display)}"
            )
        pending = total_trades - total_settled
        lines.append(f"  TOTAL {total_trades} trades, {total_settled} settled "
                     f"({pending} pending -- settlement lags ~1-2 days), {_fmt_pnl(total_pnl)}")

        # The specific pocket MAX_CONFIDENCE_YES_FOR_NO=0.10 opened: NO-side
        # entries at p_yes in (0.05, 0.10] that the old 0.05 ceiling would
        # have rejected outright before this change. This is the one number
        # that directly answers "is the parameter change itself paying off".
        pocket = db._conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), "
            "COALESCE(SUM(pnl), 0), "
            "SUM(COALESCE(is_next_day, 0)) "
            "FROM trades WHERE mode='shadow' AND side='NO' AND ts >= ? "
            "AND p_yes_raw > 0.0501 AND p_yes_raw <= 0.10",
            (since,),
        ).fetchone()
        lines.append("")
        if pocket and pocket[0]:
            n, settled, wins, losses, pnl, next_day_n = pocket
            wr = (wins / (wins + losses)) if (wins or losses) else None
            pnl_display = pnl if settled else None
            lines.append(
                f"  Widened pocket (NO, p_yes 0.05-0.10): {n} trades "
                f"({next_day_n} next-day -- unvalidated by the backtest), "
                f"{settled} settled, win% {_fmt_pct(wr)} ({wins}W/{losses}L), "
                f"{_fmt_pnl(pnl_display)}"
            )
        else:
            lines.append("  Widened pocket (NO, p_yes 0.05-0.10): no trades yet")

        # Same-day change, different mechanism: 7 cities promoted to
        # emos_primary. Tracked separately so this section answers both
        # "is the gate change working" and "is the promotion working"
        # without conflating them.
        from src.config import STATIONS, station_city
        promoted_stations = tuple(
            s[0] for s in STATIONS if station_city(s) in EMOS_PRIMARY_PROMOTED_CITIES
        )
        if promoted_stations:
            placeholders = ",".join("?" * len(promoted_stations))
            promo_row = db._conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END), "
                "SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END), COALESCE(SUM(pnl),0) "
                f"FROM trades WHERE mode='shadow' AND ts >= ? "
                f"AND station IN ({placeholders})",
                (since, *promoted_stations),
            ).fetchone()
            n, settled, wins, losses, pnl = promo_row
            wr = (wins / (wins + losses)) if (wins or losses) else None
            pnl_display = pnl if settled else None
            lines.append(
                f"  emos_primary cities (7): {n or 0} trades, {settled or 0} settled, "
                f"win% {_fmt_pct(wr)} ({wins or 0}W/{losses or 0}L), {_fmt_pnl(pnl_display)}"
            )

    except Exception:
        log.exception("Param-change impact query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


def _build_station_breakdown(db, today: datetime) -> list[str]:
    """Per-station shadow PnL/win-rate since the change, alongside each
    city's latest EMOS-vs-legacy CRPS -- so a station's trading edge and its
    underlying forecast quality are read side by side, not as two separate
    reports that have to be mentally merged.

    Sorted by trade count (most data first), not PnL -- this early, one
    settled trade swinging +/-100% is not what should decide a top-10 slot.
    """
    lines = ["Per-Station Breakdown (top 10 by trade count, since 2026-10-11)", "-" * 55]
    try:
        from src.config import STATIONS, station_city
        station_to_city = {s[0]: station_city(s) for s in STATIONS}

        rows = db._conn.execute(
            "SELECT station, COUNT(*), "
            "SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), "
            "COALESCE(SUM(pnl), 0) "
            "FROM trades WHERE mode='shadow' AND direction='high' AND ts >= ? "
            "GROUP BY station ORDER BY COUNT(*) DESC LIMIT 10",
            (PARAM_CHANGE_DATE,),
        ).fetchall()
        if not rows:
            lines.append("  No shadow trades since the change yet.")
            lines.append("")
            return lines

        lines.append(f"  {'Station':<8}{'N':>5}{'Set':>5}{'Win%':>7}{'PnL':>11}"
                     f"{'EMOS CRPS':>11}{'Legacy':>9}")
        for station, n, settled, wins, losses, pnl in rows:
            wr = (wins / (wins + losses)) if (wins or losses) else None
            # COALESCE(SUM(pnl), 0) turns "nothing settled yet" into a
            # literal 0.0, which _fmt_pnl would print as "+EUR0.00" -- that
            # reads as "broke even", not "no data yet". Show N/A instead
            # whenever nothing has actually settled.
            pnl_display = pnl if settled else None
            city = station_to_city.get(station)
            emos_crps = legacy_crps = None
            if city:
                crps_row = db._conn.execute(
                    "SELECT crps_score FROM emos_calibration "
                    "WHERE city=? AND model_mode='emos_shadow' "
                    "ORDER BY trained_at DESC LIMIT 1",
                    (city,),
                ).fetchone()
                emos_crps = crps_row[0] if crps_row else None
                legacy_row = db._conn.execute(
                    "SELECT AVG(crps_score) FROM emos_crps_log "
                    "WHERE city=? AND model_mode='legacy'",
                    (city,),
                ).fetchone()
                legacy_crps = legacy_row[0] if legacy_row else None
            emos_str = f"{emos_crps:.3f}" if emos_crps is not None else "N/A"
            legacy_str = f"{legacy_crps:.3f}" if legacy_crps is not None else "N/A"
            lines.append(
                f"  {station:<8}{n:>5}{settled:>5}{_fmt_pct(wr):>7}"
                f"{_fmt_pnl(pnl_display):>11}{emos_str:>11}{legacy_str:>9}"
            )
    except Exception:
        log.exception("Station breakdown query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


def _running_commit_sha() -> "str | None":
    """Short SHA of the checked-out code (issue #913 item 4)."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=str(Path(__file__).resolve().parent),
        )
        sha = out.stdout.strip()
        return sha if out.returncode == 0 and sha else None
    except Exception:
        return None


def _build_silent_failures(db, today: datetime) -> "tuple[list[str], dict]":
    """Errors / silent failures (24h): scan_decisions write failures (issue
    #913), recorded by run.py as guardrail_events rows, plus the running
    commit SHA so "merged" vs "deployed" is visible. Any non-zero write
    failure sets ``n_write_failed`` which the verdict reports as CRIT."""
    lines = ["Errors / silent failures (24h)", "-" * 30]
    flags: dict = {"n_write_failed": 0, "n_signature_mismatch": 0}
    try:
        since = (today - timedelta(hours=24)).isoformat()
        rows = db._conn.execute(
            "SELECT event_type, COUNT(*), COALESCE(SUM(raw_value), 0) "
            "FROM guardrail_events WHERE ts >= ? AND event_type IN "
            "('scan_decision_write_failed', 'scan_decision_signature_mismatch') "
            "GROUP BY event_type",
            (since,),
        ).fetchall()
        by_type = {r[0]: (int(r[1]), int(r[2])) for r in rows}
        polls_w, rows_w = by_type.get("scan_decision_write_failed", (0, 0))
        polls_s, rows_s = by_type.get("scan_decision_signature_mismatch", (0, 0))
        flags["n_write_failed"] = rows_w + rows_s
        flags["n_signature_mismatch"] = polls_s
        lines.append(
            f"  scan_decisions upsert failures: {rows_w + rows_s} rows over "
            f"{polls_w + polls_s} poll(s)  |  signature/schema mismatch polls: {polls_s}"
        )
        if rows_w + rows_s:
            lines.append(
                "  [CRIT] scan_decisions writes are failing -- M3 station-days "
                "are being lost; check logs for '[scan_decisions] upsert failed'"
            )
    except Exception:
        log.exception("Silent-failure query failed")
        lines.append("  (unavailable -- query error)")
    sha = _running_commit_sha()
    lines.append(f"  Running commit: {sha or 'unknown'}")
    lines.append("")
    return lines, flags


#: A unit with more restarts than this since its last (re)start is alarming
#: regardless of its momentary state (issue #944: 348,602 / 78,547 restarts).
UNIT_RESTART_WARN = 5

_SYSTEMD_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
_SYSTEMCTL_TIMEOUT_S = 10


def _systemctl(*args: str) -> str:
    """Run ``systemctl <args>`` and return stdout. Raises on any failure
    (missing binary, non-zero exit, timeout) so callers can degrade."""
    import subprocess
    proc = subprocess.run(
        ["systemctl", *args], capture_output=True, text=True,
        timeout=_SYSTEMCTL_TIMEOUT_S, check=True,
    )
    return proc.stdout


def _managed_units() -> "list[str]":
    """Unit names shipped in ``deploy/systemd/`` (what install.sh manages)."""
    return sorted(p.name for p in _SYSTEMD_DIR.iterdir()
                  if p.suffix in (".service", ".timer"))


def _build_services(db, today: datetime) -> "tuple[list[str], dict]":
    """Systemd unit health (issue #944): failed / restarting units, a high
    ``NRestarts`` (itself the alarm, independent of current state), and
    ``meteoedge*`` units present on the host but absent from deploy/systemd/.
    Degrades to ``(unavailable)`` if systemctl cannot be queried."""
    lines = ["Services", "-" * 8]
    flags: dict = {"status": "OK", "details": []}
    try:
        managed = _managed_units()
        problems: list[str] = []
        for unit in managed:
            out = _systemctl("show", unit, "-p",
                             "ActiveState,SubState,NRestarts,LoadState")
            props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            if props.get("LoadState") == "not-found":
                continue  # shipped but not installed on this host
            active = props.get("ActiveState", "?")
            sub = props.get("SubState", "?")
            try:
                n_restarts = int(props.get("NRestarts") or 0)
            except ValueError:
                n_restarts = 0
            bad = (active == "failed" or "auto-restart" in sub
                   or n_restarts > UNIT_RESTART_WARN)
            lines.append(f"  {unit:<44} {active} ({sub})  restarts={n_restarts}"
                         + ("  [WARN]" if bad else ""))
            if bad:
                problems.append(f"{unit} ({n_restarts} restarts)")
        host = _systemctl("list-units", "meteoedge*", "--all", "--plain",
                          "--no-legend", "--no-pager")
        host_units = {line.split()[0] for line in host.splitlines() if line.split()}
        unmanaged = sorted(u for u in host_units
                           if u.endswith((".service", ".timer"))
                           and u not in set(managed))
        for u in unmanaged:
            lines.append(f"  [WARN] unmanaged unit on host, not in deploy/systemd/: {u}")
        details = []
        if problems:
            lines.append(f"  [WARN] {len(problems)} unit(s) failed or restarting: "
                         + ", ".join(problems))
            details.append(f"{len(problems)} unit(s) failed/restarting: "
                           + ", ".join(problems))
        if unmanaged:
            details.append("unmanaged unit(s): " + ", ".join(unmanaged))
        if details:
            flags = {"status": "WARN", "details": details}
    except Exception:
        log.exception("Systemd unit query failed")
        lines.append("  (unavailable -- systemctl query error)")
        flags = {"status": "UNKNOWN", "details": []}
    lines.append("")
    return lines, flags


def _build_blockers(db, today: datetime) -> list[str]:
    """Known open issues status check."""
    lines = ["Open Blockers", "-" * 8]
    try:
        # #885: ensemble_sigma_f -- check if GEFS data is flowing
        gefs_count = db._conn.execute(
            "SELECT COUNT(*) FROM model_forecast_log WHERE model='gefs'"
        ).fetchone()[0]
        gefs_status = f"{gefs_count:,} GEFS rows" if gefs_count > 0 else "[WARN] NO GEFS DATA"
        lines.append(f"  #885 [M2]  ensemble_sigma_f -- {gefs_status}")

        # #897: KORD GEFS gap
        kord_gefs = db._conn.execute(
            "SELECT COUNT(*) FROM model_forecast_log WHERE model='gefs' AND station='KORD'"
        ).fetchone()[0]
        kord_status = "[OK] data flowing" if kord_gefs > 0 else "[WARN] gap confirmed"
        lines.append(f"  #897       KORD GEFS capture gap -- {kord_status} ({kord_gefs} rows)")

        # #893: EMOS training sigma order-dependent
        lines.append("  #893       EMOS training sigma order -- OPEN (triaged, after M3)")

    except Exception:
        log.exception("Blocker query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


def _build_verdict(sections: "list[tuple[str, list[str], dict]]") -> list[str]:
    """One-line health verdict, built from each section's own structured
    result -- never a substring scan over the whole rendered report.

    ``sections`` is ``[(name, lines, flags), ...]`` as assembled by
    ``build_report``. Sections that have been migrated to the flags contract
    (currently: ``bot``, ``table_liveness``) are checked via their ``flags``
    dict, scoped to the section that produced the condition -- this is what
    eliminates the issue #914 defect-3 bug (a WARN anywhere in the report
    getting attributed to an unrelated section). Full flags migration for the
    remaining sections (``trading``, ``pipeline``, ``guardrails``, ``emos``)
    is a natural fast-follow.

    Add new conditions here by reading ``flags_by_section`` -- this is the
    extension point for #913's error-aggregation section and CRIT path.
    """
    flags_by_section = {name: flags for name, _lines, flags in sections}
    all_text = "\n".join(line for _name, lines, _flags in sections for line in lines)

    crit_issues: list[str] = []
    warn_issues: list[str] = []
    unknown_issues: list[str] = []

    bot_flags = flags_by_section.get("bot", {})
    bot_status = bot_flags.get("status")
    if bot_status in ("STALE", "CRIT"):
        crit_issues.append("bot stale")
    elif bot_status == "WARN":
        warn_issues.append(bot_flags.get("detail") or "bot pulse degraded")

    # issue #1237: a stale table is reported as its own WARN(s), one per
    # affected table, rather than folded into an unrelated section's label --
    # the same "name the thing that actually tripped" discipline as the M3
    # sub-indicators below.
    table_liveness_flags = flags_by_section.get("table_liveness", {})
    if table_liveness_flags.get("status") == "WARN":
        warn_issues.extend(table_liveness_flags.get("details", []))

    silent_flags = flags_by_section.get("silent_failures", {})
    if silent_flags.get("n_write_failed"):
        crit_issues.append(
            f"{silent_flags['n_write_failed']} scan_decisions write failure(s) in 24h"
        )

    log_flags = flags_by_section.get("log_errors", {})
    if log_flags.get("n_new") or log_flags.get("n_spike"):
        warn_issues.append(
            f"log errors: {log_flags.get('n_new', 0)} new, "
            f"{log_flags.get('n_spike', 0)} spiking class(es) in 24h"
        )

    # issue #944: failed / restarting / unmanaged systemd units.
    warn_issues.extend(flags_by_section.get("services", {}).get("details", []))

    copy_stuck_flags = flags_by_section.get("copy_stuck", {})
    if copy_stuck_flags.get("n_stuck"):
        warn_issues.append(
            f"{copy_stuck_flags['n_stuck']} copy position(s) stuck "
            f"({copy_stuck_flags.get('n_urgent', 0)} urgent)"
        )

    live_stuck = flags_by_section.get("copy_live_stuck", {})
    if live_stuck.get("n_stuck") or live_stuck.get("n_pending_stale") or live_stuck.get("n_ghost"):
        warn_issues.append(
            f"live copy: {live_stuck.get('n_stuck', 0)} stuck, "
            f"{live_stuck.get('n_pending_stale', 0)} stale pending, "
            f"{live_stuck.get('n_ghost', 0)} unresolved ghost"
        )

    copy_skip_flags = flags_by_section.get("copy_skips", {})
    if copy_skip_flags.get("n_structural") or copy_skip_flags.get("n_unknown"):
        warn_issues.append(
            f"copy skips: {copy_skip_flags.get('n_structural', 0)} structural, "
            f"{copy_skip_flags.get('n_unknown', 0)} unknown-reason in 24h"
        )

    # "unavailable" is the shared query-error fallback string emitted by
    # every section's own except-block -- it is intentionally checked across
    # the whole report, unlike the section-specific WARN markers above.
    if "unavailable" in all_text:
        warn_issues.append("partial data only")

    # Precedence: CRIT > WARN > UNKNOWN > OK
    if crit_issues:
        all_issues = crit_issues + warn_issues + unknown_issues
        verdict = f"[CRIT] Degrading -- {', '.join(all_issues)}"
    elif warn_issues:
        all_issues = warn_issues + unknown_issues
        verdict = f"[WARN] Stable -- watch: {', '.join(all_issues)}"
    elif unknown_issues:
        verdict = f"[UNKNOWN] Cannot fully assess -- {', '.join(unknown_issues)}"
    else:
        verdict = "[OK] Healthy -- all systems nominal"

    return ["Verdict", "-" * 6, f"  {verdict}", ""]


def build_report(db) -> str:
    """Assemble the full report body from all sections.

    Each section builder may return either ``list[str]`` (the legacy
    contract) or ``(list[str], dict)`` -- lines plus structured status flags
    (issue #914 defect 3). Both are normalized here so ``_build_verdict`` can
    always work off structured, per-section results.
    """
    today = _utc_now()

    sections: list[tuple[str, list[str], dict]] = []
    for name, builder in [
        ("services", _build_services),
        ("bot", _build_bot_health),
        ("table_liveness", _build_table_liveness),
        ("trading", _build_trading),
        ("pipeline", _build_pipeline),
        ("guardrails", _build_guardrails),
        ("emos", _build_emos_progress),
        ("copy_stuck", _build_copy_stuck),
        ("copy_live_stuck", _build_copy_live_stuck),
        ("copy_skips", _build_copy_skips),
        ("silent_failures", _build_silent_failures),
        ("log_errors", _build_log_errors),
        ("param_change", _build_param_change_impact),
        ("stations", _build_station_breakdown),
    ]:
        try:
            result = builder(db, today)
        except Exception:
            log.exception("Section %s failed entirely", name)
            sections.append((name, [f"({name} -- ERROR, see log)", ""], {}))
            continue
        if isinstance(result, tuple):
            lines, flags = result
        else:
            lines, flags = result, {}
        sections.append((name, lines, flags))

    # Collect all lines + verdict
    body_lines = _build_header(today)
    for _, lines, _flags in sections:
        body_lines.extend(lines)
    body_lines.extend(_build_verdict(sections))

    body_lines.append("-" * 50)
    body_lines.append("MeteoEdge daily health report -- auto-generated")
    return "\n".join(body_lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: "list[str] | None" = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="MeteoEdge daily health report -- query DB, email summary."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print report to stdout instead of sending email.",
    )
    args = parser.parse_args(argv)

    # Lazy import so --help works without the DB module
    from src.data.db import Database

    db = Database()
    try:
        body = build_report(db)
    finally:
        db.close()

    if args.dry_run:
        try:
            print(body)
        except UnicodeEncodeError:
            # Windows console may not support all characters
            print(body.encode("ascii", errors="replace").decode("ascii"))
        return 0

    subject = f"MeteoEdge Daily Health -- {_utc_now().strftime('%Y-%m-%d')}"
    ok = _send_email(subject, body)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
