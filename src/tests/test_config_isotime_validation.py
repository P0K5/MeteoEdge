"""COPY_LIVE_DRAWDOWN_SINCE validation on the dashboard config tab (issue #1317 follow-up)."""
from src.dashboard.api import _validate_config_value


def test_accepts_iso_timestamp():
    assert _validate_config_value("COPY_LIVE_DRAWDOWN_SINCE", "2026-10-05T00:00:00+00:00") == (
        "2026-10-05T00:00:00+00:00", None)


def test_accepts_z_suffix():
    value, err = _validate_config_value("COPY_LIVE_DRAWDOWN_SINCE", "2026-10-05T00:00:00Z")
    assert err is None and value == "2026-10-05T00:00:00Z"


def test_accepts_empty_as_all_time():
    assert _validate_config_value("COPY_LIVE_DRAWDOWN_SINCE", "") == ("", None)


def test_rejects_partial_date_with_clear_error():
    value, err = _validate_config_value("COPY_LIVE_DRAWDOWN_SINCE", "2026-10-0")
    assert value == "" and "ISO-8601" in err
