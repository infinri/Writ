"""Legacy install-relative session caches carry into the state root: once, never clobbering."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MIGRATE = REPO / "bin" / "lib" / "writ_state_migrate.py"
BOOTSTRAP = REPO / "hooks" / "scripts" / "session-start-bootstrap.sh"


def _mod():
    spec = importlib.util.spec_from_file_location("writ_state_migrate", MIGRATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed(d: Path, sid: str, mode: str, mtime: float | None = None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"writ-session-{sid}.json"
    p.write_text(json.dumps({"mode": mode}))
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _mode(p: Path) -> str:
    return json.loads(p.read_text())["mode"]


@pytest.fixture()
def layout(tmp_path):
    home = tmp_path / "home"
    skill = tmp_path / "ws" / "Writ"
    dest = tmp_path / "state" / "writ" / "session"
    home.mkdir()
    skill.mkdir(parents=True)
    return home, skill, dest


def _carry(home, skill, dest) -> int:
    return _mod().carry_legacy_sessions(str(skill), str(dest), str(home))


class TestCarry:
    def test_a_legacy_cache_is_carried(self, layout):
        home, skill, dest = layout
        _seed(skill / "var" / "session", "a1", "work")
        assert _carry(home, skill, dest) == 1
        assert _mode(dest / "writ-session-a1.json") == "work"

    def test_an_existing_destination_is_never_overwritten(self, layout):
        home, skill, dest = layout
        _seed(skill / "var" / "session", "a1", "work", mtime=2_000_000_000)
        _seed(dest, "a1", "conversation", mtime=1_000_000_000)
        assert _carry(home, skill, dest) == 0
        assert _mode(dest / "writ-session-a1.json") == "conversation"

    def test_the_newest_legacy_copy_wins(self, layout):
        home, skill, dest = layout
        _seed(skill / "var" / "session", "a1", "investigate", mtime=1_000_000_000)
        _seed(home / ".claude" / "skills" / "writ" / "var" / "session", "a1", "work", mtime=2_000_000_000)
        assert _carry(home, skill, dest) == 1
        assert _mode(dest / "writ-session-a1.json") == "work"

    def test_the_mtime_is_preserved(self, layout):
        home, skill, dest = layout
        _seed(skill / "var" / "session", "a1", "work", mtime=1_500_000_000)
        _carry(home, skill, dest)
        assert int((dest / "writ-session-a1.json").stat().st_mtime) == 1_500_000_000

    def test_plugin_version_siblings_are_scanned(self, tmp_path):
        home = tmp_path / "home"
        base = home / ".claude" / "plugins" / "cache" / "writ" / "writ"
        skill = base / "1.9.0"
        skill.mkdir(parents=True)
        _seed(base / "1.8.0" / "var" / "session", "old", "work")
        dest = tmp_path / "state" / "writ" / "session"
        assert _carry(home, skill, dest) == 1
        assert _mode(dest / "writ-session-old.json") == "work"

    def test_siblings_of_a_non_plugin_checkout_are_not_scanned(self, layout):
        home, skill, dest = layout
        _seed(skill.parent / "magento" / "var" / "session", "x", "work")
        assert _carry(home, skill, dest) == 0
        assert not dest.exists()

    def test_the_marketplace_clone_location_is_scanned(self, layout):
        home, skill, dest = layout
        _seed(home / ".claude" / "plugins" / "marketplaces" / "writ" / "var" / "session", "m1", "debug")
        assert _carry(home, skill, dest) == 1

    def test_only_session_caches_are_carried(self, layout):
        home, skill, dest = layout
        legacy = skill / "var" / "session"
        legacy.mkdir(parents=True)
        for name in ("writ-grant-a1.json", "writ-events-a1.buf", "sess_abc", "writ-session-a1.json.x.tmp"):
            (legacy / name).write_text("{}")
        assert _carry(home, skill, dest) == 0

    def test_a_symlink_is_not_followed(self, layout, tmp_path):
        home, skill, dest = layout
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"mode": "work"}))
        legacy = skill / "var" / "session"
        legacy.mkdir(parents=True)
        (legacy / "writ-session-ln.json").symlink_to(outside)
        assert _carry(home, skill, dest) == 0

    def test_a_second_run_carries_nothing(self, layout):
        home, skill, dest = layout
        _seed(skill / "var" / "session", "a1", "work")
        assert _carry(home, skill, dest) == 1
        assert _carry(home, skill, dest) == 0
        assert sorted(p.name for p in dest.iterdir()) == ["writ-session-a1.json"]


class TestEntryPoint:
    def test_an_explicit_writ_cache_dir_disables_migration(self, tmp_path):
        env = {**os.environ, "WRIT_CACHE_DIR": str(tmp_path / "pinned"),
               "XDG_STATE_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "home")}
        r = subprocess.run([sys.executable, str(MIGRATE)], env=env,
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert not (tmp_path / "xdg").exists() and not (tmp_path / "pinned").exists()

    def test_the_migration_runs_before_the_plugin_root_exit(self):
        text = BOOTSTRAP.read_text()
        assert text.index("writ_state_migrate.py") < text.index('if [ -z "${CLAUDE_PLUGIN_ROOT:-}" ]; then')

    def test_session_start_carries_a_legacy_cache_with_no_plugin_root(self, tmp_path):
        """A FAKE skill tree holding exactly what the hook and the script need, so no real
        legacy directory is ever read."""
        skill = tmp_path / "skill"
        for rel in ("hooks/scripts/session-start-bootstrap.sh", "bin/lib/writ_state_migrate.py",
                    "writ/__init__.py", "writ/shared/__init__.py", "writ/shared/state_root.py"):
            (skill / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, skill / rel)
        _seed(skill / "var" / "session", "boot1", "work")
        env = {k: v for k, v in os.environ.items() if k not in ("WRIT_CACHE_DIR", "CLAUDE_PLUGIN_ROOT")}
        env.update({"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "xdg")})
        r = subprocess.run(["bash", str(skill / "hooks" / "scripts" / "session-start-bootstrap.sh")],
                           input="{}", env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert _mode(tmp_path / "xdg" / "writ" / "session" / "writ-session-boot1.json") == "work"
