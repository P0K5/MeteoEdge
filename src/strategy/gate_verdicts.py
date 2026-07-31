"""Canonical gate-verdict enum -- single source of truth (issue #912).

Previously this frozenset was duplicated in three places: scanner.GATE_VERDICTS,
Database._SCAN_DECISION_GATE_VERDICTS, and a hand-written DDL CHECK constraint
in src/data/db.py. Adding a new verdict to only the first two (the guarded
pair) still broke every *existing* database, because SQLite CHECK constraints
can't be widened in place and the DDL only applies via CREATE TABLE IF NOT
EXISTS (a no-op against a live table) -- see issue #912 for the incident.

This module exists so there is exactly one place to edit. Both
src.strategy.scanner and src.data.db import GATE_VERDICTS from here (never
from each other, to avoid a scanner -> taf_disruption -> db -> scanner
import cycle). The DDL CHECK constraint on scan_decisions.gate_verdict has
been dropped entirely (see the scan_decisions migration in db.py's
_migrate()) -- validation now lives solely in the Python validator at
Database.upsert_scan_decision, which raises a clear ValueError instead of
letting SQLite reject the row with an opaque IntegrityError.
"""

# The 11 canonical gate-verdict enums (issue #756 / epic #754; names locked
# with the design spec docs/design/edge-tab-bracket-decisions.md), plus
# "day_mismatch_shadow" (issue #820). Every evaluated high-side bracket's
# snap dict carries exactly one of these under "gate_verdict" -- surfacing
# only, scanner.py never uses it to gate/alter a decision. "traded_live" is
# also scan_markets()'s pre-execution placeholder for a live (non-shadow,
# non-next-day) candidate that passed every gate -- src.scripts.run.poll_once
# upgrades it to entry_guard/timeout_today/traded_live once the entry-guard
# check and (if applicable) the live execution attempt have resolved (the
# "verdict seam", see run.py).
GATE_VERDICTS = frozenset({
    "traded_live", "shadow_only", "next_day_shadow", "entry_guard", "timeout_today",
    "below_min_edge", "above_max_edge", "below_min_price", "below_min_confidence",
    "margin_gate", "mae_gate", "day_mismatch_shadow",
})
