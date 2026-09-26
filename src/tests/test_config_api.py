"""Tests for the DB-backed config parameter store (issue #234).

Covers:
- bot_config table creation
- get_config / set_config / get_all_config round-trips
- seed_config: first run seeds from env / defaults
- seed_config: skipped when row already exists (DB is authoritative)
- get_live_config: type casting
- GET /api/config response shape
- PATCH /api/config happy path
- PATCH /api/config invalid key
- PATCH /api/config out-of-bounds value
- PATCH /api/config enum validation (EMOS_DEFAULT_MODE)
"""
from __future__ import annotations

import os
import pytest
from fastapi.testclient import TestClient

from src.data.db import Database
from src.config import CONFIG_DEFAULTS, seed_config, get_live_config
from src.dashboard.api import _CONFIG_META


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db() -> Database:
    """Return a fresh in-memory Database instance."""
    return Database(":memory:")


# ---------------------------------------------------------------------------
# Database layer — bot_config table and methods
# ---------------------------------------------------------------------------

class TestBotConfigTable:
    """bot_config table must exist and support CRUD via the three helpers."""

    def test_table_exists(self):
        db = _db()
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='bot_config'"
        )
        assert cur.fetchone() is not None

    def test_get_config_missing_key_returns_none(self):
        db = _db()
        assert db.get_config("NONEXISTENT_KEY") is None

    def test_set_and_get_config(self):
        db = _db()
        db.set_config("MIN_EDGE_CENTS", "18.5")
        assert db.get_config("MIN_EDGE_CENTS") == "18.5"

    def test_set_config_upserts(self):
        """Second set_config for same key overwrites, not duplicates."""
        db = _db()
        db.set_config("MIN_EDGE_CENTS", "15.0")
        db.set_config("MIN_EDGE_CENTS", "20.0")
        assert db.get_config("MIN_EDGE_CENTS") == "20.0"
        cur = db._conn.execute("SELECT COUNT(*) FROM bot_config WHERE key='MIN_EDGE_CENTS'")
        assert cur.fetchone()[0] == 1

    def test_set_config_updated_at_is_set(self):
        db = _db()
        db.set_config("POLL_INTERVAL_SECONDS", "300")
        cur = db._conn.execute("SELECT updated_at FROM bot_config WHERE key='POLL_INTERVAL_SECONDS'")
        updated_at = cur.fetchone()[0]
        assert updated_at is not None
        assert len(updated_at) > 0

    def test_get_all_config_returns_dict(self):
        db = _db()
        db.set_config("KEY_A", "val_a")
        db.set_config("KEY_B", "val_b")
        result = db.get_all_config()
        assert result["KEY_A"] == "val_a"
        assert result["KEY_B"] == "val_b"

    def test_get_all_config_empty_db(self):
        db = _db()
        assert db.get_all_config() == {}


# ---------------------------------------------------------------------------
# seed_config
# ---------------------------------------------------------------------------

class TestSeedConfig:
    """seed_config seeds from env/defaults on first run; skips existing rows."""

    def test_first_run_seeds_all_defaults(self):
        db = _db()
        seed_config(db)
        all_cfg = db.get_all_config()
        for key in CONFIG_DEFAULTS:
            assert key in all_cfg, f"{key} was not seeded"

    def test_first_run_seeds_from_env(self, monkeypatch):
        db = _db()
        monkeypatch.setenv("MIN_EDGE_CENTS", "17.5")
        seed_config(db)
        assert db.get_config("MIN_EDGE_CENTS") == "17.5"

    def test_first_run_uses_hardcoded_default_when_env_not_set(self, monkeypatch):
        db = _db()
        monkeypatch.delenv("MIN_EDGE_CENTS", raising=False)
        seed_config(db)
        # Default for MIN_EDGE_CENTS is 15.0
        assert db.get_config("MIN_EDGE_CENTS") == "15.0"

    def test_existing_row_not_overwritten(self, monkeypatch):
        """If a DB row already exists, seed_config must leave it alone."""
        db = _db()
        db.set_config("MIN_EDGE_CENTS", "99.0")
        # Even if env says something different
        monkeypatch.setenv("MIN_EDGE_CENTS", "1.0")
        seed_config(db)
        # DB value must remain unchanged
        assert db.get_config("MIN_EDGE_CENTS") == "99.0"

    def test_seed_is_idempotent(self):
        """Calling seed_config twice must not raise or change values."""
        db = _db()
        seed_config(db)
        first = db.get_all_config()
        seed_config(db)
        second = db.get_all_config()
        assert first == second


# ---------------------------------------------------------------------------
# get_live_config
# ---------------------------------------------------------------------------

class TestGetLiveConfig:
    """get_live_config must return typed values for all seeded keys."""

    def test_returns_typed_values(self):
        db = _db()
        seed_config(db)
        cfg = get_live_config(db)
        assert isinstance(cfg["MIN_EDGE_CENTS"], float)
        assert isinstance(cfg["MIN_PRICE_CENTS"], int)
        assert isinstance(cfg["EMOS_DEFAULT_MODE"], str)

    def test_reflects_custom_db_values(self):
        db = _db()
        seed_config(db)
        db.set_config("POLL_INTERVAL_SECONDS", "600")
        cfg = get_live_config(db)
        assert cfg["POLL_INTERVAL_SECONDS"] == 600

    def test_bool_true_variants(self):
        db = _db()
        seed_config(db)
        for true_val in ("true", "True", "TRUE", "1", "yes"):
            db.set_config("RESIDUAL_CORRECTION_ENABLED", true_val)
            cfg = get_live_config(db)
            assert cfg["RESIDUAL_CORRECTION_ENABLED"] is True, f"Expected True for {true_val!r}"

    def test_bool_false_variants(self):
        db = _db()
        seed_config(db)
        for false_val in ("false", "False", "0", "no"):
            db.set_config("RESIDUAL_CORRECTION_ENABLED", false_val)
            cfg = get_live_config(db)
            assert cfg["RESIDUAL_CORRECTION_ENABLED"] is False, f"Expected False for {false_val!r}"


# ---------------------------------------------------------------------------
# API endpoints — GET /api/config and PATCH /api/config
# ---------------------------------------------------------------------------

@pytest.fixture()
def api_client(tmp_path):
    """Return a FastAPI TestClient with a seeded in-memory Database injected.

    Restores the original _db on teardown so module-level state does not leak
    into other test files.
    """
    from src.dashboard import api as api_module

    original_db = api_module._db
    db = Database(":memory:")
    seed_config(db)
    api_module.set_db(db)

    client = TestClient(api_module.app, raise_server_exceptions=True)
    yield client, db

    # Restore original DB so subsequent tests in other files aren't affected
    api_module.set_db(original_db)


class TestGetConfigEndpoint:
    """GET /api/config must return all parameters grouped by category."""

    def test_returns_200(self, api_client):
        client, _ = api_client
        resp = client.get("/api/config")
        assert resp.status_code == 200

    def test_response_is_grouped_by_category(self, api_client):
        client, _ = api_client
        data = client.get("/api/config").json()
        for group in ("strategy", "risk", "position", "timing"):
            assert group in data, f"Missing group: {group}"

    def test_each_param_has_required_fields(self, api_client):
        client, _ = api_client
        data = client.get("/api/config").json()
        for group, params in data.items():
            for key, entry in params.items():
                assert "value" in entry, f"{key} missing 'value'"
                assert "description" in entry, f"{key} missing 'description'"
                assert "type" in entry, f"{key} missing 'type'"

    def test_all_config_keys_present(self, api_client):
        """Every non-hidden config key must be present. Hidden keys (issue #852 —
        deprecated/no-op parameters like EMOS_SIGMA_SOURCE) are deliberately
        excluded from the response, so they're excluded from this check too."""
        client, _ = api_client
        data = client.get("/api/config").json()
        all_keys = {k for group in data.values() for k in group}
        for key in CONFIG_DEFAULTS:
            if _CONFIG_META.get(key, {}).get("hidden", False):
                assert key not in all_keys, f"Hidden key {key!r} unexpectedly present"
                continue
            assert key in all_keys, f"Key {key!r} missing from response"

    def test_value_types_are_correct(self, api_client):
        client, _ = api_client
        data = client.get("/api/config").json()
        # MIN_EDGE_CENTS should be float
        assert isinstance(data["strategy"]["MIN_EDGE_CENTS"]["value"], float)
        # MIN_PRICE_CENTS should be int
        assert isinstance(data["strategy"]["MIN_PRICE_CENTS"]["value"], int)
        # RESIDUAL_CORRECTION_ENABLED should be bool
        assert isinstance(data["strategy"]["RESIDUAL_CORRECTION_ENABLED"]["value"], bool)


class TestPatchConfigEndpoint:
    """PATCH /api/config must validate and persist parameter changes."""

    def test_patch_float_happy_path(self, api_client):
        client, db = api_client
        resp = client.patch("/api/config", json={"key": "MIN_EDGE_CENTS", "value": 18.0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["value"] == pytest.approx(18.0)
        # Value must be persisted in DB
        assert db.get_config("MIN_EDGE_CENTS") == "18.0"

    def test_patch_int_happy_path(self, api_client):
        client, db = api_client
        resp = client.patch("/api/config", json={"key": "POLL_INTERVAL_SECONDS", "value": 600})
        assert resp.status_code == 200
        assert resp.json()["value"] == 600
        assert db.get_config("POLL_INTERVAL_SECONDS") == "600"

    def test_patch_bool_happy_path(self, api_client):
        client, db = api_client
        resp = client.patch("/api/config", json={"key": "RESIDUAL_CORRECTION_ENABLED", "value": True})
        assert resp.status_code == 200
        assert resp.json()["value"] is True

    def test_patch_enum_happy_path(self, api_client):
        client, db = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "EMOS_DEFAULT_MODE", "value": "emos_shadow"},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] == "emos_shadow"
        assert db.get_config("EMOS_DEFAULT_MODE") == "emos_shadow"

    def test_patch_invalid_key_returns_400(self, api_client):
        client, _ = api_client
        resp = client.patch("/api/config", json={"key": "NONEXISTENT_KEY", "value": 42})
        assert resp.status_code == 400
        assert "Unknown config key" in resp.json()["detail"]

    def test_patch_out_of_bounds_float_returns_400(self, api_client):
        client, _ = api_client
        # MIN_EDGE_CENTS max is 50.0
        resp = client.patch("/api/config", json={"key": "MIN_EDGE_CENTS", "value": 999.0})
        assert resp.status_code == 400

    def test_patch_out_of_bounds_int_returns_400(self, api_client):
        client, _ = api_client
        # POLL_INTERVAL_SECONDS max is 3600
        resp = client.patch("/api/config", json={"key": "POLL_INTERVAL_SECONDS", "value": 99999})
        assert resp.status_code == 400

    def test_patch_invalid_enum_returns_400(self, api_client):
        client, _ = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "EMOS_DEFAULT_MODE", "value": "bad_mode"},
        )
        assert resp.status_code == 400

    def test_patch_below_min_float_returns_400(self, api_client):
        client, _ = api_client
        # MIN_EDGE_CENTS min is 1.0
        resp = client.patch("/api/config", json={"key": "MIN_EDGE_CENTS", "value": 0.0})
        assert resp.status_code == 400

    def test_patch_response_includes_description_and_type(self, api_client):
        client, _ = api_client
        resp = client.patch("/api/config", json={"key": "MAX_EDGE_CENTS", "value": 22.0})
        assert resp.status_code == 200
        data = resp.json()
        assert "description" in data
        assert "type" in data
        assert data["type"] == "float"


# ---------------------------------------------------------------------------
# _db=None guard — both config endpoints must return 503, not 500
# ---------------------------------------------------------------------------

@pytest.fixture()
def no_db_client():
    """TestClient with _db set to None to simulate uninitialised database."""
    from src.dashboard import api as api_module

    original_db = api_module._db
    api_module.set_db(None)

    client = TestClient(api_module.app, raise_server_exceptions=False)
    yield client

    api_module.set_db(original_db)


class TestConfigEndpointsWithoutDb:
    """GET and PATCH /api/config must return 503 when _db is None."""

    def test_get_config_returns_503_when_db_none(self, no_db_client):
        resp = no_db_client.get("/api/config")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Database not initialised"


# ---------------------------------------------------------------------------
# Copy-trading config keys (issue #1115 / epic #1100)
# ---------------------------------------------------------------------------

_COPY_KEYS = (
    "COPY_TRADING_ENABLED",
    "COPY_DEFAULT_FLAT_STAKE_USD",
    "COPY_MAX_WALLETS_FOLLOWED",
    "COPY_MAX_EXPOSURE_PER_WALLET_USD",
    "COPY_MAX_TOTAL_EXPOSURE_USD",
    "COPY_DAILY_LOSS_LIMIT_USD",
    "COPY_DRAWDOWN_STOP_PCT",
)


class TestCopyTradingConfigDefaults:
    """The copy-trading keys must load with correctly-typed defaults."""

    def test_all_copy_keys_in_config_defaults(self):
        for key in _COPY_KEYS:
            assert key in CONFIG_DEFAULTS, f"{key} missing from CONFIG_DEFAULTS"

    def test_get_live_config_returns_documented_defaults(self):
        db = _db()
        seed_config(db)
        cfg = get_live_config(db)
        assert cfg["COPY_TRADING_ENABLED"] is False
        assert isinstance(cfg["COPY_TRADING_ENABLED"], bool)
        assert cfg["COPY_DEFAULT_FLAT_STAKE_USD"] == pytest.approx(5.0)
        assert isinstance(cfg["COPY_DEFAULT_FLAT_STAKE_USD"], float)
        assert cfg["COPY_MAX_WALLETS_FOLLOWED"] == 10
        assert isinstance(cfg["COPY_MAX_WALLETS_FOLLOWED"], int)
        assert cfg["COPY_MAX_EXPOSURE_PER_WALLET_USD"] == pytest.approx(50.0)
        assert isinstance(cfg["COPY_MAX_EXPOSURE_PER_WALLET_USD"], float)
        assert cfg["COPY_MAX_TOTAL_EXPOSURE_USD"] == pytest.approx(250.0)
        assert isinstance(cfg["COPY_MAX_TOTAL_EXPOSURE_USD"], float)
        assert cfg["COPY_DAILY_LOSS_LIMIT_USD"] == pytest.approx(25.0)
        assert isinstance(cfg["COPY_DAILY_LOSS_LIMIT_USD"], float)
        assert cfg["COPY_DRAWDOWN_STOP_PCT"] == pytest.approx(0.20)
        assert isinstance(cfg["COPY_DRAWDOWN_STOP_PCT"], float)

    def test_all_copy_keys_have_config_meta(self):
        for key in _COPY_KEYS:
            assert key in _CONFIG_META, f"{key} missing from _CONFIG_META"
            assert _CONFIG_META[key]["group"] == "copy_trading"

    def test_copy_trading_group_present_in_get_config_response(self, api_client):
        client, _ = api_client
        data = client.get("/api/config").json()
        assert "copy_trading" in data
        for key in _COPY_KEYS:
            assert key in data["copy_trading"], f"{key} missing from copy_trading group"


class TestCopyTradingConfigBounds:
    """_validate_config_value must accept in-bounds and reject out-of-bounds values."""

    @pytest.mark.parametrize(
        "key,in_bounds,out_of_bounds",
        [
            ("COPY_DEFAULT_FLAT_STAKE_USD", 10.0, 9999.0),
            ("COPY_MAX_WALLETS_FOLLOWED", 5, 9999),
            ("COPY_MAX_EXPOSURE_PER_WALLET_USD", 100.0, 9999.0),
            # COPY_MAX_TOTAL_EXPOSURE_USD: in_bounds must not exceed COPY_TRADING_CAPITAL_USD (100.0)
            ("COPY_MAX_TOTAL_EXPOSURE_USD", 50.0, 99999.0),
            ("COPY_DAILY_LOSS_LIMIT_USD", 40.0, 9999.0),
            ("COPY_DRAWDOWN_STOP_PCT", 0.30, 1.5),
        ],
    )
    def test_patch_bounds(self, api_client, key, in_bounds, out_of_bounds):
        client, db = api_client
        resp = client.patch("/api/config", json={"key": key, "value": in_bounds})
        assert resp.status_code == 200, f"{key} rejected an in-bounds value: {resp.json()}"
        assert db.get_config(key) == str(in_bounds)

        resp = client.patch("/api/config", json={"key": key, "value": out_of_bounds})
        assert resp.status_code == 400, f"{key} accepted an out-of-bounds value"

    def test_patch_kill_switch_happy_path(self, api_client):
        client, db = api_client
        resp = client.patch("/api/config", json={"key": "COPY_TRADING_ENABLED", "value": True})
        assert resp.status_code == 200
        assert resp.json()["value"] is True
        assert db.get_config("COPY_TRADING_ENABLED") == "true"


class TestCopyTradingKillSwitchIsolation:
    """Toggling COPY_TRADING_ENABLED must not affect weather-strategy keys, and
    vice versa (issue #1115 acceptance criteria: kill-switch isolation)."""

    def test_toggling_copy_switch_does_not_change_weather_keys(self):
        db = _db()
        seed_config(db)
        deb_before = get_live_config(db)["DEB_ENABLED"]
        residual_before = get_live_config(db)["RESIDUAL_CORRECTION_ENABLED"]

        db.set_config("COPY_TRADING_ENABLED", "True")

        cfg = get_live_config(db)
        assert cfg["COPY_TRADING_ENABLED"] is True
        assert cfg["DEB_ENABLED"] == deb_before
        assert cfg["RESIDUAL_CORRECTION_ENABLED"] == residual_before

    def test_toggling_weather_key_does_not_change_copy_switch(self):
        db = _db()
        seed_config(db)
        copy_before = get_live_config(db)["COPY_TRADING_ENABLED"]

        db.set_config("DEB_ENABLED", "True")
        db.set_config("RESIDUAL_CORRECTION_ENABLED", "False")

        cfg = get_live_config(db)
        assert cfg["DEB_ENABLED"] is True
        assert cfg["RESIDUAL_CORRECTION_ENABLED"] is False
        assert cfg["COPY_TRADING_ENABLED"] == copy_before

    def test_patch_config_returns_503_when_db_none(self, no_db_client):
        resp = no_db_client.patch("/api/config", json={"key": "MIN_EDGE_CENTS", "value": 18.0})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "Database not initialised"


# ---------------------------------------------------------------------------
# Live copy-trading config keys (issue #1163 / epic G #1158) -- mirrors the
# paper _COPY_KEYS block above exactly one layer up.
# ---------------------------------------------------------------------------

_COPY_LIVE_KEYS = (
    "COPY_LIVE_TRADING_ENABLED",
    "COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD",
    "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD",
)


class TestCopyLiveTradingConfigDefaults:
    """The live copy-trading keys must load with correctly-typed defaults."""

    def test_all_copy_live_keys_in_config_defaults(self):
        for key in _COPY_LIVE_KEYS:
            assert key in CONFIG_DEFAULTS, f"{key} missing from CONFIG_DEFAULTS"

    def test_get_live_config_returns_documented_defaults(self):
        db = _db()
        seed_config(db)
        cfg = get_live_config(db)
        assert cfg["COPY_LIVE_TRADING_ENABLED"] is False
        assert isinstance(cfg["COPY_LIVE_TRADING_ENABLED"], bool)
        assert cfg["COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD"] == pytest.approx(50.0)
        assert isinstance(cfg["COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD"], float)
        assert cfg["COPY_LIVE_MAX_TOTAL_EXPOSURE_USD"] == pytest.approx(250.0)
        assert isinstance(cfg["COPY_LIVE_MAX_TOTAL_EXPOSURE_USD"], float)

    def test_all_copy_live_keys_have_config_meta(self):
        for key in _COPY_LIVE_KEYS:
            assert key in _CONFIG_META, f"{key} missing from _CONFIG_META"
            assert _CONFIG_META[key]["group"] == "copy_trading"

    def test_copy_trading_group_contains_live_keys_in_get_config_response(self, api_client):
        client, _ = api_client
        data = client.get("/api/config").json()
        assert "copy_trading" in data
        for key in _COPY_LIVE_KEYS:
            assert key in data["copy_trading"], f"{key} missing from copy_trading group"


class TestCopyLiveTradingConfigBounds:
    """_validate_config_value must accept in-bounds and reject out-of-bounds
    values for the live numeric keys."""

    @pytest.mark.parametrize(
        "key,in_bounds,out_of_bounds",
        [
            ("COPY_LIVE_MAX_EXPOSURE_PER_WALLET_USD", 100.0, 9999.0),
            # Note: COPY_LIVE_MAX_TOTAL_EXPOSURE_USD capital ceiling validation is tested in
            # TestCapitalCeilingValidation with monkeypatched capital values, since the default
            # COPY_LIVE_CAPITAL_USD is 0.0 (live trading disabled by default).
        ],
    )
    def test_patch_bounds(self, api_client, key, in_bounds, out_of_bounds):
        client, db = api_client
        resp = client.patch("/api/config", json={"key": key, "value": in_bounds})
        assert resp.status_code == 200, f"{key} rejected an in-bounds value: {resp.json()}"
        assert db.get_config(key) == str(in_bounds)

        resp = client.patch("/api/config", json={"key": key, "value": out_of_bounds})
        assert resp.status_code == 400, f"{key} accepted an out-of-bounds value"

    def test_patch_bounds_live_with_capital(self, api_client, monkeypatch):
        """COPY_LIVE_MAX_TOTAL_EXPOSURE_USD: test both min/max bounds and capital ceiling."""
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_LIVE_CAPITAL_USD", 250.0)

        client, db = api_client
        # In-bounds (< min/max AND < capital): should succeed
        resp = client.patch("/api/config", json={"key": "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD", "value": 100.0})
        assert resp.status_code == 200, f"In-bounds rejected: {resp.json()}"
        assert db.get_config("COPY_LIVE_MAX_TOTAL_EXPOSURE_USD") == "100.0"

        # Out-of-bounds (> min/max, but < capital): should fail on bounds
        resp = client.patch("/api/config", json={"key": "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD", "value": 99999.0})
        assert resp.status_code == 400, "Out-of-bounds bounds check failed"

        # Exceeds capital (< min/max bounds but > capital): should fail on ceiling
        resp = client.patch("/api/config", json={"key": "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD", "value": 300.0})
        assert resp.status_code == 400, "Capital ceiling check failed"

    def test_patch_live_kill_switch_happy_path(self, api_client):
        client, db = api_client
        resp = client.patch(
            "/api/config", json={"key": "COPY_LIVE_TRADING_ENABLED", "value": True}
        )
        assert resp.status_code == 200
        assert resp.json()["value"] is True
        assert db.get_config("COPY_LIVE_TRADING_ENABLED") == "true"


class TestCopyLiveTradingKillSwitchIsolation:
    """Toggling COPY_LIVE_TRADING_ENABLED must not affect COPY_TRADING_ENABLED
    (paper) or weather-strategy keys, and vice versa (issue #1163 acceptance
    criteria: live/paper kill-switch independence)."""

    def test_toggling_live_switch_does_not_change_paper_switch(self):
        db = _db()
        seed_config(db)
        paper_before = get_live_config(db)["COPY_TRADING_ENABLED"]

        db.set_config("COPY_LIVE_TRADING_ENABLED", "True")

        cfg = get_live_config(db)
        assert cfg["COPY_LIVE_TRADING_ENABLED"] is True
        assert cfg["COPY_TRADING_ENABLED"] == paper_before

    def test_toggling_paper_switch_does_not_change_live_switch(self):
        db = _db()
        seed_config(db)
        live_before = get_live_config(db)["COPY_LIVE_TRADING_ENABLED"]

        db.set_config("COPY_TRADING_ENABLED", "True")

        cfg = get_live_config(db)
        assert cfg["COPY_TRADING_ENABLED"] is True
        assert cfg["COPY_LIVE_TRADING_ENABLED"] == live_before

    def test_toggling_live_switch_does_not_change_weather_keys(self):
        db = _db()
        seed_config(db)
        deb_before = get_live_config(db)["DEB_ENABLED"]
        residual_before = get_live_config(db)["RESIDUAL_CORRECTION_ENABLED"]

        db.set_config("COPY_LIVE_TRADING_ENABLED", "True")

        cfg = get_live_config(db)
        assert cfg["COPY_LIVE_TRADING_ENABLED"] is True
        assert cfg["DEB_ENABLED"] == deb_before
        assert cfg["RESIDUAL_CORRECTION_ENABLED"] == residual_before


# ---------------------------------------------------------------------------
# Issue #1215: Reject exposure caps above their capital pool at config write time
# ---------------------------------------------------------------------------

class TestCapitalCeilingValidation:
    """Cross-field validation: exposure caps must not exceed their capital pools."""

    def test_patch_copy_max_exposure_below_capital_returns_200(self, api_client, monkeypatch):
        """COPY_MAX_TOTAL_EXPOSURE_USD < COPY_TRADING_CAPITAL_USD must be accepted."""
        # Patch the constant to make the test deterministic
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_TRADING_CAPITAL_USD", 500.0)

        client, db = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_MAX_TOTAL_EXPOSURE_USD", "value": 100.0}
        )
        assert resp.status_code == 200, f"Unexpected error: {resp.json()}"
        assert resp.json()["value"] == pytest.approx(100.0)
        assert db.get_config("COPY_MAX_TOTAL_EXPOSURE_USD") == "100.0"

    def test_patch_copy_max_exposure_equals_capital_returns_200(self, api_client, monkeypatch):
        """COPY_MAX_TOTAL_EXPOSURE_USD == COPY_TRADING_CAPITAL_USD must be accepted.

        This is the boundary case and must be allowed per acceptance criteria.
        Production's current config has this configuration (500 = 500).
        """
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_TRADING_CAPITAL_USD", 500.0)

        client, db = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_MAX_TOTAL_EXPOSURE_USD", "value": 500.0}
        )
        assert resp.status_code == 200, f"Boundary case failed: {resp.json()}"
        assert resp.json()["value"] == pytest.approx(500.0)
        assert db.get_config("COPY_MAX_TOTAL_EXPOSURE_USD") == "500.0"

    def test_patch_copy_max_exposure_exceeds_capital_returns_400(self, api_client, monkeypatch):
        """COPY_MAX_TOTAL_EXPOSURE_USD > COPY_TRADING_CAPITAL_USD must be rejected.

        This reproduces the production outage: 500 cap against 100 pool.
        """
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_TRADING_CAPITAL_USD", 100.0)

        client, _ = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_MAX_TOTAL_EXPOSURE_USD", "value": 500.0}
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "COPY_MAX_TOTAL_EXPOSURE_USD" in detail
        assert "COPY_TRADING_CAPITAL_USD" in detail
        assert "100.00" in detail or "100.0" in detail
        assert "500.00" in detail or "500.0" in detail

    def test_patch_copy_live_max_exposure_below_capital_returns_200(self, api_client, monkeypatch):
        """COPY_LIVE_MAX_TOTAL_EXPOSURE_USD < COPY_LIVE_CAPITAL_USD must be accepted."""
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_LIVE_CAPITAL_USD", 250.0)

        client, db = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD", "value": 100.0}
        )
        assert resp.status_code == 200, f"Unexpected error: {resp.json()}"
        assert resp.json()["value"] == pytest.approx(100.0)
        assert db.get_config("COPY_LIVE_MAX_TOTAL_EXPOSURE_USD") == "100.0"

    def test_patch_copy_live_max_exposure_equals_capital_returns_200(self, api_client, monkeypatch):
        """COPY_LIVE_MAX_TOTAL_EXPOSURE_USD == COPY_LIVE_CAPITAL_USD must be accepted."""
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_LIVE_CAPITAL_USD", 250.0)

        client, db = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD", "value": 250.0}
        )
        assert resp.status_code == 200, f"Boundary case failed: {resp.json()}"
        assert resp.json()["value"] == pytest.approx(250.0)
        assert db.get_config("COPY_LIVE_MAX_TOTAL_EXPOSURE_USD") == "250.0"

    def test_patch_copy_live_max_exposure_exceeds_capital_returns_400(self, api_client, monkeypatch):
        """COPY_LIVE_MAX_TOTAL_EXPOSURE_USD > COPY_LIVE_CAPITAL_USD must be rejected."""
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_LIVE_CAPITAL_USD", 100.0)

        client, _ = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD", "value": 200.0}
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "COPY_LIVE_MAX_TOTAL_EXPOSURE_USD" in detail
        assert "COPY_LIVE_CAPITAL_USD" in detail
        assert "100.00" in detail or "100.0" in detail
        assert "200.00" in detail or "200.0" in detail

    def test_patch_unrelated_key_still_works(self, api_client, monkeypatch):
        """Unrelated config keys (e.g., COPY_DEFAULT_FLAT_STAKE_USD) must not be affected."""
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_TRADING_CAPITAL_USD", 100.0)

        client, db = api_client
        # This should succeed because COPY_DEFAULT_FLAT_STAKE_USD is not an exposure cap
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_DEFAULT_FLAT_STAKE_USD", "value": 10.0}
        )
        assert resp.status_code == 200, f"Unrelated key rejected: {resp.json()}"
        assert resp.json()["value"] == pytest.approx(10.0)
        assert db.get_config("COPY_DEFAULT_FLAT_STAKE_USD") == "10.0"

    def test_patch_capital_ceiling_message_names_both_params_and_values(self, api_client, monkeypatch):
        """Error message must name both keys and both values per acceptance criteria."""
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_TRADING_CAPITAL_USD", 123.45)

        client, _ = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_MAX_TOTAL_EXPOSURE_USD", "value": 234.56}
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        # Message must name the exposure cap key
        assert "COPY_MAX_TOTAL_EXPOSURE_USD" in detail
        # Message must name the capital key
        assert "COPY_TRADING_CAPITAL_USD" in detail
        # Message must name both values
        assert "234.56" in detail or "234.5" in detail
        assert "123.45" in detail or "123.4" in detail or "123.5" in detail

    def test_patch_string_input_bypass_must_reject(self, api_client, monkeypatch):
        """String input ("500" instead of 500) must NOT bypass capital ceiling check.

        ConfigPatchRequest.value is typed Any and _validate_config_value accepts
        loose input (coerces "500" to float), so prior implementations were
        vulnerable to string bypass. This test pins that the gate holds even when
        the caller sends a string instead of a number.
        """
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_TRADING_CAPITAL_USD", 100.0)

        client, _ = api_client
        # Send as string "500" instead of numeric 500
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_MAX_TOTAL_EXPOSURE_USD", "value": "500"}
        )
        # Must be rejected, not written to DB (which would recreate the latent-fatal config)
        assert resp.status_code == 400, (
            "String bypass vulnerability: ceiling check was skipped when value "
            "was sent as string instead of numeric type"
        )
        detail = resp.json()["detail"]
        # Message should still name both parameters
        assert "COPY_MAX_TOTAL_EXPOSURE_USD" in detail
        assert "COPY_TRADING_CAPITAL_USD" in detail

    def test_regression_production_outage_500_vs_100(self, api_client, monkeypatch):
        """Regression test for production outage on 2026-09-26.

        The exact values that triggered the crash loop (500 cap against 100 pool)
        must be rejected at write time (issue #1215).
        """
        from src.dashboard import api as api_module
        monkeypatch.setattr(api_module, "COPY_TRADING_CAPITAL_USD", 100.0)

        client, _ = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "COPY_MAX_TOTAL_EXPOSURE_USD", "value": 500.0}
        )
        assert resp.status_code == 400, (
            "Regression test failed: the values that caused the production "
            "outage (500 cap vs 100 pool) must be rejected at config write time"
        )
        detail = resp.json()["detail"]
        assert "500" in detail and "100" in detail


# ---------------------------------------------------------------------------
# Issue #451: USE_ENSEMBLE_SIGMA promotion gate
# ---------------------------------------------------------------------------

class TestUseEnsembleSigmaConfig:
    """Tests for USE_ENSEMBLE_SIGMA flag wiring (issue #451)."""

    def test_use_ensemble_sigma_in_config_defaults(self):
        """USE_ENSEMBLE_SIGMA must be in CONFIG_DEFAULTS.

        Issue #799: default flipped True -- sigma_raw now sources from the
        real per-row ensemble spread by default, and this is the single flag
        Database._active_sigma_source() keys the retrain/serve coefficient
        track on.
        """
        assert "USE_ENSEMBLE_SIGMA" in CONFIG_DEFAULTS
        assert CONFIG_DEFAULTS["USE_ENSEMBLE_SIGMA"] is True

    def test_use_ensemble_sigma_seeded_as_true(self):
        """USE_ENSEMBLE_SIGMA must seed to True by default (issue #799)."""
        db = _db()
        seed_config(db)
        cfg = get_live_config(db)
        assert cfg["USE_ENSEMBLE_SIGMA"] is True
        assert isinstance(cfg["USE_ENSEMBLE_SIGMA"], bool)

    def test_use_ensemble_sigma_can_be_set_to_false(self):
        """USE_ENSEMBLE_SIGMA must still accept an explicit False (legacy 'fixed' track)."""
        db = _db()
        seed_config(db)
        db.set_config("USE_ENSEMBLE_SIGMA", "false")
        cfg = get_live_config(db)
        assert cfg["USE_ENSEMBLE_SIGMA"] is False

    def test_use_ensemble_sigma_can_be_set_to_true(self):
        """USE_ENSEMBLE_SIGMA must accept True value."""
        db = _db()
        seed_config(db)
        db.set_config("USE_ENSEMBLE_SIGMA", "true")
        cfg = get_live_config(db)
        assert cfg["USE_ENSEMBLE_SIGMA"] is True

    def test_use_ensemble_sigma_api_endpoint(self, api_client):
        """GET /api/config must include USE_ENSEMBLE_SIGMA."""
        client, _ = api_client
        data = client.get("/api/config").json()
        # USE_ENSEMBLE_SIGMA is in the 'forecast' group
        assert "forecast" in data
        assert "USE_ENSEMBLE_SIGMA" in data["forecast"]
        param = data["forecast"]["USE_ENSEMBLE_SIGMA"]
        assert param["type"] == "bool"
        assert param["value"] is True  # issue #799: default flipped on

    def test_use_ensemble_sigma_patch_endpoint(self, api_client):
        """PATCH /api/config must accept USE_ENSEMBLE_SIGMA changes."""
        client, db = api_client
        resp = client.patch(
            "/api/config",
            json={"key": "USE_ENSEMBLE_SIGMA", "value": True},
        )
        assert resp.status_code == 200
        assert resp.json()["value"] is True
        assert db.get_config("USE_ENSEMBLE_SIGMA") == "true"

    def test_use_ensemble_sigma_has_description(self, api_client):
        """USE_ENSEMBLE_SIGMA must have a description in metadata."""
        client, _ = api_client
        data = client.get("/api/config").json()
        param = data["forecast"]["USE_ENSEMBLE_SIGMA"]
        assert "description" in param
        assert len(param["description"]) > 0
        assert "ensemble" in param["description"].lower()
