"""Daily health report email for MeteoEdge.

Gathers a snapshot of bot health, trading activity, data pipeline status,
guardrail events, EMOS progress, and M3 remediation-plan metrics, then
emails it as a plain-text report via SMTP.

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

from src.config import MODEL_PROB_CAP  # noqa: E402
from src.logging_config import setup_logging  # noqa: E402

#: Start of the M3 clean-data collection window, and the date station-days are
#: counted from. This has moved TWICE as probability defects landed mid-window
#: -- #917 (degF half-width ladders) went live 2026-08-01, #920 (truncation
#: without renormalisation) on 2026-08-05 -- so it is a constant rather than a
#: literal buried in a query. Both moves invalidated every station-day
#: collected before them; leaving the old date in place is how this report came
#: to print "362/300 (121%)" on 2026-08-04 against a window that was entirely
#: contaminated. Keep in step with docs/REMEDIATION_PLAN.md's M3 section.
M3_CLEAN_DATA_CLOCK_START = "2026-08-06"

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


def _ladder_mass(db, since_date: str) -> "dict[str, float] | None":
    """Mean SUM(raw_p_yes) per ladder over the last 24h, and its worst kind.

    **The check whose absence cost two weeks.** Two probability defects reached
    production and survived undetected because nothing asserted the one
    invariant that makes them obvious on sight: a gap-free bracket ladder must
    integrate to ~1.0.

    * #917 -- degF dash-range brackets integrated at half width. Ladders summed
      to **0.53** for a week.
    * #920 -- truncation without renormalisation. Ladders summed to **0.80**,
      and the 15% lost at the top was mistaken for the 3% lost at the bottom
      until it was measured per-end.

    Both were visible in this single number from the day they landed. Neither
    was being watched, so this is the headline M3 indicator: **a clean count of
    contaminated station-days is worse than no count at all.**

    Grouped by (station, poll_ts, date) against ``scan_decisions``. That table
    Grouped by **(station, date)** and NOT by ``poll_ts``. ``scan_decisions``
    upserts on (station, ticker, date), so that pair already identifies exactly
    one ladder -- every ticker holds a single row carrying its most recent
    evaluation.

    The first version of this grouped by (station, poll_ts, date) and filtered
    ``poll_ts >= 24h ago``, on the reasoning that a ladder is written in one
    poll cycle and therefore shares a timestamp. That is false: a market that
    stops being scanned (settled, delisted, outside station hours) keeps an
    older ``poll_ts``, so any ladder straddling the 24h boundary was split into
    fragments and each fragment scored as a separate deficient ladder. On
    2026-08-06 it reported "0.947 mean, 0.464 worst, 10/55 deficient" against a
    production state where every one of 322 ladders summed to 1.000 -- caught
    only because ``m3_window_diagnostics`` reads the append-only
    ``bracket_evals`` and disagreed.

    A monitor that cries wolf is worse than none: it trains the reader to
    ignore exactly the signal it exists to raise. Hence the date filter now
    applies to ``date`` (which settlement day the ladder belongs to) rather
    than to ``poll_ts`` (when a given bracket last happened to be written).

    Returns ``None`` when there is nothing to measure -- absent data must not
    read as a healthy ladder.
    """
    rows = db._conn.execute(
        "SELECT station, date, SUM(raw_p_yes) AS total, COUNT(*) AS n "
        "FROM scan_decisions WHERE date >= ? AND raw_p_yes IS NOT NULL "
        "GROUP BY station, date HAVING n >= 5",
        (since_date,),
    ).fetchall()
    if not rows:
        return None
    # Index 2: the query is (station, date, SUM, COUNT). Dropping poll_ts
    # from the SELECT shifted this left -- r[3] silently became COUNT(*),
    # which reported every ladder as '11.000' regardless of its mass.
    totals = [r[2] for r in rows if r[2] is not None]
    if not totals:
        return None
    # Worst individual ladder matters more than the mean: a defect confined to
    # one station or one ladder shape is exactly what a mean hides, which is
    # how #917 stayed invisible while degC ladders looked fine.
    return {
        "mean": sum(totals) / len(totals),
        "worst": min(totals),
        "n_ladders": len(totals),
        "n_deficient": sum(1 for t in totals if t < 0.90),
    }


def _build_m3_progress(db, today: datetime) -> list[str]:
    """M3 station-day accrual and leading indicators."""
    lines = ["M3 Progress", "-" * 10]
    try:
        since = (today - timedelta(hours=24)).isoformat()

        # Mass conservation FIRST: it qualifies everything below it. A
        # station-day counted while ladders are leaking mass is a contaminated
        # station-day, and reporting the count above the health check is what
        # let "121% -- power bar met" mean nothing on 2026-08-04.
        # Settlement days from yesterday on: a ladder is keyed by the day it
        # settles, not by when a bracket was last written.
        mass = _ladder_mass(db, (today - timedelta(days=1)).date().isoformat())
        if mass is None:
            lines.append("  Ladder mass:   (no ladders in 24h -- cannot assess)")
            mass_ok = False
        else:
            mass_ok = 0.95 <= mass["mean"] <= 1.05 and mass["n_deficient"] == 0
            lines.append(
                f"  Ladder mass:   {mass['mean']:.3f} mean, {mass['worst']:.3f} worst "
                f"({mass['n_deficient']}/{mass['n_ladders']} deficient) "
                f"{'[OK]' if mass_ok else '[WARN]'}"
            )
            if not mass_ok:
                lines.append("                 ^ probabilities are leaking mass "
                             "(cf. #917, #920) -- station-days below are NOT clean")

        # Station-days accrue from the CLEAN-DATA CLOCK, which has moved twice
        # as probability defects landed mid-window (#917 on 2026-08-01, #920 on
        # 2026-08-05). Counting from an out-of-date start is not a cosmetic
        # error: on 2026-08-04 this reported "362/300 (121%)" -- i.e. run the
        # gate -- against a window that was 100% contaminated.
        clock_start = M3_CLEAN_DATA_CLOCK_START
        station_days = db._conn.execute(
            "SELECT COUNT(DISTINCT station || '-' || date) FROM scan_decisions "
            "WHERE poll_ts >= ? AND raw_p_yes IS NOT NULL AND raw_p_yes != 0.0 "
            "AND yes_ask > 1 AND yes_ask < 99 AND no_ask > 1 AND no_ask < 99",
            (clock_start,),
        ).fetchone()[0] or 0

        pct = station_days / 300 * 100
        elapsed = max(1, (today.date() - date.fromisoformat(clock_start)).days)
        rate = station_days / elapsed
        days_to_bar = max(0, int((300 - station_days) / max(1, rate)))

        lines.append(f"  Station-days:  {station_days}/300 ({pct:.0f}%) "
                     f"scoreable, since {clock_start}")
        lines.append(f"  Accrual rate:  ~{rate:.0f}/day -> power bar ~{today.strftime('%b %d')} + {days_to_bar}d")
        if station_days >= 300 and not mass_ok:
            lines.append("                 ^ bar met on COUNT only -- ladder mass is "
                         "not clean, so the gate must NOT run")

        # Leading indicators: rail concentration and p_yes=0 artifact rate
        # from the most recent 24h of scan_decisions
        total_brackets = db._conn.execute(
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts >= ?",
            (since,),
        ).fetchone()[0] or 0

        # Derive rail thresholds from MODEL_PROB_CAP so the metric follows the clamp.
        # Symmetric clamp: lower = 1.0 - MODEL_PROB_CAP, upper = MODEL_PROB_CAP.
        # True rail concentration ~62.4%; see #917 for investigation of mass at clamp floor.
        rail_lower = round(1.0 - MODEL_PROB_CAP, 10)
        rail_upper = MODEL_PROB_CAP
        rail_count = db._conn.execute(
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts >= ? "
            "AND (capped_p_yes <= ? OR capped_p_yes >= ?)",
            (since, rail_lower, rail_upper),
        ).fetchone()[0] or 0

        zero_artifact = db._conn.execute(
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts >= ? "
            "AND raw_p_yes = 0.0",
            (since,),
        ).fetchone()[0] or 0

        if not total_brackets:
            # Previously `or 1`, which made an empty 24h render as "0.0% [OK]"
            # -- no data reading as perfect health, the same failure the ladder
            # -mass check above exists to prevent. Say there is nothing to
            # measure instead.
            lines.append("  Rail / artifact: (no brackets in 24h -- cannot assess)")
            lines.append("")
            return lines

        rail_pct = rail_count / total_brackets * 100
        zero_pct = zero_artifact / total_brackets * 100

        rail_ok = rail_pct < 20
        zero_ok = zero_pct < 10

        lines.append(f"  Rail (0-{rail_lower*100:.1f}% / {rail_upper*100:.1f}%-100%): {rail_pct:.1f}% {'[OK]' if rail_ok else '[WARN]'}")
        lines.append(f"  p_yes=0.0 artifact:     {zero_pct:.1f}% {'[OK]' if zero_ok else '[WARN]'}")

    except Exception:
        log.exception("M3 progress query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


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
    (currently: ``bot``) are checked via their ``flags`` dict. Sections that
    have not yet migrated are checked against *their own* lines only, keyed
    by section name -- this still eliminates the issue #914 defect-3 bug
    (a WARN anywhere in the report getting attributed to an unrelated
    section) because each check is scoped to the section that produced the
    condition, not the full report text. Full flags migration for the
    remaining sections is a natural fast-follow once #911's concurrent edit
    to ``_build_m3_progress`` has landed.

    Add new conditions here by reading ``flags_by_section`` /
    ``lines_by_section`` -- this is the extension point for #913's
    error-aggregation section and CRIT path.
    """
    lines_by_section = {name: lines for name, lines, _flags in sections}
    flags_by_section = {name: flags for name, _lines, flags in sections}
    all_text = "\n".join(line for _name, lines, _flags in sections for line in lines)

    crit_issues: list[str] = []
    warn_issues: list[str] = []

    bot_flags = flags_by_section.get("bot", {})
    bot_status = bot_flags.get("status")
    if bot_status in ("STALE", "CRIT"):
        crit_issues.append("bot stale")
    elif bot_status == "WARN":
        warn_issues.append(bot_flags.get("detail") or "bot pulse degraded")

    m3_text = "\n".join(lines_by_section.get("m3", []))
    if "p_yes=0.0 artifact:" in m3_text and "[WARN]" in m3_text:
        warn_issues.append("artifact rate high")

    blockers_text = "\n".join(lines_by_section.get("blockers", []))
    if "[WARN] gap confirmed" in blockers_text:
        warn_issues.append("#897 KORD gap")
    if "NO GEFS DATA" in blockers_text:
        warn_issues.append("#885 no GEFS data")

    # "unavailable" is the shared query-error fallback string emitted by
    # every section's own except-block -- it is intentionally checked across
    # the whole report, unlike the section-specific WARN markers above.
    if "unavailable" in all_text:
        warn_issues.append("partial data only")

    if crit_issues:
        verdict = f"[CRIT] Degrading -- {', '.join(crit_issues + warn_issues)}"
    elif warn_issues:
        verdict = f"[WARN] Stable -- watch: {', '.join(warn_issues)}"
    else:
        verdict = "[OK] Healthy -- all systems nominal, M3 accruing on track"

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
        ("bot", _build_bot_health),
        ("trading", _build_trading),
        ("pipeline", _build_pipeline),
        ("guardrails", _build_guardrails),
        ("m3", _build_m3_progress),
        ("emos", _build_emos_progress),
        ("blockers", _build_blockers),
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
