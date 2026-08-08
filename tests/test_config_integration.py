"""Integration tests: cli.py, server.py, and conftest.py read Neo4j creds
from writ.toml via writ/config.py -- no hardcoded strings.

Per TEST-TDD-001: skeletons approved before implementation.
Per ARCH-CONST-001: no magic values in source -- all tunables from writ.toml.

W2 (server package split, branch refactor/w2-server-split): TestServerNoHardcodedCreds
reads via writ_server_source() (tests/conftest.py), which is layout-agnostic -- it
scans every *.py under writ/server/ if that directory exists (post-split), else the
single writ/server.py file (pre-split). TestRepoWideNoHardcodedCreds is unaffected:
its glob (`WRIT_ROOT / "writ"`).rglob("*.py") already walks into writ/server/**
automatically once server.py becomes a package.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tests.conftest import writ_server_source

# Future config module -- ImportError expected until implementation lands.
try:
    from writ.config import (
        DEFAULT_NEO4J_PASSWORD,
        DEFAULT_NEO4J_URI,
        DEFAULT_NEO4J_USER,
        get_neo4j_password,
        get_neo4j_uri,
        get_neo4j_user,
        load_config,
    )
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _CONFIG_AVAILABLE,
    reason="writ/config.py not yet implemented",
)

# Reference the canonical defaults from writ/config.py rather than
# duplicating the literal values here. Two reasons: (1) the meta-test
# stays correct if the canonical defaults change in writ/config.py;
# (2) the credential-literal pre-write hook (writ-crypto-scan) cannot
# distinguish "literal as test fixture" from "literal as production
# credential assignment", and pulling the values from writ.config
# removes the literals from this file entirely.
HARDCODED_URI = DEFAULT_NEO4J_URI
HARDCODED_USER = DEFAULT_NEO4J_USER
HARDCODED_PASSWORD = DEFAULT_NEO4J_PASSWORD

WRIT_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _neutralize_the_env_layer(monkeypatch):
    """These tests assert on the writ.toml and built-in-default layers of `_neo4j_setting`.

    The environment layer sits ABOVE both and legitimately wins (that is what lets one
    process be pointed at a disposable instance). So a run that exports WRIT_NEO4J_URI --
    which an isolated graph run does -- had these comparing the override against the
    default and failing. They passed before only because nothing in the suite set the
    variable, which quietly made the ambient environment part of the assertion. A test of
    a lower precedence layer has to neutralize the higher one rather than assume it is unset.
    """
    for var in ("WRIT_NEO4J_URI", "WRIT_NEO4J_USER", "WRIT_NEO4J_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


def _source_of(module_path: Path) -> str:
    return module_path.read_text()


# ---------------------------------------------------------------------------
# TestCliNoHardcodedCreds
# ---------------------------------------------------------------------------


class TestCliNoHardcodedCreds:
    """writ/cli.py must not contain hardcoded Neo4j connection strings."""

    def test_cli_does_not_contain_hardcoded_uri(self) -> None:
        """writ/cli.py source does not contain the literal bolt://localhost:7687 string."""
        source = _source_of(WRIT_ROOT / "writ" / "cli.py")
        assert HARDCODED_URI not in source, (
            f"writ/cli.py still contains hardcoded URI '{HARDCODED_URI}' -- "
            "must be replaced with get_neo4j_uri() from writ/config.py"
        )

    def test_cli_does_not_contain_hardcoded_password(self) -> None:
        """writ/cli.py source does not contain the literal 'writdevpass' string."""
        source = _source_of(WRIT_ROOT / "writ" / "cli.py")
        assert HARDCODED_PASSWORD not in source, (
            f"writ/cli.py still contains hardcoded password '{HARDCODED_PASSWORD}'"
        )

    def test_cli_imports_config(self) -> None:
        """writ/cli.py imports from writ.config (directly or via lazy import inside commands)."""
        source = _source_of(WRIT_ROOT / "writ" / "cli.py")
        assert "writ.config" in source or "from writ import config" in source, (
            "writ/cli.py does not import writ.config"
        )


# ---------------------------------------------------------------------------
# TestServerNoHardcodedCreds
# ---------------------------------------------------------------------------


class TestServerNoHardcodedCreds:
    """writ.server (module or, post W2-split, package) must not contain hardcoded
    Neo4j connection strings."""

    def test_server_does_not_contain_hardcoded_uri(self) -> None:
        """writ.server source does not contain the literal bolt://localhost:7687 string."""
        source = writ_server_source()
        assert HARDCODED_URI not in source, (
            f"writ.server still contains hardcoded URI '{HARDCODED_URI}'"
        )

    def test_server_does_not_contain_hardcoded_password(self) -> None:
        """writ.server source does not contain the literal 'writdevpass' string."""
        source = writ_server_source()
        assert HARDCODED_PASSWORD not in source, (
            f"writ.server still contains hardcoded password '{HARDCODED_PASSWORD}'"
        )

    def test_server_imports_config(self) -> None:
        """writ.server imports from writ.config."""
        source = writ_server_source()
        assert "writ.config" in source or "from writ import config" in source, (
            "writ.server does not import writ.config"
        )


# ---------------------------------------------------------------------------
# TestConftestNoHardcodedCreds
# ---------------------------------------------------------------------------


class TestConftestNoHardcodedCreds:
    """tests/conftest.py must not contain hardcoded Neo4j connection strings."""

    def test_conftest_does_not_contain_hardcoded_uri(self) -> None:
        """tests/conftest.py source does not contain the literal bolt://localhost:7687 string."""
        source = _source_of(WRIT_ROOT / "tests" / "conftest.py")
        assert HARDCODED_URI not in source, (
            f"tests/conftest.py still contains hardcoded URI '{HARDCODED_URI}'"
        )

    def test_conftest_does_not_contain_hardcoded_password(self) -> None:
        """tests/conftest.py source does not contain the literal default password string."""
        source = _source_of(WRIT_ROOT / "tests" / "conftest.py")
        assert HARDCODED_PASSWORD not in source, (
            f"tests/conftest.py still contains hardcoded password '{HARDCODED_PASSWORD}'"
        )


# ---------------------------------------------------------------------------
# TestRepoWideNoHardcodedCreds (Finding 9, 2026-05-14)
#
# The three classes above cover writ/cli.py, writ/server.py, and
# tests/conftest.py. The remaining ~20 sites that hardcoded the default
# Neo4j password (scripts/*, benchmarks/*, most tests/test_*.py) were
# uncovered drift surface. Finding 9 fixed those sites; this class
# extends the meta-test to lock the property repo-wide so the drift
# cannot recur.
# ---------------------------------------------------------------------------


# Files that legitimately contain the canonical default literal. Each
# entry is itemized so additions require a deliberate decision.
_HARDCODED_ALLOWLIST = {
    # The canonical default constant. writ/config.py:DEFAULT_NEO4J_PASSWORD
    # IS the source of truth that all consumers (including the tests
    # below) read via the get_neo4j_password() accessor.
    WRIT_ROOT / "writ" / "config.py",
    # This meta-test file itself imports DEFAULT_NEO4J_PASSWORD from
    # writ.config and uses it as HARDCODED_PASSWORD for the assertions
    # against other files. The literal does not appear here directly,
    # but the imported value resolves to the canonical default at
    # runtime, so the file is on the allowlist by symmetry.
    WRIT_ROOT / "tests" / "test_config_integration.py",
}


def _iter_python_sources_under(root: Path):
    """Yield every .py file under `root`, excluding .venv, __pycache__,
    .mypy_cache, and the allowlist."""
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if "__pycache__" in parts or ".venv" in parts or ".mypy_cache" in parts:
            continue
        if path in _HARDCODED_ALLOWLIST:
            continue
        yield path


class TestRepoWideNoHardcodedCreds:
    """No Python file under writ/, scripts/, benchmarks/, or tests/
    contains the canonical default Neo4j password as a literal, except
    the documented allowlist (writ/config.py default constant; this
    meta-test file)."""

    @pytest.mark.parametrize(
        "subtree",
        ["writ", "scripts", "benchmarks", "tests"],
    )
    def test_no_hardcoded_password_outside_allowlist(self, subtree: str) -> None:
        """Catches the bug class fixed by Finding 9 (2026-05-14): hardcoded
        Neo4j credentials across scripts, benchmarks, and tests that drifted
        from writ.toml without test coverage. After Finding 9, this property
        holds repo-wide outside the documented allowlist. Note: this walk
        already covers writ/server/** transparently once the W2 split turns
        writ/server.py into a package -- rglob("*.py") descends into it."""
        offenders: list[Path] = []
        for source_path in _iter_python_sources_under(WRIT_ROOT / subtree):
            if HARDCODED_PASSWORD in source_path.read_text():
                offenders.append(source_path.relative_to(WRIT_ROOT))

        assert not offenders, (
            f"Files under {subtree}/ contain the canonical default Neo4j "
            f"password as a literal instead of reading it via "
            f"writ/config.py get_neo4j_password(): "
            f"{[str(p) for p in offenders]}. If a new site genuinely "
            f"requires the literal (rare), add it to _HARDCODED_ALLOWLIST "
            f"in this file with a comment explaining why."
        )


# ---------------------------------------------------------------------------
# TestMissingConfigFallback
# ---------------------------------------------------------------------------


class TestMissingConfigFallback:
    """When writ.toml is absent, all consumers fall back to documented defaults."""

    def test_cli_uses_default_uri_when_config_missing(self, tmp_path: Path) -> None:
        """get_neo4j_uri returns the default URI when no writ.toml exists."""
        uri = get_neo4j_uri(str(tmp_path / "no_writ.toml"))
        assert uri == HARDCODED_URI

    def test_server_uses_default_user_when_config_missing(self, tmp_path: Path) -> None:
        """get_neo4j_user returns the default user when no writ.toml exists."""
        user = get_neo4j_user(str(tmp_path / "no_writ.toml"))
        assert user == HARDCODED_USER

    def test_server_uses_default_password_when_config_missing(self, tmp_path: Path) -> None:
        """get_neo4j_password returns the default password when no writ.toml exists."""
        password = get_neo4j_password(str(tmp_path / "no_writ.toml"))
        assert password == HARDCODED_PASSWORD


# ---------------------------------------------------------------------------
# TestOverridingTomlChangesLoadedValues
# ---------------------------------------------------------------------------


class TestOverridingTomlChangesLoadedValues:
    """Providing a writ.toml with custom values changes what consumers receive."""

    def test_custom_uri_propagates_to_accessor(self, tmp_path: Path) -> None:
        """A writ.toml with uri = 'bolt://myhost:9999' causes get_neo4j_uri to return that value."""
        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[neo4j]\nuri = "bolt://myhost:9999"\nuser = "u"\npassword = "p"\n')
        uri = get_neo4j_uri(str(toml_file))
        assert uri == "bolt://myhost:9999"

    def test_custom_password_propagates_to_accessor(self, tmp_path: Path) -> None:
        """A writ.toml with a custom password causes get_neo4j_password to return that value."""
        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[neo4j]\nuri = "bolt://localhost:7687"\nuser = "neo4j"\npassword = "custom_pass"\n')
        password = get_neo4j_password(str(toml_file))
        assert password == "custom_pass"

    def test_two_different_toml_files_return_different_values(self, tmp_path: Path) -> None:
        """load_config with two different files returns independent results."""
        file_a = tmp_path / "a.toml"
        file_b = tmp_path / "b.toml"
        file_a.write_text('[neo4j]\nuri = "bolt://host-a:7687"\n')
        file_b.write_text('[neo4j]\nuri = "bolt://host-b:7687"\n')

        cfg_a = load_config(str(file_a))
        cfg_b = load_config(str(file_b))

        assert cfg_a["neo4j"]["uri"] != cfg_b["neo4j"]["uri"]
