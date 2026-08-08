"""Unit tests for writ/config.py -- centralized writ.toml loader.

Per TEST-TDD-001: skeletons approved before implementation.
Per ARCH-CONST-001: all tunables must live in writ.toml with named constant defaults.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Import the future config module -- ImportError is expected until impl lands
# ---------------------------------------------------------------------------

try:
    from writ.config import (
        load_config,
        get_neo4j_uri,
        get_neo4j_user,
        get_neo4j_password,
        get_hnsw_cache_dir,
        DEFAULT_NEO4J_URI,
        DEFAULT_NEO4J_USER,
        DEFAULT_NEO4J_PASSWORD,
        DEFAULT_HNSW_CACHE_DIR,
    )
except ImportError:
    pytestmark = pytest.mark.skip(reason="writ/config.py not yet implemented")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


@pytest.fixture()
def minimal_toml(tmp_path) -> str:
    """Write a minimal writ.toml and return its path."""
    toml_file = tmp_path / "writ.toml"
    toml_file.write_text(
        '[neo4j]\nuri = "bolt://custom:7687"\nuser = "admin"\npassword = "secret"\n'
        '[hnsw]\ncache_dir = "/tmp/hnsw_test"\n'
    )
    return str(toml_file)


@pytest.fixture()
def partial_toml(tmp_path) -> str:
    """writ.toml with only the neo4j section -- hnsw section missing."""
    toml_file = tmp_path / "writ.toml"
    toml_file.write_text('[neo4j]\nuri = "bolt://partial:7687"\n')
    return str(toml_file)


@pytest.fixture()
def empty_toml(tmp_path) -> str:
    """Completely empty writ.toml."""
    toml_file = tmp_path / "writ.toml"
    toml_file.write_text("")
    return str(toml_file)


# ---------------------------------------------------------------------------
# TestLoadConfig -- raw dict loading
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """load_config() returns a dict from a writ.toml file."""

    def test_loads_full_file(self, minimal_toml: str) -> None:
        """Full writ.toml loads without error and returns a dict."""
        result = load_config(minimal_toml)
        assert isinstance(result, dict)

    def test_returns_neo4j_section(self, minimal_toml: str) -> None:
        """Loaded config contains a 'neo4j' key with the correct values."""
        result = load_config(minimal_toml)
        assert result.get("neo4j", {}).get("uri") == "bolt://custom:7687"

    def test_returns_hnsw_section(self, minimal_toml: str) -> None:
        """Loaded config contains an 'hnsw' key with cache_dir."""
        result = load_config(minimal_toml)
        assert result.get("hnsw", {}).get("cache_dir") == "/tmp/hnsw_test"

    def test_missing_file_returns_empty_dict(self, tmp_path) -> None:
        """When writ.toml does not exist, load_config returns {}."""
        result = load_config(str(tmp_path / "no_such_file.toml"))
        assert result == {}

    def test_empty_file_returns_empty_dict(self, empty_toml: str) -> None:
        """Empty writ.toml returns {}."""
        result = load_config(empty_toml)
        assert result == {}


# ---------------------------------------------------------------------------
# TestDefaults -- typed accessors fall back to constants
# ---------------------------------------------------------------------------


class TestDefaults:
    """Typed accessors return documented defaults when config is absent."""

    def test_neo4j_uri_default(self, tmp_path) -> None:
        """get_neo4j_uri returns DEFAULT_NEO4J_URI when file is missing."""
        uri = get_neo4j_uri(str(tmp_path / "missing.toml"))
        assert uri == DEFAULT_NEO4J_URI

    def test_neo4j_user_default(self, tmp_path) -> None:
        """get_neo4j_user returns DEFAULT_NEO4J_USER when file is missing."""
        user = get_neo4j_user(str(tmp_path / "missing.toml"))
        assert user == DEFAULT_NEO4J_USER

    def test_neo4j_password_default(self, tmp_path) -> None:
        """get_neo4j_password returns DEFAULT_NEO4J_PASSWORD when file is missing."""
        password = get_neo4j_password(str(tmp_path / "missing.toml"))
        assert password == DEFAULT_NEO4J_PASSWORD

    def test_hnsw_cache_dir_default(self, tmp_path) -> None:
        """get_hnsw_cache_dir returns DEFAULT_HNSW_CACHE_DIR when hnsw section absent."""
        cache_dir = get_hnsw_cache_dir(str(tmp_path / "missing.toml"))
        assert cache_dir == DEFAULT_HNSW_CACHE_DIR

    def test_partial_config_uses_defaults_for_missing_keys(self, partial_toml: str) -> None:
        """Accessor for a missing key falls back to default even when file exists."""
        cache_dir = get_hnsw_cache_dir(partial_toml)
        assert cache_dir == DEFAULT_HNSW_CACHE_DIR


# ---------------------------------------------------------------------------
# TestOverride -- explicit values take precedence over defaults
# ---------------------------------------------------------------------------


class TestOverride:
    """Values in writ.toml override all defaults."""

    def test_neo4j_uri_overridden(self, minimal_toml: str) -> None:
        """URI from writ.toml replaces the default."""
        uri = get_neo4j_uri(minimal_toml)
        assert uri == "bolt://custom:7687"
        assert uri != DEFAULT_NEO4J_URI

    def test_neo4j_user_overridden(self, minimal_toml: str) -> None:
        """User from writ.toml replaces the default."""
        user = get_neo4j_user(minimal_toml)
        assert user == "admin"

    def test_neo4j_password_overridden(self, minimal_toml: str) -> None:
        """Password from writ.toml replaces the default."""
        password = get_neo4j_password(minimal_toml)
        assert password == "secret"

    def test_hnsw_cache_dir_overridden(self, minimal_toml: str) -> None:
        """cache_dir from writ.toml replaces the default."""
        cache_dir = get_hnsw_cache_dir(minimal_toml)
        assert cache_dir == "/tmp/hnsw_test"

    def test_hnsw_cache_dir_expands_tilde(self, tmp_path) -> None:
        """A tilde override in writ.toml must be expanded, not left literal.

        Regression: an unexpanded '~' made hnswlib create a literal '~/' dir
        wherever the process ran. The getter must expand via expanduser.
        """
        import os

        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[hnsw]\ncache_dir = "~/my_writ_cache"\n')
        cache_dir = get_hnsw_cache_dir(str(toml_file))
        assert "~" not in cache_dir, (
            f"tilde must be expanded, got: {cache_dir!r}"
        )
        assert cache_dir == os.path.expanduser("~/my_writ_cache")


# ---------------------------------------------------------------------------
# TestConsumers -- expected import surface for downstream modules
# ---------------------------------------------------------------------------


class TestConsumers:
    """load_config and typed accessors are importable by all consumer modules."""

    def test_cli_can_import_config(self) -> None:
        """writ/cli.py can import get_neo4j_uri, get_neo4j_user, get_neo4j_password."""
        from writ.config import get_neo4j_uri, get_neo4j_user, get_neo4j_password
        assert callable(get_neo4j_uri)
        assert callable(get_neo4j_user)
        assert callable(get_neo4j_password)

    def test_server_can_import_config(self) -> None:
        """writ/server.py can import get_neo4j_uri, get_neo4j_user, get_neo4j_password."""
        from writ.config import get_neo4j_uri, get_neo4j_user, get_neo4j_password
        assert callable(get_neo4j_uri)
        assert callable(get_neo4j_user)
        assert callable(get_neo4j_password)

    def test_pipeline_can_import_config(self) -> None:
        """writ/retrieval/pipeline.py can import get_hnsw_cache_dir."""
        from writ.config import get_hnsw_cache_dir
        assert callable(get_hnsw_cache_dir)

    def test_conftest_can_import_config(self) -> None:
        """tests/conftest.py can import config accessors instead of hardcoded strings."""
        from writ.config import get_neo4j_uri, get_neo4j_user, get_neo4j_password
        # Verify these return strings (the defaults)
        assert isinstance(get_neo4j_uri(), str)
        assert isinstance(get_neo4j_user(), str)
        assert isinstance(get_neo4j_password(), str)


# ---------------------------------------------------------------------------
# TestMalformedConfigWarns -- Wave 1 Cycle 4 S2 (RED-first)
# ---------------------------------------------------------------------------


class TestMalformedConfigWarns:
    """S2: a present-but-malformed writ.toml warns visibly and still returns {};
    an absent file stays silent.

    load_config's blanket `except Exception: return {}` currently swallows a
    malformed writ.toml with zero signal, so every getter silently falls back to
    a hardcoded default. RED today: no stderr warning is emitted.
    """

    def test_malformed_toml_warns_and_returns_empty(self, tmp_path, capsys) -> None:
        toml_file = tmp_path / "writ.toml"
        toml_file.write_text("this is not [ valid = toml =\n")
        result = load_config(str(toml_file))
        assert result == {}  # fallback preserved
        err = capsys.readouterr().err
        assert "writ.toml" in err or str(toml_file) in err, (
            f"expected a stderr warning naming the file, got: {err!r}"
        )
        assert "malformed" in err.lower(), (
            f"expected the warning to call out the malformed TOML, got: {err!r}"
        )

    def test_absent_file_is_silent(self, tmp_path, capsys) -> None:
        result = load_config(str(tmp_path / "no_such.toml"))
        assert result == {}
        assert capsys.readouterr().err == "", (
            "an absent config file is a normal case (fine), not malformed -- "
            "it must not warn"
        )

    def test_non_utf8_toml_warns_and_returns_empty(self, tmp_path, capsys) -> None:
        """A non-UTF8 / binary writ.toml is unparseable, not unreadable.

        tomllib.load() UTF-8-decodes the file's bytes internally, so a binary or
        wrong-encoding file raises UnicodeDecodeError (a ValueError subclass, NOT
        an OSError, NOT a TOMLDecodeError). Before the widen, that error escaped
        load_config unhandled and crashed get_neo4j_* at daemon startup and every
        DB-touching CLI command. It must be handled like malformed TOML: warn and
        fall back to {}.
        """
        toml_file = tmp_path / "writ.toml"
        toml_file.write_bytes(b"\xff\xfe\x00\x01 not toml")
        result = load_config(str(toml_file))
        assert result == {}  # fallback preserved, no crash
        err = capsys.readouterr().err
        assert "writ.toml" in err or str(toml_file) in err, (
            f"expected a stderr warning naming the file, got: {err!r}"
        )
        assert "malformed" in err.lower(), (
            f"expected the warning to call out the unparseable file, got: {err!r}"
        )


# ---------------------------------------------------------------------------
# TestWarnConfigIgnoredHelper -- Wave 1 Cycle 5 G3 (RED-first)
# ---------------------------------------------------------------------------


class TestWarnConfigIgnoredHelper:
    """G3: load_config's two failure branches (malformed/unparseable via
    TOMLDecodeError/UnicodeDecodeError, and unreadable via OSError) share one
    stderr-warning helper, _warn_config_ignored(config_path, reason), instead of
    duplicating the print boilerplate (DRY-DUP-003).

    RED today: _warn_config_ignored does not exist on writ.config at all.
    """

    def test_warn_config_ignored_exists_and_is_callable(self) -> None:
        from writ.config import _warn_config_ignored

        assert callable(_warn_config_ignored)

    def test_malformed_branch_still_warns_via_shared_helper(self, tmp_path, capsys) -> None:
        """Pin: post-extraction, the malformed-TOML branch must still warn with
        the same observable shape as TestMalformedConfigWarns above (naming the
        file and calling out 'malformed'), just routed through the helper."""
        toml_file = tmp_path / "writ.toml"
        toml_file.write_text("this is not [ valid = toml =\n")
        result = load_config(str(toml_file))
        assert result == {}
        err = capsys.readouterr().err
        assert str(toml_file) in err or "writ.toml" in err, (
            f"expected a stderr warning naming the file, got: {err!r}"
        )
        assert "malformed" in err.lower(), (
            f"expected the warning to call out the malformed TOML, got: {err!r}"
        )

    def test_unreadable_branch_still_warns_via_shared_helper(self, tmp_path, capsys) -> None:
        """Pin: the unreadable-file (OSError, e.g. permission-denied) branch
        must ALSO warn -- via the SAME shared helper as the malformed branch,
        not a second, independently-drifting message. No prior test in this
        file exercises the OSError branch at all."""
        toml_file = tmp_path / "writ.toml"
        toml_file.write_text('[neo4j]\nuri = "bolt://x:7687"\n')
        toml_file.chmod(0o000)
        try:
            result = load_config(str(toml_file))
        finally:
            toml_file.chmod(0o644)  # restore so tmp_path cleanup can remove it
        assert result == {}  # fallback preserved
        err = capsys.readouterr().err
        assert err != "", "the unreadable-file branch must warn, not silently fall back"
        assert str(toml_file) in err or "writ.toml" in err, (
            f"expected a stderr warning naming the file, got: {err!r}"
        )
