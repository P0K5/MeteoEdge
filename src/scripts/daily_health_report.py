"""Daily health report email for MeteoEdge.

Gathers a snapshot of bot health, trading activity, data pipeline status,
guardrail events, EMOS progress, and M3 remediation-plan metrics, then
emails it as a plain-text report via the existing SMTP infrastructure.

Usage::

    python -m src.scripts.daily_health_report           # send email
    python -m src.scripts.daily_health_report --dry-run # print to stdout

Schedule: systemd timer ``meteoedge-health-report.timer`` fires daily at
14:00 UTC (after settlement, resolve-outcomes, and prob-cap report).

Design constraints:
- Runs in ~seconds; all queries are lightweight aggregates.
- Degrades gracefully: if a section's data is unavailable it reports
  "(unavailable)" rather than crashing.
- Reuses ``AlertManager._send()`` from ``src.monitoring.alerts`` -- no new
  SMTP plumbing.
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup -- make `src` importable from this script's location.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE.parents[1]) not in sys.path:
    sys.path.insert(0, str(_HERE.parents[1]))

from src.logging_config import setup_logging  # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SMTP / email constants (mirrors src/monitoring/alerts.py)
# ---------------------------------------------------------------------------
ALERT_EMAIL_TO = "andre.freixo.santos@gmail.com"
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
POLL_WARN_MINUTES = 20
POLL_STALE_MINUTES = 60
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
    if not SMTP_USER or not SMTP_PASS:
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


def _build_bot_health(db, today: datetime) -> list[str]:
    """Poll recency, poll count, and systemd status."""
    lines = ["Bot Pulse", "-" * 10]
    try:
        row = db._conn.execute(
            "SELECT MAX(poll_ts) FROM scan_decisions"
        ).fetchone()
        last_poll_str = row[0] if row and row[0] else None
        if last_poll_str:
            last_poll = datetime.fromisoformat(last_poll_str)
            gap_min = (_utc_now() - last_poll).total_seconds() / 60
            lines.append(f"  Last poll:   {_iso(last_poll)} ({gap_min:.0f} min ago)")

            # Polls in last 24h
            cutoff = (today - timedelta(hours=24)).isoformat()
            count_row = db._conn.execute(
                "SELECT COUNT(DISTINCT poll_ts) FROM scan_decisions WHERE poll_ts >= ?",
                (cutoff,),
            ).fetchone()
            poll_count = count_row[0] if count_row else 0
            expected = int(24 * 60 / 5)  # 288 polls/day at 5-min interval
            lines.append(f"  Polls 24h:   {poll_count}/{expected}")

            # Check for gaps
            gaps = db._conn.execute(
                "SELECT poll_ts FROM scan_decisions "
                "WHERE poll_ts >= ? ORDER BY poll_ts",
                (cutoff,),
            ).fetchall()
            max_gap = 0
            for i in range(1, len(gaps)):
                dt1 = datetime.fromisoformat(gaps[i - 1][0])
                dt2 = datetime.fromisoformat(gaps[i][0])
                gap = (dt2 - dt1).total_seconds() / 60
                if gap > max_gap:
                    max_gap = gap
            if len(gaps) >= 2 and max_gap > POLL_WARN_MINUTES:
                lines.append(f"  [WARN] Max gap:    {max_gap:.0f} min (threshold: {POLL_WARN_MINUTES})")

            if gap_min <= POLL_WARN_MINUTES:
                lines.append("  Status:      [OK] Healthy")
            elif gap_min <= POLL_STALE_MINUTES:
                lines.append(f"  Status:      [WARN] {gap_min:.0f} min since last poll")
            else:
                lines.append(f"  Status:      [STALE] {gap_min:.0f} min since last poll")
        else:
            lines.append("  Status:      [CRIT] No polls found in scan_decisions")
    except Exception:
        log.exception("Bot health query failed")
        lines.append("  (unavailable -- query error)")
    lines.append("")
    return lines


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
            lines.append(f"  Win rate ({len(wr_rows)} settled): {_fmt_pct(wr)} ({wins}/{len(wr_rows)})")
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


def _build_m3_progress(db, today: datetime) -> list[str]:
    """M3 station-day accrual and leading indicators."""
    lines = ["M3 Progress", "-" * 10]
    try:
        # Count distinct (station, date) pairs in bracket_evals since
        # the clean-data window started (2026-07-24).
        station_days = db._conn.execute(
            "SELECT COUNT(DISTINCT station || '-' || date) FROM scan_decisions "
            "WHERE poll_ts >= '2026-07-24'"
        ).fetchone()[0] or 0

        pct = station_days / 300 * 100
        rate = station_days / max(1, (today.date() - datetime(2026, 7, 24).date()).days)
        days_to_bar = max(0, int((300 - station_days) / max(1, rate)))

        lines.append(f"  Station-days:  {station_days}/300 ({pct:.0f}%)")
        lines.append(f"  Accrual rate:  ~{rate:.0f}/day -> power bar ~{today.strftime('%b %d')} + {days_to_bar}d")

        # Leading indicators: rail concentration and p_yes=0 artifact rate
        # from the most recent 24h of scan_decisions
        since = (today - timedelta(hours=24)).isoformat()
        total_brackets = db._conn.execute(
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts >= ?",
            (since,),
        ).fetchone()[0] or 1

        rail_count = db._conn.execute(
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts >= ? "
            "AND (capped_p_yes <= 0.02 OR capped_p_yes >= 0.98)",
            (since,),
        ).fetchone()[0] or 0

        zero_artifact = db._conn.execute(
            "SELECT COUNT(*) FROM scan_decisions WHERE poll_ts >= ? "
            "AND raw_p_yes = 0.0",
            (since,),
        ).fetchone()[0] or 0

        rail_pct = rail_count / total_brackets * 100
        zero_pct = zero_artifact / total_brackets * 100

        rail_ok = rail_pct < 20  # pre-fix was 62.6%
        zero_ok = zero_pct < 10  # pre-fix was 17.8%

        lines.append(f"  Rail (0-2% / 98-100%): {rail_pct:.1f}% {'[OK]' if rail_ok else '[WARN]'} (pre-fix 62.6%)")
        lines.append(f"  p_yes=0.0 artifact:     {zero_pct:.1f}% {'[OK]' if zero_ok else '[WARN]'} (pre-fix 17.8%)")

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


def _build_verdict(body_lines: list[str]) -> list[str]:
    """One-line health verdict based on sections above."""
    all_text = "\n".join(body_lines)
    crit_issues = []
    warn_issues = []

    if "[CRIT]" in all_text:
        crit_issues.append("bot stale")
    if "p_yes=0.0 artifact:" in all_text and "[WARN]" in all_text:
        warn_issues.append("artifact rate high")
    if "[WARN] gap confirmed" in all_text:
        warn_issues.append("#897 KORD gap")
    if "NO GEFS DATA" in all_text:
        warn_issues.append("#885 no GEFS data")
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
    """Assemble the full report body from all sections."""
    today = _utc_now()

    sections: list[tuple[str, list[str]]] = []
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
            sections.append((name, builder(db, today)))
        except Exception:
            log.exception("Section %s failed entirely", name)
            sections.append((name, [f"({name} -- ERROR, see log)", ""]))

    # Collect all lines + verdict
    body_lines = _build_header(today)
    for _, lines in sections:
        body_lines.extend(lines)
    body_lines.extend(_build_verdict(body_lines))

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
