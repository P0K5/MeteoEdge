"""Promotion gate checker — reads latest backtest reports and emits PROMOTE/HOLD/KILL decisions.

Reads the most recent backtest report for each stack from backtest_results/:
  - hrrr_nbm_skill_*.md       → HRRR + NBM stack
  - ecmwf_icon_skill_*.md     → ECMWF + ICON stack

Parses the MAE improvement line from each report, applies the 0.3 °F gate,
and prints a clear decision for each stack.

Exit codes:
  0 — all active stacks pass (PROMOTE or no report yet)
  1 — one or more stacks fail (HOLD or KILL)

Usage:
    python -m src.scripts.check_promotion_gate
    python -m src.scripts.check_promotion_gate --dir backtest_results
"""

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKTEST_DIR = _REPO_ROOT / "backtest_results"

# Gate threshold: minimum MAE improvement (°F) to promote
MAE_GATE_F = 0.3

# Pattern for the MAE improvement row in the recommendation table.
# Matches lines like:
#   | MAE improvement (NWS → 4-model) | +0.412 °F | YES |
#   | MAE improvement (baseline → ensemble) | -0.027 °F | NO |
_MAE_PATTERN = re.compile(
    r"\|\s*MAE improvement[^|]*\|\s*([+-]?\d+\.\d+)\s*°F\s*\|",
    re.IGNORECASE,
)

# Pattern for the decision line: ### **Decision: PROMOTE**
_DECISION_PATTERN = re.compile(
    r"###\s+\*\*Decision:\s+(PROMOTE|HOLD|KILL)\*\*",
    re.IGNORECASE,
)


def _find_latest_report(directory: Path, prefix: str) -> "Path | None":
    """Return the most recently-dated report matching *prefix*_*.md."""
    candidates = sorted(directory.glob(f"{prefix}_*.md"))
    return candidates[-1] if candidates else None


def _parse_report(path: Path) -> "tuple[float | None, str | None]":
    """Parse a backtest report, returning (mae_improvement, decision) or (None, None)."""
    text = path.read_text(encoding="utf-8")

    mae_match = _MAE_PATTERN.search(text)
    mae_improvement = float(mae_match.group(1)) if mae_match else None

    dec_match = _DECISION_PATTERN.search(text)
    decision = dec_match.group(1).upper() if dec_match else None

    return mae_improvement, decision


def _check_stack(directory: Path, prefix: str, label: str) -> "tuple[str, bool]":
    """Check one stack's latest report. Returns (status_line, passed)."""
    report = _find_latest_report(directory, prefix)
    if report is None:
        msg = f"  [{label}] NO REPORT FOUND — no backtest run yet (HOLD)"
        return msg, False  # conservatively hold if no data

    mae_improvement, decision = _parse_report(report)

    if decision is None and mae_improvement is None:
        msg = f"  [{label}] PARSE ERROR — could not read decision from {report.name} (HOLD)"
        return msg, False

    # Use parsed decision if available; re-derive from MAE if not
    if decision is None:
        if mae_improvement is None:
            decision = "HOLD"
        elif mae_improvement >= MAE_GATE_F:
            decision = "PROMOTE"
        elif mae_improvement >= 0:
            decision = "HOLD"
        else:
            decision = "KILL"

    mae_str = f"{mae_improvement:+.3f} °F" if mae_improvement is not None else "unknown"
    passed = decision == "PROMOTE"
    msg = (
        f"  [{label}] {decision} — "
        f"MAE delta={mae_str}, gate={MAE_GATE_F:+.1f} °F — "
        f"report: {report.name}"
    )
    return msg, passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check backtest promotion gates for all forecast stacks"
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=_BACKTEST_DIR,
        help=f"Directory containing backtest reports (default: {_BACKTEST_DIR})",
    )
    args = parser.parse_args()

    directory: Path = args.dir
    if not directory.exists():
        print(f"[gate] Backtest directory not found: {directory}")
        print("[gate] Run the backtest scripts first:")
        print("[gate]   python -m src.scripts.hrrr_nbm_backtest")
        print("[gate]   python -m src.scripts.ecmwf_icon_backtest")
        sys.exit(1)

    print("=" * 60)
    print("Promotion Gate Check")
    print(f"Directory: {directory}")
    print("=" * 60)
    print()

    stacks = [
        ("hrrr_nbm_skill", "HRRR + NBM (US)"),
        ("ecmwf_icon_skill", "ECMWF + ICON (International)"),
    ]

    any_failed = False
    for prefix, label in stacks:
        line, passed = _check_stack(directory, prefix, label)
        print(line)
        if not passed:
            any_failed = True

    print()
    print("=" * 60)
    if any_failed:
        print("OVERALL: HOLD / KILL — one or more stacks did not pass the gate.")
        print()
        print("Actions:")
        print("  - Re-run backtest after 30 days of live data.")
        print("  - To roll back to baseline:")
        print('    sqlite3 data/meteoedge.db "UPDATE bot_config SET value=\'baseline\' '
              'WHERE key=\'FORECAST_STACK\';"')
        sys.exit(1)
    else:
        print("OVERALL: PROMOTE — all stacks pass the MAE gate.")
        print()
        print("Next steps:")
        print("  - Set FORECAST_STACK to the appropriate value in DB config.")
        print("  - Monitor DEB weights and per-station MAE for the next 7 days.")
        sys.exit(0)


if __name__ == "__main__":
    main()
