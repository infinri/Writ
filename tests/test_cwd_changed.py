"""Tests for the CwdChanged hook and detected_domain session cache field (Cycle C, Item 10).

Per TEST-TDD-001: skeletons approved before implementation.
Covers: detected_domain default in _read_cache, domain detection from marker
files, session cache update, friction event logging, exit 0 contract, and
settings.json registration.

W2 (server package split, branch refactor/w2-server-split): TestRagInjectDomainPassthrough
reads via writ_server_source() (tests/conftest.py), which is layout-agnostic -- it scans
every *.py under writ/server/ if that directory exists (post-split: this content is
expected in routes/query.py), else the single writ/server.py file (pre-split).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import writ_server_source

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

SESSION_ID = "test-cwd-session"
SKILL_DIR = str(Path(__file__).resolve().parent.parent)
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"
HOOK_PATH = f"{SKILL_DIR}/hooks/scripts/writ-cwd-changed.sh"
SETTINGS_PATH = str(Path(__file__).resolve().parent.parent / "hooks" / "hooks.json")

MARKER_TO_DOMAIN: dict[str, str] = {
    "composer.json": "php",
    "pyproject.toml": "python",
    "package.json": "javascript",
    "Cargo.toml": "rust",
    "go.mod": "go",
}


def _load_writ_session():
    """Load writ-session.py as a module without installing it."""
    spec = importlib.util.spec_from_file_location("writ_session_cwd", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cache(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": SESSION_ID,
        "mode": "Work",
        "current_phase": "implementation",
        "remaining_budget": 5000,
        "context_percent": 30,
        "loaded_rule_ids": [],
        "loaded_rules": [],
        "loaded_rule_ids_by_phase": {},
        "queries": 0,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
        "is_orchestrator": False,
    }
    base.update(overrides)
    return base


def _make_cwd_envelope(cwd: str) -> str:
    """Produce a JSON envelope simulating CwdChanged hook stdin."""
    return json.dumps({"cwd": cwd, "session_id": SESSION_ID})


def _run_hook(cwd_dir: str, cache_dir: str, session_id: str = SESSION_ID) -> subprocess.CompletedProcess:
    """Run the CwdChanged hook with a simulated envelope."""
    envelope = json.dumps({"cwd": cwd_dir, "session_id": session_id})
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = cache_dir
    env["WRIT_PORT"] = "19999"  # unreachable port to force subprocess fallback
    return subprocess.run(
        ["bash", HOOK_PATH],
        input=envelope,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# TestDetectedDomainDefault -- session cache schema
# ---------------------------------------------------------------------------


class TestDetectedDomainDefault:
    """detected_domain must be present with a null default in _read_cache."""

    def setup_method(self) -> None:
        self.mod = _load_writ_session()
        self._tmpdir = tempfile.mkdtemp()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._tmpdir})
        self._env_patch.start()

    def teardown_method(self) -> None:
        self._env_patch.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fresh_cache_has_detected_domain_key(self) -> None:
        """_read_cache for a new session returns a dict that includes detected_domain."""
        cache = self.mod._read_cache("fresh-session")
        assert "detected_domain" in cache

    def test_detected_domain_default_is_null(self) -> None:
        """detected_domain defaults to None / null, not 'universal' or ''."""
        cache = self.mod._read_cache("fresh-session")
        assert cache["detected_domain"] is None

    def test_existing_cache_without_field_gets_detected_domain_setdefault(self) -> None:
        """_read_cache on a legacy cache file without detected_domain returns detected_domain: None."""
        legacy_cache = {"loaded_rule_ids": [], "remaining_budget": 8000}
        path = self.mod._cache_path("legacy-session")
        with open(path, "w") as f:
            json.dump(legacy_cache, f)
        cache = self.mod._read_cache("legacy-session")
        assert cache["detected_domain"] is None


# ---------------------------------------------------------------------------
# TestCwdChangedDomainDetection -- hook marker-file logic
# ---------------------------------------------------------------------------


class TestCwdChangedDomainDetection:
    """writ-cwd-changed.sh detects the correct domain from marker files."""

    def setup_method(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._cache_tmpdir = tempfile.mkdtemp()
        self.mod = _load_writ_session()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._cache_tmpdir})
        self._env_patch.start()
        # Pre-create a cache file so the hook can update it
        cache = _make_cache()
        path = self.mod._cache_path(SESSION_ID)
        with open(path, "w") as f:
            json.dump(cache, f)

    def teardown_method(self) -> None:
        self._env_patch.stop()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        shutil.rmtree(self._cache_tmpdir, ignore_errors=True)

    def _write_marker(self, filename: str) -> str:
        """Create a marker file in the temp dir; return the dir path."""
        marker_path = os.path.join(self._tmpdir, filename)
        Path(marker_path).touch()
        return self._tmpdir

    def _run_and_get_domain(self, cwd_dir: str) -> str:
        """Run the hook and read detected_domain from cache."""
        result = _run_hook(cwd_dir, self._cache_tmpdir)
        assert result.returncode == 0
        # Read cache directly since we set WRIT_CACHE_DIR
        path = os.path.join(self._cache_tmpdir, f"writ-session-{SESSION_ID}.json")
        with open(path) as f:
            cache = json.load(f)
        return cache.get("detected_domain", "")

    def test_composer_json_detected_as_php_domain(self) -> None:
        """Directory containing composer.json -> detected_domain = 'php'."""
        cwd = self._write_marker("composer.json")
        assert self._run_and_get_domain(cwd) == "php"

    def test_pyproject_toml_detected_as_python_domain(self) -> None:
        """Directory containing pyproject.toml -> detected_domain = 'python'."""
        cwd = self._write_marker("pyproject.toml")
        assert self._run_and_get_domain(cwd) == "python"

    def test_package_json_detected_as_javascript_domain(self) -> None:
        """Directory containing package.json -> detected_domain = 'javascript'."""
        cwd = self._write_marker("package.json")
        assert self._run_and_get_domain(cwd) == "javascript"

    def test_cargo_toml_detected_as_rust_domain(self) -> None:
        """Directory containing Cargo.toml -> detected_domain = 'rust'."""
        cwd = self._write_marker("Cargo.toml")
        assert self._run_and_get_domain(cwd) == "rust"

    def test_go_mod_detected_as_go_domain(self) -> None:
        """Directory containing go.mod -> detected_domain = 'go'."""
        cwd = self._write_marker("go.mod")
        assert self._run_and_get_domain(cwd) == "go"

    def test_no_marker_files_detected_as_universal(self) -> None:
        """Directory with no recognizable marker files -> detected_domain = 'universal'."""
        empty_dir = tempfile.mkdtemp()
        try:
            assert self._run_and_get_domain(empty_dir) == "universal"
        finally:
            import shutil
            shutil.rmtree(empty_dir, ignore_errors=True)

    def test_multiple_markers_picks_first_match_in_priority_order(self) -> None:
        """Directory with both composer.json and package.json picks the primary marker."""
        self._write_marker("composer.json")
        self._write_marker("package.json")
        assert self._run_and_get_domain(self._tmpdir) == "php"

    def test_detection_is_based_on_file_existence_not_content(self) -> None:
        """Empty marker file (zero bytes) is still recognized as a valid domain signal."""
        cwd = self._write_marker("pyproject.toml")
        # File was created empty by touch -- verify it's 0 bytes
        marker_path = os.path.join(cwd, "pyproject.toml")
        assert os.path.getsize(marker_path) == 0
        assert self._run_and_get_domain(cwd) == "python"


# ---------------------------------------------------------------------------
# TestCwdChangedSessionCacheUpdate -- hook writes detected_domain to cache
# ---------------------------------------------------------------------------


class TestCwdChangedSessionCacheUpdate:
    """writ-cwd-changed.sh updates detected_domain in the session cache."""

    def setup_method(self) -> None:
        self._cwd_tmpdir = tempfile.mkdtemp()
        self._cache_tmpdir = tempfile.mkdtemp()
        self.mod = _load_writ_session()
        self._env_patch = mock.patch.dict(os.environ, {"WRIT_CACHE_DIR": self._cache_tmpdir})
        self._env_patch.start()
        # Pre-create a cache file
        cache = _make_cache()
        path = self.mod._cache_path(SESSION_ID)
        with open(path, "w") as f:
            json.dump(cache, f)

    def teardown_method(self) -> None:
        self._env_patch.stop()
        import shutil
        shutil.rmtree(self._cwd_tmpdir, ignore_errors=True)
        shutil.rmtree(self._cache_tmpdir, ignore_errors=True)

    def _read_domain(self) -> Any:
        path = os.path.join(self._cache_tmpdir, f"writ-session-{SESSION_ID}.json")
        with open(path) as f:
            return json.load(f).get("detected_domain")

    def test_hook_writes_detected_domain_to_session_cache(self) -> None:
        """After the hook runs against a python project dir, cache detected_domain == 'python'."""
        Path(os.path.join(self._cwd_tmpdir, "pyproject.toml")).touch()
        result = _run_hook(self._cwd_tmpdir, self._cache_tmpdir)
        assert result.returncode == 0
        assert self._read_domain() == "python"

    def test_hook_overwrites_previous_detected_domain(self) -> None:
        """Running the hook a second time with a different cwd replaces the stored domain."""
        Path(os.path.join(self._cwd_tmpdir, "pyproject.toml")).touch()
        _run_hook(self._cwd_tmpdir, self._cache_tmpdir)
        assert self._read_domain() == "python"

        # Second run with a different dir
        second_dir = tempfile.mkdtemp()
        try:
            Path(os.path.join(second_dir, "Cargo.toml")).touch()
            _run_hook(second_dir, self._cache_tmpdir)
            assert self._read_domain() == "rust"
        finally:
            import shutil
            shutil.rmtree(second_dir, ignore_errors=True)

    def test_hook_stores_universal_when_no_markers_present(self) -> None:
        """Cache gets detected_domain='universal' when the new cwd has no marker files."""
        empty_dir = tempfile.mkdtemp()
        try:
            _run_hook(empty_dir, self._cache_tmpdir)
            assert self._read_domain() == "universal"
        finally:
            import shutil
            shutil.rmtree(empty_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# TestCwdChangedHookContract -- exit code, timer, friction log
# ---------------------------------------------------------------------------


class TestCwdChangedHookContract:
    """writ-cwd-changed.sh must always exit 0, use timers, and log events."""

    def test_hook_file_exists(self) -> None:
        """writ-cwd-changed.sh exists on disk at the expected path."""
        assert os.path.exists(HOOK_PATH), f"Hook not found at {HOOK_PATH}"

    def test_hook_sources_common_sh(self) -> None:
        """writ-cwd-changed.sh sources common.sh for shared utilities."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "common.sh" in source, "Hook must source common.sh"

    def test_hook_always_exits_0(self) -> None:
        """Running the hook with a valid stdin envelope exits with code 0."""
        cache_dir = tempfile.mkdtemp()
        cwd_dir = tempfile.mkdtemp()
        try:
            # Pre-create cache
            cache_path = os.path.join(cache_dir, f"writ-session-{SESSION_ID}.json")
            with open(cache_path, "w") as f:
                json.dump(_make_cache(), f)
            result = _run_hook(cwd_dir, cache_dir)
            assert result.returncode == 0
        finally:
            import shutil
            shutil.rmtree(cache_dir, ignore_errors=True)
            shutil.rmtree(cwd_dir, ignore_errors=True)

    def test_hook_is_positioned_to_be_instrumented(self) -> None:
        """WHAT THIS USED TO ASSERT, and why it changed: two cases required the literal
        strings `hook_timer_start` and `hook_timer_end` in the source. Those calls are gone,
        because common.sh now installs the telemetry trap for every script under
        hooks/scripts/ that sources it, and keeping the explicit call as well made this hook
        emit its row twice. The property worth pinning is the one the trap depends on.
        Whether a row actually appears is proven by running the hook, in
        tests/test_hook_telemetry_coverage.py.
        """
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "common.sh" in source, (
            "the hook must source common.sh, which installs its telemetry trap"
        )
        assert "/hooks/scripts/" in str(HOOK_PATH), (
            "the hook must live under hooks/scripts/, which is what the trap keys on"
        )

    def test_hook_logs_cwd_changed_friction_event(self) -> None:
        """Hook calls log_friction_event with event name 'cwd_changed'."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "cwd_changed" in source, "Hook must log cwd_changed friction event"

    def test_hook_reads_cwd_from_stdin_envelope(self) -> None:
        """Hook source references the cwd field from the JSON stdin envelope."""
        with open(HOOK_PATH) as f:
            source = f.read()
        # The hook must parse stdin JSON to get the new working directory.
        assert "cwd" in source, "Hook must extract cwd from stdin envelope"


# ---------------------------------------------------------------------------
# TestCwdChangedSettingsJson -- settings.json registration
# ---------------------------------------------------------------------------


class TestCwdChangedSettingsJson:
    """writ-cwd-changed.sh is registered in settings.json."""

    def _load_settings(self) -> dict:
        with open(SETTINGS_PATH) as f:
            return json.load(f)

    def test_cwd_changed_event_registered_in_settings(self) -> None:
        """settings.json has CwdChanged event entry pointing to writ-cwd-changed.sh."""
        settings = self._load_settings()
        hooks = settings.get("hooks", {})
        cwd_hooks = hooks.get("CwdChanged", [])
        hook_commands = " ".join(
            h.get("command", "") if isinstance(h, dict) else str(h)
            for entry in cwd_hooks
            for h in (entry.get("hooks", []) if isinstance(entry, dict) and "hooks" in entry else [entry])
        )
        assert "writ-cwd-changed.sh" in hook_commands, (
            "CwdChanged event must register writ-cwd-changed.sh"
        )
        # NOTE: under plugin-canonical, hooks are dispatched by the plugin manifest and do NOT
        # need a Bash-allowlist entry in ~/.claude/settings.json. The standalone-era
        # "hook Bash permission present" assertion was removed with the standalone sunset (7a9f4ba).


# ---------------------------------------------------------------------------
# TestRagInjectDomainPassthrough -- writ-rag-inject.sh uses detected_domain
# ---------------------------------------------------------------------------


class TestRagInjectDomainPassthrough:
    """writ-rag-inject.sh reads detected_domain from cache and passes it as domain."""

    def test_rag_inject_includes_detected_domain_in_query_request(self) -> None:
        """#8: the /prompt-bundle endpoint reads detected_domain from the cache and
        passes it as 'domain' to /query when non-null and not 'universal'."""
        source = writ_server_source()
        assert "detected_domain" in source, (
            "the /prompt-bundle endpoint must read detected_domain from the session cache"
        )

    def test_rag_inject_omits_domain_when_detected_domain_is_universal(self) -> None:
        """#8: the endpoint does not pass domain=universal; 'universal' is treated as no hint."""
        source = writ_server_source()
        # Must skip adding domain when the value is 'universal' or null.
        assert "universal" in source, (
            "the /prompt-bundle endpoint must handle the 'universal' domain as a no-op"
        )


# ---------------------------------------------------------------------------
# TestCwdChangedAutoInstallGuard -- seam guards on post-commit, not the retired hook
# ---------------------------------------------------------------------------


class TestCwdChangedAutoInstallGuard:
    """The auto-install seam must guard on the installed post-commit hook,
    not the retired prepare-commit-msg hook (Phase 3c, ADR-3c-2)."""

    def _source(self) -> str:
        with open(HOOK_PATH) as f:
            return f.read()

    def test_guard_references_post_commit_hook(self) -> None:
        """The seam builds and greps the post-commit hook path."""
        source = self._source()
        assert "hooks/post-commit" in source, (
            "auto-install guard must target the installed post-commit hook"
        )

    def test_guard_does_not_reference_retired_prepare_commit_msg(self) -> None:
        """The retired prepare-commit-msg hook is never the guard target."""
        source = self._source()
        assert "prepare-commit-msg" not in source, (
            "auto-install guard must not reference the retired prepare-commit-msg hook"
        )

    def test_guard_still_greps_writ_marker(self) -> None:
        """The marker predicate (# >>> Writ) is unchanged."""
        source = self._source()
        assert "# >>> Writ" in source, "guard must still grep the Writ marker"
