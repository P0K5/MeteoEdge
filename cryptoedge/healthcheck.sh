#!/usr/bin/env bash
# cryptoedge health check -- run from the repo root:  bash cryptoedge/healthcheck.sh
#
# Exists because these queries were being reconstructed by hand each time, and
# one of those hand-written versions was WRONG in a way that manufactured a
# false alarm (2026-09-02): poll_runs.ts is ISO8601 with a 'T' separator
# ("2026-09-02T07:01:59+00:00") while datetime('now') returns a SPACE
# separator. 'T' (0x54) sorts above ' ' (0x20), so a naive
#     WHERE ts > datetime('now','-1 hour')
# matches EVERY row from the same date, not the last hour. It reported 3,027
# polls and 513 failures for an hour that actually had 240 polls and zero
# failures, and triggered an unnecessary service restart.
#
# Every time-window comparison below wraps the column: datetime(ts).
set -uo pipefail
DB="${1:-data/cryptoedge.db}"

echo "### SERVICE ###"
systemctl is-active cryptoedge.service cryptoedge-resolve.timer 2>/dev/null \
  || echo "(systemd not available -- skipping)"

echo
echo "### VITALS  (want: ~240 polls/hr, 0 failed, 0 stuck) ###"
sqlite3 -column "$DB" <<SQL
SELECT 'last_poll_min_ago' k,
       round((julianday('now')-julianday(max(ts)))*24*60,1) v FROM poll_runs
UNION ALL SELECT 'polls_last_hour',
       (SELECT count(*) FROM poll_runs WHERE datetime(ts) > datetime('now','-1 hour'))
UNION ALL SELECT 'FAILED_last_hour',
       (SELECT coalesce(sum(ok=0),0) FROM poll_runs
         WHERE datetime(ts) > datetime('now','-1 hour'))
UNION ALL SELECT 'usable_btc5m_windows',
       (SELECT count(DISTINCT q.slug) FROM quotes q JOIN resolutions r ON r.slug=q.slug
         WHERE q.asset='btc' AND q.window_min=5 AND r.resolved_up IS NOT NULL
           AND abs(q.seconds_to_settlement-300)<=30
           AND q.best_bid IS NOT NULL AND q.best_ask IS NOT NULL)
UNION ALL SELECT 'stuck_resolutions',
       (SELECT count(*) FROM resolutions WHERE resolved_up IS NULL
         AND window_end_ms < strftime('%s','now')*1000-900000);
SQL

echo
echo "### LAST 12 HOURS  (240/hr and 0 failed is healthy) ###"
sqlite3 -column "$DB" <<SQL
SELECT substr(ts,1,13) hour, count(*) polls, sum(ok=0) failed,
       round(3600.0/count(*),1) avg_gap_s
  FROM poll_runs WHERE datetime(ts) > datetime('now','-12 hours')
 GROUP BY 1 ORDER BY 1;
SQL

echo
echo "### DATA INTEGRITY  (all must be 0) ###"
sqlite3 -column "$DB" <<SQL
SELECT 'quotes_from_failed_polls' k,
       (SELECT count(*) FROM quotes q JOIN poll_runs p ON p.ts=q.poll_ts
         WHERE p.ok=0) v
UNION ALL SELECT 'fabricated_50_50',
       (SELECT coalesce(sum(best_bid=0.5 AND best_ask=0.5),0) FROM quotes)
UNION ALL SELECT 'price_out_of_range',
       (SELECT coalesce(sum(best_bid<=0 OR best_bid>=1 OR best_ask<=0 OR best_ask>1),0)
          FROM quotes WHERE best_bid IS NOT NULL AND best_ask IS NOT NULL)
UNION ALL SELECT 'crossed_book',
       (SELECT coalesce(sum(best_bid>best_ask),0) FROM quotes);
SQL
