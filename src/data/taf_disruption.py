"""TAF disruption flag — confirmation layer for severe weather during peak windows.

TAF is a confirmation layer only. This module does NOT compute model probabilities
or scoring. It only flags whether a Temporary Fluctuation window containing
significant weather (TS/SH/FG) overlaps the peak settlement window.
"""
from __future__ import annotations

from src.data.db import Database

_DISRUPTION_CODES = frozenset({"TS", "SH", "FG"})
_DISRUPTION_GROUP = "Temporary Fluctuation"


def check_taf_disruption(
    city: str,
    db: Database,
    peak_start: str,
    peak_end: str,
) -> bool:
    """Return True when a TEMPO/PROB window with TS/SH/FG overlaps [peak_start, peak_end].

    All three conditions must hold:
    1. A taf_windows row for *city* has valid_from within [peak_start, peak_end]
    2. group_type == "Temporary Fluctuation" (TEMPO or PROB — not FM/BECMG/Base Period)
    3. sig_wx contains at least one of: TS, SH, FG

    Returns False when no TAF data exists, when no window overlaps, or when
    overlapping windows are not Temporary Fluctuation type.

    TAF is fail-open: when no data is available the flag is False so that
    the trade is NOT suppressed.
    """
    windows = db.get_taf_windows(city, from_ts=peak_start, to_ts=peak_end)
    for window in windows:
        if window.get("group_type") != _DISRUPTION_GROUP:
            continue
        sig_wx = window.get("sig_wx") or ""
        codes = set(sig_wx.split())
        if codes & _DISRUPTION_CODES:
            return True
    return False
