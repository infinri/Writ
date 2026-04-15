"""Integration tests: cli.py, server.py, and conftest.py read Neo4j creds
from writ.toml via writ/config.py -- no hardcoded strings.

Per TEST-TDD-001: skeletons approved before implementation.
Per ARCH-CONST-001: no magic values in source -- all tunables from writ.toml.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Future config module -- ImportError expected until implementation lands.
try:
    from writ.config import load_config, get_neo4j_uri, get_neo4j_user, get_neo4j_password
    _CONFIG_AVAILABLE = True
except ImportError:
    _CONFIG_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _CONFIG_AVAILABLE,
    reason="writ/config.py not yet implemented",
)

HARDCODED_URI = "bolt://localhost:7687"
HARDCODED_USER = "neo4j"
HARDCODED_PASSWORD = "writdevpass"

WRIT_ROOT = Path(__file__).parent.parent


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
    """writ/server.py must not contain hardcoded Neo4j connection strings."""

    def test_server_does_not_contain_hardcoded_uri(self) -> None:
        """writ/server.py source does not contain the literal bolt://localhost:7687 string."""
        source = _source_of(WRIT_ROOT / "writ" / "server.py")
        assert HARDCODED_URI not in source, (
            f"writ/server.py still contains hardcoded URI '{HARDCODED_URI}'"
        )

    def test_server_does_not_contain_hardcoded_password(self) -> None:
        """writ/server.py source does not contain the literal 'writdevpass' string."""
        source = _source_of(WRIT_ROOT / "writ" / "server.py")
        assert HARDCODED_PASSWORD not in source, (
            f"writ/server.py still contains hardcoded password '{HARDCODED_PASSWORD}'"
        )

    def test_server_imports_config(self) -> None:
        """writ/server.py imports from writ.config."""
        source = _source_of(WRIT_ROOT / "writ" / "server.py")
        assert "writ.config" in source or "from writ import config" in source, (
            "writ/server.py does not import writ.config"
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
        """tests/conftest.py source does not contain the literal 'writdevpass' string."""
        source = _source_of(WRIT_ROOT / "tests" / "conftest.py")
        assert HARDCODED_PASSWORD not in source, (
            f"tests/conftest.py still contains hardcoded password '{HARDCODED_PASSWORD}'"
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
