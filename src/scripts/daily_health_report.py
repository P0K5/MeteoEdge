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


def _resolved_station_days(clock_start: str) -> "int | None":
    """Resolved station-days for the clean window -- the gate's own population.

    Uses ``resolve_bracket_outcomes(since=...)``, the capability #1018 added
    for exactly this. Before it existed the only tool that reported resolved
    ``n`` for a window was ``bss_market_vs_model_report``, which also prints
    the BSS number -- so learning the true gate date meant running the gate and
    seeing the answer, an optional-stopping problem manufactured by a missing
    CLI flag.

    **Never issues network requests.** This runs daily by email and must not
    depend on Polymarket being reachable, so Gamma is served from the
    persistent cache only; anything uncached falls back to METAR. That makes
    the figure a slight UNDER-count on a cold cache, which is the safe
    direction here: it can only push the projected gate date later, and erring
    early is the direction #1018 exists to stop.

    Returns ``None`` when the figure cannot be computed. Absent data must not
    silently read as a measured ratio.
    """
    try:
        from src.scripts.resolve_bracket_outcomes import resolve_bracket_outcomes
        _rows, counts = resolve_bracket_outcomes(
            since=clock_start, allow_network=False)
    except Exception:
        log.exception("resolved station-day count failed")
        return None
    n = counts.get("n_station_days")
    return n if isinstance(n, int) and n > 0 else None


def _window_mass(clock_start: str) -> "dict | None":
    """Window-wide mass verdict since the clean-data clock (#1022).

    ``_ladder_mass`` above is scoped to the last 24 hours, which is the right
    cadence for catching a defect the day it ships but **structurally cannot
    see** a deficiency from earlier in the window. #917 and #920 each survived
    weeks in exactly that blind spot: every day between the bad day and today
    prints OK on fresh data while the window carries the defect.

    This is the figure that may qualify the cumulative station-day count.
    It delegates to ``m3_window_diagnostics.deficient_days_since`` rather than
    re-deriving the rule, so the daily email and the on-demand diagnostic agree
    **by construction** -- the property the comment at the mass check has
    always claimed and never had.

    Returns ``None`` when there is nothing to measure; absent data is not a
    pass.
    """
    try:
        from src.scripts.bss_market_vs_model_report import load_bracket_eval_rows
        from src.scripts.m3_window_diagnostics import (
            deficient_days_since,
            group_ladders,
            mass_by_day,
            no_evidence_days_since,
        )
        rows = [r for r in load_bracket_eval_rows()
                if (r.get("ts") or "")[:10] >= clock_start]
    except Exception:
        log.exception("window mass read failed")
        return None

    if not rows:
        return None
    masses = mass_by_day(group_ladders(rows))
    if not masses:
        return None
    return {
        "n_days": len(masses),
        "deficient_days": deficient_days_since(masses, clock_start),
        "no_evidence_days": no_evidence_days_since(masses, clock_start),
    }


def _ladder_sharpness(clock_start: str) -> "dict | None":
    """Modal-share health of last-poll same-day ladders since the clock (#1021).

    Mass conservation is **necessary, not sufficient**. A uniform ladder sums
    to 1.0 and passes the mass, gap, rail and artifact checks while carrying no
    information at all -- and 11 of the 12 near-uniform ladders #1021 found
    were correctly normalised, so every existing invariant was blind to them.

    Scoped to the window rather than 24h because a handful of flat ladders is a
    slow-accumulating property of the scored population, not a same-day
    regression. Delegates to ``m3_window_diagnostics`` for the same
    agree-by-construction reason as ``_window_mass``.
    """
    try:
        from src.scripts.bss_market_vs_model_report import load_bracket_eval_rows
        from src.scripts.m3_window_diagnostics import (
            SHARPNESS_FLOOR,
            group_ladders,
            near_uniform_ladders,
            sharpness_stats,
        )
        rows = [r for r in load_bracket_eval_rows()
                if (r.get("ts") or "")[:10] >= clock_start]
    except Exception:
        log.exception("sharpness read failed")
        return None

    if not rows:
        return None
    ladders = group_ladders(rows)
    stats = sharpness_stats(ladders)
    if not stats["n"]:
        return None
    stats["flattest"] = near_uniform_ladders(ladders)[:3]
    # Carried on the result so the caller quotes the threshold it was actually
    # measured against, rather than a second copy that can drift from it.
    stats["floor"] = SHARPNESS_FLOOR
    return stats


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

    **The definition is the gate's, verbatim**::

        len({(station, settlement_date) for r in rows_after_exclusions})

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

    Mirrors the gate's exclusions: exact-zero model probabilities and
    rail-clipped market prices.
    """
    from src.scripts.bss_market_vs_model_report import (
        RAIL_HIGH_CENTS, RAIL_LOW_CENTS, load_bracket_eval_rows)
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
        p, yes_ask, no_ask = (r.get("p_yes_raw"), r.get("yes_ask"),
                              r.get("no_ask"))
        if p is None or p == 0.0 or yes_ask is None or no_ask is None:
            continue
        if (yes_ask <= RAIL_LOW_CENTS or yes_ask >= RAIL_HIGH_CENTS
                or no_ask <= RAIL_LOW_CENTS or no_ask >= RAIL_HIGH_CENTS):
            continue
        if station and settle:
            pairs.add((station, settle))
    return pairs


def _build_m3_progress(db, today: datetime) -> "tuple[list[str], dict]":
    """M3 station-day accrual and leading indicators.

    Returns ``(lines, flags)``. The section carries THREE independently
    WARN-able indicators -- ladder mass, high rail vs. structural ceiling,
    and interior-zero gaps -- so ``flags`` reports each one under its own
    key (``mass_status`` / ``window_mass_status`` / ``sharpness_status`` /
    ``rail_status`` / ``gaps_status``, each
    ``OK / WARN / UNKNOWN``) instead of a single section-wide marker.
    ``_build_verdict`` reads these directly so a WARN on one indicator is
    never misattributed to another (issue #972: a bare ``"[WARN]" in
    m3_text`` scan named every WARN in this section "artifact rate high",
    even when it was actually the mass or rail indicator that tripped).
    """
    lines = ["M3 Progress", "-" * 10]
    flags: dict = {
        "mass_status": "UNKNOWN",
        "window_mass_status": "UNKNOWN",
        "sharpness_status": "UNKNOWN",
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
        # TWO SCOPES, each qualifying only what it covers (#1022).
        #
        # The 24h check is the fresh-regression signal -- the right cadence for
        # catching a defect the day it ships. It is NOT evidence about the
        # cumulative station-day count below it, and reading it as such is the
        # defect #1022 records: on 2026-08-19 one deficient ladder in 24h
        # printed "station-days below are NOT clean" against a 249-station-day
        # window that `m3_window_diagnostics` confirmed clean on the same host.
        #
        # The window-wide check is the one that may qualify the count, and it
        # closes the blind spot in the other direction, which is the worse one:
        # a 24h-only check structurally cannot see a deficiency from earlier in
        # the window, and prints [OK] every day between the bad day and today.
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
                f"({mass['n_deficient']}/{mass['n_ladders']} deficient, last 24h) "
                f"{'[OK]' if mass_ok else '[WARN]'}"
            )
            if mass.get("n_excused"):
                lines.append(f"                 ({mass['n_excused']} short ladder(s) "
                             "excused -- no open-ended tail bracket)")
            if not mass_ok:
                # Scoped language: this says a ladder went bad TODAY. Whether
                # the window is clean is the next check's question, not this
                # one's.
                lines.append("                 ^ a ladder went deficient in the "
                             "last 24h (cf. #917, #920) -- fresh regression, see "
                             "window mass below for the cumulative verdict")

        window = _window_mass(M3_CLEAN_DATA_CLOCK_START)
        if window is None:
            lines.append("  Window mass:   (no ladders since "
                         f"{M3_CLEAN_DATA_CLOCK_START} -- cannot assess)")
            window_ok = False
            flags["window_mass_status"] = "UNKNOWN"
        else:
            bad_days = window["deficient_days"]
            window_ok = not bad_days
            flags["window_mass_status"] = "OK" if window_ok else "WARN"
            lines.append(
                f"  Window mass:   {len(bad_days)}/{window['n_days']} day(s) "
                f"deficient since {M3_CLEAN_DATA_CLOCK_START} "
                f"{'[OK]' if window_ok else '[WARN]'}"
            )
            if bad_days:
                lines.append("                 ^ " + ", ".join(bad_days[:5]))
                lines.append("                 ^ station-days below are NOT clean "
                             "-- the gate must not run until this is explained "
                             "or the window restarted")
            # `no_evidence_days` is deliberately NOT printed here. It requires
            # BOTH the censored and uncensored populations to carry judgeable
            # ladders, which is the right bar for the on-demand diagnostic but
            # fires most days in a daily email: post-#920 truncation is fixed,
            # so ordinary days legitimately have no censored ladders. A line
            # that prints every day is a line nobody reads, and #1022 exists
            # partly because this section already cried wolf once.

        sharp = _ladder_sharpness(M3_CLEAN_DATA_CLOCK_START)
        if sharp is None:
            lines.append("  Sharpness:     (no last-poll same-day ladders "
                         "-- cannot assess)")
            flags["sharpness_status"] = "UNKNOWN"
        else:
            sharp_ok = sharp["n_flat"] == 0
            flags["sharpness_status"] = "OK" if sharp_ok else "WARN"
            lines.append(
                f"  Sharpness:     {sharp['median']:.3f} median modal share, "
                f"{sharp['n_flat']}/{sharp['n']} near-uniform "
                f"{'[OK]' if sharp_ok else '[WARN]'}"
            )
            if not sharp_ok:
                lines.append("                 ^ last-poll same-day ladders whose "
                             "modal bracket holds < "
                             f"{sharp['floor']:.2f} -- these conserve mass and "
                             "carry no information (#1021). Diagnostic, not a "
                             "gate blocker.")
                for lad in sharp.get("flattest", []):
                    lines.append(f"                   maxp={lad['max_p']:.4f} "
                                 f"{lad['station']} {lad['ts']}")

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

        lines.append(f"  Station-days:  {station_days}/300 ({pct:.0f}%) "
                     f"scoreable, since {clock_start}")

        # THE PROJECTION MUST COMPARE LIKE WITH LIKE (#1018).
        #
        # `station_days` is a PRE-resolution count; 300 is a RESOLVED bar. The
        # previous arithmetic -- `(300 - station_days) / rate` -- projected one
        # population onto the other's bar and printed a date up to a week
        # early, in the same optimistic direction `_marginal_accrual` was
        # introduced to stop erring in.
        #
        # The haircut is now MEASURED for the current window rather than the
        # hardcoded "~25-30%", which came from 81/111 on 2026-08-10 -- four
        # days into the window, when most settlement dates had not had time to
        # resolve. That figure was lag-dominated, not haircut-dominated, and
        # nothing re-measured it because nothing could: `resolve_bracket_
        # outcomes` had no --since. It does now, and this reads the same
        # capability in-process.
        resolved = _resolved_station_days(clock_start)
        if resolved is None or not station_days:
            ratio = None
            lines.append("                 ^ pre-resolution upper bound; the gate "
                         "scores only resolved station-days (ratio not measurable "
                         "this run)")
        else:
            ratio = resolved / station_days
            lines.append(f"                 ^ pre-resolution upper bound; the gate "
                         f"scores only resolved station-days -- measured "
                         f"{resolved}/{station_days} = {ratio:.0%} of these have "
                         f"resolved so far")

        # Project on the RESOLVED count against the resolved bar. With no
        # measured ratio, fall back to the raw count -- and say so, rather than
        # inventing a haircut. The printed date must never be EARLIER than the
        # resolved-count date, which this guarantees by construction: the
        # resolved count is <= the pre-resolution count, so the remaining gap
        # is >= the one the old arithmetic used.
        if ratio:
            effective = resolved
            effective_rate = rate * ratio
        else:
            effective = station_days
            effective_rate = rate
        # int(): `effective_rate` is a float, so the ceiling division yields
        # one too, and the line rendered "power bar ~Aug 19 + 30.0d".
        days_to_bar = (int(max(0, -(-(300 - effective) // effective_rate)))
                       if effective_rate > 0 else None)

        if days_to_bar is None:
            lines.append("  Accrual rate:  no scoreable rows yet -- bar date unknown")
        else:
            basis = "resolved" if ratio else "pre-resolution (unadjusted)"
            lines.append(f"  Accrual rate:  ~{rate:.0f}/day raw, "
                         f"~{effective_rate:.0f}/day {basis} -> power bar "
                         f"~{today.strftime('%b %d')} + {days_to_bar}d")
        # The CUMULATIVE count is qualified by the WINDOW-wide verdict, never
        # by 24h of evidence (#1022). Using `mass_ok` here compared one day of
        # data against a 14-day count.
        if station_days >= 300 and not window_ok:
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
    (currently: ``bot``, ``m3``) are checked via their ``flags`` dict --
    ``m3`` in particular reports THREE independent indicators
    (``mass_status`` / ``window_mass_status`` / ``sharpness_status`` /
    ``rail_status`` / ``gaps_status``) so a WARN on one is
    never misattributed to another (issue #972: a section-wide
    ``"[WARN]" in m3_text`` scan used to name every M3 WARN "artifact rate
    high", even when it was the mass or rail indicator that tripped).
    Sections that have not yet migrated are checked against *their own*
    lines only, keyed by section name -- this still eliminates the issue
    #914 defect-3 bug (a WARN anywhere in the report getting attributed to
    an unrelated section) because each check is scoped to the section that
    produced the condition, not the full report text. Full flags migration
    for the remaining sections (``trading``, ``pipeline``, ``guardrails``,
    ``emos``, ``blockers``) is a natural fast-follow.

    Add new conditions here by reading ``flags_by_section`` /
    ``lines_by_section`` -- this is the extension point for #913's
    error-aggregation section and CRIT path.
    """
    lines_by_section = {name: lines for name, lines, _flags in sections}
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

    m3_flags = flags_by_section.get("m3", {})
    # Three distinct M3 probability indicators, named separately for the same
    # reason #972 split this block in the first place: a shared label makes a
    # WARN on one read as a WARN on another. 24h mass, window mass and
    # sharpness fail for different reasons and imply different responses.
    if m3_flags.get("mass_status") == "WARN":
        warn_issues.append("M3 ladder mass leaking (last 24h)")
    elif m3_flags.get("mass_status") == "UNKNOWN":
        unknown_issues.append("M3 ladder mass not assessed (last 24h)")

    if m3_flags.get("window_mass_status") == "WARN":
        warn_issues.append("M3 window mass deficient -- station-days NOT clean")
    elif m3_flags.get("window_mass_status") == "UNKNOWN":
        unknown_issues.append("M3 window mass not assessed")

    if m3_flags.get("sharpness_status") == "WARN":
        warn_issues.append("M3 near-uniform ladders (#1021)")
    elif m3_flags.get("sharpness_status") == "UNKNOWN":
        unknown_issues.append("M3 sharpness not assessed")

    if m3_flags.get("rail_status") == "WARN":
        warn_issues.append("M3 high rail vs structural ceiling")
    elif m3_flags.get("rail_status") == "UNKNOWN":
        unknown_issues.append("M3 high rail not assessed")

    if m3_flags.get("gaps_status") == "WARN":
        warn_issues.append("artifact rate high")
    elif m3_flags.get("gaps_status") == "UNKNOWN":
        unknown_issues.append("M3 gaps not assessed")

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
