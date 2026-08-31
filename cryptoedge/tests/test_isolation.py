"""Enforce the isolation invariant: cryptoedge must never touch the trading path.

The live-trading halt (#1053/#1054) is a safety invariant of this repo. This
package exists to research a DIFFERENT thesis and must be structurally incapable
of affecting the weather bot. That is a property worth a test, not a comment --
see cryptoedge/README.md.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
FORBIDDEN_PREFIXES = ("src.execution", "src.strategy", "src.model",
                      "src.data.db", "src.dashboard")
FORBIDDEN_LITERALS = ("meteoedge.db", "station_overrides", "open_positions",
                      "risk_state")

MODULES = sorted(p for p in PKG.glob("*.py"))


def _imported_names(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_trading_path_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for name in _imported_names(tree):
        for bad in FORBIDDEN_PREFIXES:
            assert not name.startswith(bad), (
                f"{path.name} imports {name!r} -- cryptoedge must not reach into "
                f"MeteoEdge's trading path (README.md isolation invariant)")


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_no_trading_table_or_db_literals(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            for bad in FORBIDDEN_LITERALS:
                # db.py names meteoedge.db only to REFUSE opening it
                if bad in low and path.name != "db.py":
                    raise AssertionError(
                        f"{path.name} references {bad!r} -- cryptoedge writes only "
                        f"to its own database (README.md isolation invariant)")


def test_db_connect_refuses_meteoedge_db(tmp_path):
    from cryptoedge import db
    with pytest.raises(ValueError, match="never open meteoedge.db"):
        db.connect(tmp_path / "meteoedge.db")


def test_db_connect_creates_schema(tmp_path):
    from cryptoedge import db
    con = db.connect(tmp_path / "cryptoedge.db")
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"quotes", "resolutions", "poll_runs"} <= tables
    con.close()


def test_no_order_placement_surface():
    """No module here may define or call anything that places an order."""
    for path in MODULES:
        src = path.read_text(encoding="utf-8").lower()
        for bad in ("post_order", "place_order", "create_order", "clob_client"):
            assert bad not in src, f"{path.name} references {bad!r}"
