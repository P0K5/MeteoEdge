"""TAF (Terminal Aerodrome Forecast) parser.

Converts raw TAF text into structured time-window records ready for DB
insertion.  Pure Python -- uses only ``re`` and ``datetime``; no external
aviation-weather parsing libraries.

Supported group types
---------------------
- Base period  -> "Base Period"
- FM           -> "Definite Change"
- TEMPO        -> "Temporary Fluctuation"
- BECMG        -> "Gradual Transition"
- PROB30/PROB40 -> "Temporary Fluctuation" (probabilistic TEMPO)

Output dict keys (match taf_windows table schema)
--------------------------------------------------
city, issued_at, valid_from, valid_to, group_type, temp, wind_kt,
sig_wx, raw_text
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Significant-weather codes that can trigger taf_disruption downstream.
_SIG_WX_CODES = ("TSRA", "TSGR", "TSGS", "TS", "SHRA", "SHSN", "SHGR", "SH", "FG", "FZFG")

# Group-type labels
_LABEL_BASE = "Base Period"
_LABEL_FM = "Definite Change"
_LABEL_TEMPO = "Temporary Fluctuation"
_LABEL_BECMG = "Gradual Transition"

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# TAF header: station DDHHMM Z DDhh/DDhh  (issued_at and overall validity)
_RE_HEADER = re.compile(
    r"([A-Z]{4})\s+"
    r"(\d{2})(\d{2})(\d{2})Z"
    r"\s+(\d{2})(\d{2})/(\d{2})(\d{2})"
)

# FM group: FM DDHHMM
_RE_FM = re.compile(r"\bFM(\d{2})(\d{2})(\d{2})\b")

# TEMPO / BECMG / PROB groups
_RE_PERIOD = re.compile(
    r"\b(TEMPO|BECMG|PROB30|PROB40)(?:\s+TEMPO)?\s+(\d{2})(\d{2})/(\d{2})(\d{2})\b"
)

# Wind: DDDssKT or VRBssKT
_RE_WIND = re.compile(r"\b(?:VRB|\d{3})(\d{2,3})(?:G\d{2,3})?KT\b")

# Temperature TX or T notation
_RE_TEMP_TX = re.compile(r"\bTX(M?\d+)/(\d{4})Z\b")
_RE_TEMP_T  = re.compile(r"\bT(M?\d+)(?:/(\d{4})Z)?\b")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_temp(raw: str) -> Optional[float]:
    """Convert TAF temp token (e.g. '25', 'M05') to float Celsius."""
    if raw.startswith("M"):
        return -float(raw[1:])
    return float(raw)


def _to_utc_iso(day: int, hour: int, minute: int, ref: datetime) -> str:
    """Build an ISO 8601 UTC string from a TAF day/hour/minute token.

    TAF validity periods may use hour=24 to denote midnight (start of day+1).
    We normalise that to day+1 at hour 0.
    """
    from datetime import timedelta
    year = ref.year
    month = ref.month
    if day < ref.day - 20:
        month += 1
        if month > 12:
            month = 1
            year += 1
    # Normalise hour=24 → next day at 00:00
    if hour == 24:
        dt = datetime(year, month, day, 0, minute, tzinfo=timezone.utc) + timedelta(days=1)
    else:
        dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _extract_wind_kt(text: str) -> Optional[float]:
    m = _RE_WIND.search(text)
    if m:
        return float(m.group(1))
    return None


def _extract_temp(text: str) -> Optional[float]:
    """Extract temperature from TX or T notation; returns Celsius or None."""
    m = _RE_TEMP_TX.search(text)
    if m:
        return _decode_temp(m.group(1))
    m = _RE_TEMP_T.search(text)
    if m:
        return _decode_temp(m.group(1))
    return None


def _extract_sig_wx(text: str) -> Optional[str]:
    """Extract significant weather codes present in *text*.

    Returns a space-separated string of matched codes, or None.
    CAVOK implicitly means no sig_wx.
    """
    if "CAVOK" in text:
        return None
    found = []
    seen_prefixes: set[str] = set()
    for code in _SIG_WX_CODES:
        if re.search(r"\b" + re.escape(code) + r"\b", text):
            prefix = code[:2]
            if prefix not in seen_prefixes:
                found.append(code)
                seen_prefixes.add(prefix)
    return " ".join(found) if found else None


def _extract_raw_taf(raw: str) -> str:
    """If *raw* looks like JSON, extract the TAF text from rawOb/rawTAF field."""
    stripped = raw.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, list) and data:
                data = data[0]
            if isinstance(data, dict):
                for key in ("rawOb", "rawTAF", "raw_text", "raw"):
                    if key in data and data[key]:
                        return str(data[key])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    return stripped


# ---------------------------------------------------------------------------
# Main parser class
# ---------------------------------------------------------------------------

class TafParser:
    """Parse raw TAF text into a list of time-window dicts."""

    def parse(self, raw_taf: str, city: str) -> list[dict]:
        """Parse *raw_taf* and return a list of window dicts for *city*.

        Accepts plain TAF text or JSON blob from aviationweather.gov.
        Returns empty list for empty/unrecognisable input without raising.
        """
        if not raw_taf or not raw_taf.strip():
            return []

        raw_taf = _extract_raw_taf(raw_taf)
        if not raw_taf:
            return []

        # Normalise whitespace
        text = " ".join(raw_taf.split())

        # Parse header
        hm = _RE_HEADER.search(text)
        if not hm:
            return []

        issue_day  = int(hm.group(2))
        issue_hour = int(hm.group(3))
        issue_min  = int(hm.group(4))
        valid_start_day  = int(hm.group(5))
        valid_start_hour = int(hm.group(6))
        valid_end_day    = int(hm.group(7))
        valid_end_hour   = int(hm.group(8))

        ref = datetime.now(timezone.utc)
        issued_at    = _to_utc_iso(issue_day, issue_hour, issue_min, ref)
        overall_from = _to_utc_iso(valid_start_day, valid_start_hour, 0, ref)
        overall_to   = _to_utc_iso(valid_end_day, valid_end_hour, 0, ref)

        # Split into segments at each change group
        _SPLIT_RE = re.compile(
            r"(?=\bFM\d{6}\b"
            r"|\bTEMPO\s+\d{4}/\d{4}\b"
            r"|\bBECMG\s+\d{4}/\d{4}\b"
            r"|\bPROB(?:30|40)(?:\s+TEMPO)?\s+\d{4}/\d{4}\b"
            r")"
        )
        parts = _SPLIT_RE.split(text)
        if not parts:
            return []

        base_text = parts[0]
        windows: list[dict] = []

        # Base period
        windows.append({
            "city":       city,
            "issued_at":  issued_at,
            "valid_from": overall_from,
            "valid_to":   overall_to,
            "group_type": _LABEL_BASE,
            "temp":       None if "CAVOK" in base_text else _extract_temp(base_text),
            "wind_kt":    _extract_wind_kt(base_text),
            "sig_wx":     _extract_sig_wx(base_text),
            "raw_text":   base_text.strip(),
        })

        # Change groups
        for part in parts[1:]:
            part = part.strip()
            if not part:
                continue

            # FM group
            fm_m = _RE_FM.match(part)
            if fm_m:
                fm_day  = int(fm_m.group(1))
                fm_hour = int(fm_m.group(2))
                fm_min  = int(fm_m.group(3))
                grp_from = _to_utc_iso(fm_day, fm_hour, fm_min, ref)
                windows.append({
                    "city":       city,
                    "issued_at":  issued_at,
                    "valid_from": grp_from,
                    "valid_to":   overall_to,
                    "group_type": _LABEL_FM,
                    "temp":       None if "CAVOK" in part else _extract_temp(part),
                    "wind_kt":    _extract_wind_kt(part),
                    "sig_wx":     _extract_sig_wx(part),
                    "raw_text":   part,
                })
                continue

            # TEMPO / BECMG / PROB groups
            period_m = _RE_PERIOD.search(part)
            if period_m:
                keyword     = period_m.group(1)
                p_from_day  = int(period_m.group(2))
                p_from_hour = int(period_m.group(3))
                p_to_day    = int(period_m.group(4))
                p_to_hour   = int(period_m.group(5))

                grp_from = _to_utc_iso(p_from_day, p_from_hour, 0, ref)
                grp_to   = _to_utc_iso(p_to_day, p_to_hour, 0, ref)

                if keyword == "BECMG":
                    label = _LABEL_BECMG
                else:
                    # TEMPO, PROB30, PROB40
                    label = _LABEL_TEMPO

                windows.append({
                    "city":       city,
                    "issued_at":  issued_at,
                    "valid_from": grp_from,
                    "valid_to":   grp_to,
                    "group_type": label,
                    "temp":       None if "CAVOK" in part else _extract_temp(part),
                    "wind_kt":    _extract_wind_kt(part),
                    "sig_wx":     _extract_sig_wx(part),
                    "raw_text":   part,
                })

        return windows
