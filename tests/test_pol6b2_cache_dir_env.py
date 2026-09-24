"""POL-6b-2: cache-dir resolved at call time from WRIT_CACHE_DIR (no module global, no proxy).

The cache functions drop the cache_dir parameter and resolve _cache_dir() (the
WRIT_CACHE_DIR env, defaulting to the tempdir) on each call, so submodules call
cache._read_cache(session_id) with no facade dependency and tests override via
monkeypatch.setenv. cache.CACHE_DIR remains an import-time snapshot for the server health /
desync display only.

Per TEST-TDD-001: skeletons approved before implementation. RED until the rework lands.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
import os
import sys

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
FACADE_PATH = os.path.join(SKILL_ROOT, "bin", "lib", "writ-session.py")
CACHE_PATH = os.path.join(SKILL_ROOT, "writ", "session", "cache.py")


def _load_facade():
    spec = importlib.util.spec_from_file_location("writ_session_pol6b2", FACADE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_cache_module():
    if SKILL_ROOT not in sys.path:
        sys.path.insert(0, SKILL_ROOT)
    return importlib.import_module("writ.session.cache")


class TestCallTimeSignatures:
    """The I/O functions take only the session id (+data) -- the dir is resolved internally."""

    def test_read_cache_takes_only_session_id(self):
        cache = _load_cache_module()
        params = list(inspect.signature(cache._read_cache).parameters)
        assert params == ["session_id"], params

    def test_write_cache_takes_session_id_and_data(self):
        cache = _load_cache_module()
        params = list(inspect.signature(cache._write_cache).parameters)
        assert params == ["session_id", "data"], params

    def test_cache_path_takes_only_session_id(self):
        cache = _load_cache_module()
        params = list(inspect.signature(cache._cache_path).parameters)
        assert params == ["session_id"], params

    def test_cache_dir_resolver_exists(self):
        cache = _load_cache_module()
        assert callable(cache._cache_dir)


class TestEnvHonored:
    """WRIT_CACHE_DIR is resolved at call time by the cache module and the facade re-exports."""

    def test_cache_module_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        cache = _load_cache_module()
        assert cache._cache_path("envsid") == os.path.join(str(tmp_path), "writ-session-envsid.json")
        d = cache._read_cache("rt")
        d["mode"] = "work"
        cache._write_cache("rt", d)
        assert (tmp_path / "writ-session-rt.json").is_file()
        assert cache._read_cache("rt")["mode"] == "work"

    def test_facade_reexport_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        facade = _load_facade()
        d = facade._read_cache("fsid")
        d["mode"] = "debug"
        facade._write_cache("fsid", d)
        assert (tmp_path / "writ-session-fsid.json").is_file(), (
            "facade._write_cache must resolve WRIT_CACHE_DIR at call time"
        )
        assert facade._read_cache("fsid")["mode"] == "debug"

    def test_default_is_durable_skill_var_when_unset(self, monkeypatch, tmp_path):
        """REVERSED 2026-07-23. This asserted the default WAS tempfile.gettempdir(),
        which encoded the mode=None wipe: `/usr/lib/tmpfiles.d/tmp.conf` declares
        `D /tmp`, so systemd empties it at boot and every session cache died on
        reboot, silently blanking mode/gates/loaded_rule_ids on resume. The default
        is now the XDG state root's session/ and must NOT track tempfile.gettempdir().
        See tests/test_session_cache_durability.py for the full contract.
        """
        from writ.shared.state_root import state_root
        monkeypatch.delenv("WRIT_CACHE_DIR", raising=False)
        monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
        cache = _load_cache_module()
        path = cache._cache_path("d")
        assert not path.startswith(str(tmp_path))
        assert path.startswith(os.path.join(state_root(), "session") + os.sep)


class TestSnapshotForServer:
    """CACHE_DIR stays a string attribute for writ/server/routes/query.py:390 health / desync display."""

    def test_cache_module_exposes_snapshot(self):
        cache = _load_cache_module()
        assert isinstance(cache.CACHE_DIR, str)

    def test_facade_exposes_snapshot(self):
        facade = _load_facade()
        assert isinstance(facade.CACHE_DIR, str)


class TestSourceShape:
    def test_cache_defines_env_resolver(self):
        with open(CACHE_PATH) as f:
            src = f.read()
        assert "def _cache_dir(" in src
        assert "WRIT_CACHE_DIR" in src

    def test_facade_has_no_injecting_wrapper(self):
        with open(FACADE_PATH) as f:
            src = f.read()
        # the Approach-D wrapper signature must be gone
        assert "_read_cache(session_id, CACHE_DIR)" not in src
        assert 'CACHE_DIR = os.environ.get("WRIT_CACHE_DIR"' not in src, (
            "the facade must not keep its own CACHE_DIR env global"
        )
        assert "from writ.session.cache import" in src

    def test_no_attribute_setter_left_in_migrated_tests(self):
        """None of the migrated suites assign <mod>.CACHE_DIR anymore."""
        import glob
        import re
        offenders = []
        pat = re.compile(r"(?:self\.mod|writ_session|\bmod)\.CACHE_DIR\s*=")
        for path in glob.glob(os.path.join(SKILL_ROOT, "tests", "*.py")):
            with open(path) as f:
                if pat.search(f.read()):
                    offenders.append(os.path.basename(path))
        assert offenders == [], f"attribute CACHE_DIR setters remain: {offenders}"
