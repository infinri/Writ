"""Session state must survive a reboot.

RED PHASE: `_cache_dir()` currently falls back to `tempfile.gettempdir()`, so
every location assertion below fails until the default moves under the skill
install. The override and round-trip tests pass both before and after -- that is
deliberate: they pin the behavior the move must NOT break.

Root cause this file exists to prevent (2026-07-23): session caches lived in
/tmp, and `/usr/lib/tmpfiles.d/tmp.conf` declares `D /tmp`, which means systemd
EMPTIES the directory at boot. After a reboot, resuming a conversation silently
lost mode, gates_approved, and loaded_rule_ids -- presenting as the "mode=None"
wipe. Measured at the time: 341 writ-session-*.json files, every one postdating
the boot, zero predating it.

The failure was invisible because a MISSING cache is not an error anywhere:
`_read_cache` returns `_default_cache()` before its try block, so no exception is
logged, and nothing emits a mode_change because nothing changed the mode.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from writ.session import cache as cache_mod

SKILL_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def no_override(monkeypatch):
    """Exercise the shipped DEFAULT, not the test harness's override.

    conftest sets WRIT_CACHE_DIR for isolation, which would mask the very thing
    under test, so it is removed for these assertions.
    """
    monkeypatch.delenv("WRIT_CACHE_DIR", raising=False)


# --- the default must be durable --------------------------------------------


def test_default_cache_dir_is_not_the_system_temp_dir(no_override):
    """The bug in one line: the default was a directory the OS empties on boot."""
    assert Path(cache_mod._cache_dir()).resolve() != Path(tempfile.gettempdir()).resolve()


def test_default_cache_dir_is_not_inside_the_system_temp_dir(no_override):
    """Not merely a different name -- not under /tmp at all, since the boot-time
    sweep empties the whole tree, not just the top level."""
    resolved = Path(cache_mod._cache_dir()).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    assert tmp not in resolved.parents and resolved != tmp


def test_default_cache_dir_is_not_under_the_install(no_override):
    """State must survive an upgrade: a plugin install path carries the version."""
    resolved = Path(cache_mod._cache_dir()).resolve()
    assert SKILL_ROOT.resolve() not in resolved.parents and resolved != SKILL_ROOT.resolve()


def test_default_cache_dir_is_the_state_root_session_dir(no_override):
    from writ.shared.state_root import state_root
    assert cache_mod._cache_dir() == os.path.join(state_root(), "session")


# --- the override must keep working -----------------------------------------
# These pass before AND after: they pin what the move must not break (the whole
# test suite and conftest rely on this override for isolation).


def test_writ_cache_dir_override_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    assert cache_mod._cache_dir() == str(tmp_path)


def test_override_is_read_at_call_time_not_frozen_at_import(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "first"))
    assert cache_mod._cache_dir() == str(tmp_path / "first")
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path / "second"))
    assert cache_mod._cache_dir() == str(tmp_path / "second")


# --- derived paths must follow ----------------------------------------------


def test_cache_path_follows_the_resolved_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    assert Path(cache_mod._cache_path("sid-1")).parent == tmp_path


def test_lock_path_follows_the_resolved_dir(tmp_path, monkeypatch):
    """The lock must land beside the cache; a split would silently stop
    serializing concurrent writers."""
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    assert Path(cache_mod._lock_path("sid-2")).parent == tmp_path


def test_default_cache_path_follows_the_default_dir(no_override):
    assert Path(cache_mod._cache_path("sid-3")).parent == Path(cache_mod._cache_dir())


def test_default_lock_path_follows_the_default_dir(no_override):
    assert Path(cache_mod._lock_path("sid-4")).parent == Path(cache_mod._cache_dir())


# --- round trip --------------------------------------------------------------


def test_written_cache_round_trips_the_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
    with cache_mod.mutate_cache("sid-rt") as c:
        c["mode"] = "work"
        c["current_phase"] = "planning"
    assert cache_mod._read_cache("sid-rt")["mode"] == "work"


def test_cache_dir_is_created_if_absent(tmp_path, monkeypatch):
    """Unlike /tmp, the new default may not exist on a fresh install; writing
    must create it rather than fail into a blank session."""
    target = tmp_path / "not-yet-there" / "session"
    monkeypatch.setenv("WRIT_CACHE_DIR", str(target))
    with cache_mod.mutate_cache("sid-mk") as c:
        c["mode"] = "investigate"
    assert cache_mod._read_cache("sid-mk")["mode"] == "investigate"


# --- deployment must not reintroduce the volatile pin -----------------------


def test_installer_does_not_write_a_tmp_cache_pin():
    """The unit is generated inline by the installer (there is no standalone
    writ-server.service file), so the installer IS the deployment contract.
    Fixing the code default alone would leave the daemon pinned to /tmp."""
    installer = (SKILL_ROOT / "scripts" / "install-server-service.sh").read_text()
    pins = [
        line for line in installer.splitlines()
        if "WRIT_CACHE_DIR=/tmp" in line and not line.lstrip().startswith("#")
    ]
    assert not pins, f"a fresh install would recreate the bug: {pins}"


def test_installed_unit_if_present_does_not_pin_cache_dir_to_tmp():
    """The unit already on THIS machine still carries the old pin until the
    installer is re-run, so this is a deployment reminder, not a code assertion."""
    unit = Path.home() / ".config/systemd/user/writ-server.service"
    if not unit.is_file():
        pytest.skip("no installed unit on this machine")
    pins = [
        line for line in unit.read_text().splitlines()
        if "WRIT_CACHE_DIR=/tmp" in line and not line.lstrip().startswith("#")
    ]
    assert not pins, (
        f"installed unit still pins the daemon to a boot-wiped dir: {pins}. "
        f"Re-run scripts/install-server-service.sh, then restart the daemon."
    )


def test_var_session_is_gitignored():
    ignored = (SKILL_ROOT / ".gitignore").read_text()
    assert any(
        line.strip() in ("var/", "var", "var/session/", "var/session")
        for line in ignored.splitlines()
    ), "runtime session state must not be tracked"
