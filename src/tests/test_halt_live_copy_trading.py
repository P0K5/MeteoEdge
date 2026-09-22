"""Tests for src/scripts/halt_live_copy_trading.py -- the explicit live
copy-trading halt (issue #1176, epic I #1160)."""
from __future__ import annotations

from src.config import CONFIG_DEFAULTS, seed_config
from src.data.db import Database
from src.scripts.halt_live_copy_trading import _KEY, halt_live_copy_trading


def _mem_db() -> Database:
    return Database(":memory:")


def _updated_at(db: Database, key: str) -> "str | None":
    cur = db._conn.execute("SELECT updated_at FROM bot_config WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else None


def test_halts_when_currently_enabled():
    db = _mem_db()
    try:
        db.set_config(_KEY, "true")
        changed = halt_live_copy_trading(db)
        assert changed is True
        assert db.get_config(_KEY) == "false"
    finally:
        db._conn.close()


def test_noop_when_no_prior_row():
    """Default is off (CONFIG_DEFAULTS[_KEY] is False) -- a fresh DB with no
    bot_config row at all must be treated as already-halted, and the script
    must not create a row just to report the no-op."""
    db = _mem_db()
    try:
        changed = halt_live_copy_trading(db)
        assert changed is False
        assert db.get_config(_KEY) is None
    finally:
        db._conn.close()


def test_second_run_is_a_noop_and_does_not_retouch_row():
    db = _mem_db()
    try:
        db.set_config(_KEY, "true")
        halt_live_copy_trading(db)
        updated_at_after_halt = _updated_at(db, _KEY)

        changed_again = halt_live_copy_trading(db)

        assert changed_again is False
        assert db.get_config(_KEY) == "false"
        assert _updated_at(db, _KEY) == updated_at_after_halt
    finally:
        db._conn.close()


def test_dry_run_reports_but_does_not_write():
    db = _mem_db()
    try:
        db.set_config(_KEY, "true")
        changed = halt_live_copy_trading(db, dry_run=True)
        assert changed is True
        assert db.get_config(_KEY) == "true"
    finally:
        db._conn.close()


def test_dry_run_noop_when_already_off_creates_no_row():
    db = _mem_db()
    try:
        changed = halt_live_copy_trading(db, dry_run=True)
        assert changed is False
        assert db.get_config(_KEY) is None
    finally:
        db._conn.close()


def test_never_touches_other_config_keys():
    db = _mem_db()
    try:
        seed_config(db)
        db.set_config(_KEY, "true")
        before = db.get_all_config()

        halt_live_copy_trading(db)

        after = db.get_all_config()
        for key, value in before.items():
            if key == _KEY:
                continue
            assert after[key] == value, f"{key} was touched by halt_live_copy_trading"
        # No new keys were created either.
        assert set(after) == set(before)
    finally:
        db._conn.close()


def test_never_touches_station_overrides():
    db = _mem_db()
    try:
        db.set_config(_KEY, "true")
        halt_live_copy_trading(db)
        # No station override rows should exist -- this script has no
        # business anywhere near station_overrides.
        assert db.get_station_override("KJFK") is None
    finally:
        db._conn.close()


def test_key_default_matches_config_defaults():
    """Sanity check that _KEY's fallback semantics stay in sync with
    CONFIG_DEFAULTS if that default is ever changed."""
    assert CONFIG_DEFAULTS[_KEY] is False
