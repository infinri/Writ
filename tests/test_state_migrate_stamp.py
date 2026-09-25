"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4 (5c): run
bin/lib/writ_state_migrate.py only when a version stamp says it has not run yet for the
installed version.

RED at HEAD: `hooks/scripts/session-start-bootstrap.sh` step 0b runs
`python3 bin/lib/writ_state_migrate.py` UNCONDITIONALLY on every SessionStart
(session-start-bootstrap.sh:23-26); there is no `.claude-plugin/plugin.json` version
read, no stamp comparison, and `bin/lib/writ_state_migrate.py` accepts no
`--stamp-file`/`--stamp-key` and has no `carry_legacy_sessions_report`. Every
"execs 0 times" assertion below is therefore RED (today it always execs).

Fake skill trees only (mirrors tests/test_state_migration.py's own precedent): each
tree carries exactly the files the hook and the script need, copied from this repo, so
no test reads a real legacy session directory. `CLAUDE_PLUGIN_ROOT` is left unset, so
the bootstrap hook exits right after step 0b (before probing a venv or Neo4j) --
exactly what test_state_migration.py::TestEntryPoint already relies on.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP_REL = "hooks/scripts/session-start-bootstrap.sh"
MIGRATE_REL = "bin/lib/writ_state_migrate.py"

_CALL_MARKER = "@@WRIT_MIGRATE_EXEC@@"


def _shim_dir(tmp_path: Path) -> Path:
    shim = tmp_path / "path-shim"
    shim.mkdir()
    real = shutil.which("python3")
    assert real, "no python3 on PATH to build the exec-count shim from"
    wrapper = shim / "python3"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "{_CALL_MARKER}\\n%s\\n" "$*" >> "${{WRIT_MIGRATE_COUNTER:?}}"\n'
        f'exec "{real}" "$@"\n'
    )
    wrapper.chmod(0o755)
    return shim


def _migrate_exec_count(counter: Path) -> int:
    """Count only the python3 invocations whose argv names writ_state_migrate.py
    (the bootstrap script's step 0 JSON parse and step 5 carry-forward-mode also
    exec python3, and must not be counted here)."""
    lines = counter.read_text().splitlines()
    count = 0
    i = 0
    while i < len(lines):
        if lines[i] == _CALL_MARKER:
            argv = lines[i + 1] if i + 1 < len(lines) else ""
            if "writ_state_migrate.py" in argv:
                count += 1
            i += 2
        else:
            i += 1
    return count


def _build_skill_tree(tmp_path: Path, *, plugin_version: str | None = "7.7.7",
                      with_state_root: bool = True) -> Path:
    skill = tmp_path / "skill"
    rels = [BOOTSTRAP_REL, MIGRATE_REL, "writ/__init__.py", "writ/shared/__init__.py"]
    if with_state_root:
        rels.append("writ/shared/state_root.py")
    for rel in rels:
        (skill / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, skill / rel)
    if plugin_version is not None:
        plugin_dir = skill / ".claude-plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / "plugin.json").write_text(json.dumps({
            "name": "writ", "version": plugin_version,
        }))
    return skill


def _seed_legacy_cache(skill: Path, sid: str, mode: str = "work") -> None:
    d = skill / "var" / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"writ-session-{sid}.json").write_text(json.dumps({"mode": mode}))


def _run_bootstrap(skill: Path, tmp_path: Path, *, shim: Path | None = None,
                   counter: Path | None = None, extra_env: dict | None = None,
                   cache_dir: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("WRIT_CACHE_DIR", "CLAUDE_PLUGIN_ROOT")}
    env.update({"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "xdg")})
    if cache_dir is not None:
        env["WRIT_CACHE_DIR"] = cache_dir
    if shim is not None:
        env["PATH"] = f"{shim}{os.pathsep}{env['PATH']}"
    if counter is not None:
        counter.write_text("")
        env["WRIT_MIGRATE_COUNTER"] = str(counter)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(skill / BOOTSTRAP_REL)], input="{}", env=env,
        capture_output=True, text=True, timeout=30,
    )


def _stamp_path(tmp_path: Path) -> Path:
    return tmp_path / "xdg" / "writ" / "state-migrate.stamp"


class TestFirstRunMigratesAndStamps:
    def test_execs_once_carries_a_legacy_cache_and_stamps(self, tmp_path) -> None:
        skill = _build_skill_tree(tmp_path)
        _seed_legacy_cache(skill, "stamp-first")
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result.returncode == 0, result.stderr
        assert _migrate_exec_count(counter) == 1, counter.read_text()
        carried = json.loads(
            (tmp_path / "xdg" / "writ" / "session" / "writ-session-stamp-first.json").read_text()
        )
        assert carried["mode"] == "work"
        stamp = _stamp_path(tmp_path)
        assert stamp.exists(), "no stamp was written after a successful migration"
        assert stamp.read_text().splitlines()[0] == f"7.7.7 {skill}"


class TestSecondRunWithTheStampSkips:
    def test_execs_zero_times(self, tmp_path) -> None:
        skill = _build_skill_tree(tmp_path)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        first = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert first.returncode == 0, first.stderr
        assert _migrate_exec_count(counter) == 1

        second = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert second.returncode == 0, second.stderr
        assert _migrate_exec_count(counter) == 0, (
            f"the second SessionStart must not exec the migration again: {counter.read_text()}"
        )


class TestVersionChangeStampDeletionAndGarbageAllReRun:
    def _first_run(self, tmp_path) -> tuple[Path, Path, Path]:
        skill = _build_skill_tree(tmp_path)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result.returncode == 0, result.stderr
        assert _migrate_exec_count(counter) == 1
        assert _stamp_path(tmp_path).exists(), (
            "skeleton: no stamp was written by the first run -- the version-stamp "
            "gate does not exist yet, so every later assertion in this class is "
            "unreachable until it does"
        )
        return skill, shim, counter

    def test_a_new_plugin_version_reruns_and_rewrites_the_stamp(self, tmp_path) -> None:
        skill, shim, counter = self._first_run(tmp_path)
        (skill / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "writ", "version": "7.8.0"})
        )
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result.returncode == 0, result.stderr
        assert _migrate_exec_count(counter) == 1
        assert _stamp_path(tmp_path).read_text().splitlines()[0] == f"7.8.0 {skill}"

    def test_deleting_the_stamp_reruns(self, tmp_path) -> None:
        skill, shim, counter = self._first_run(tmp_path)
        _stamp_path(tmp_path).unlink()
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result.returncode == 0, result.stderr
        assert _migrate_exec_count(counter) == 1
        assert _stamp_path(tmp_path).exists()

    def test_garbage_in_the_stamp_reruns(self, tmp_path) -> None:
        skill, shim, counter = self._first_run(tmp_path)
        _stamp_path(tmp_path).write_text("not a version line at all\x00garbage")
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result.returncode == 0, result.stderr
        assert _migrate_exec_count(counter) == 1
        assert _stamp_path(tmp_path).read_text().splitlines()[0] == f"7.7.7 {skill}"


class TestNoPluginJsonMigratesEverySessionStartAndWritesNoStamp:
    def test_execs_every_run_and_never_stamps(self, tmp_path) -> None:
        skill = _build_skill_tree(tmp_path, plugin_version=None)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        for _ in range(2):
            result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
            assert result.returncode == 0, result.stderr
            assert _migrate_exec_count(counter) == 1, counter.read_text()
        assert not _stamp_path(tmp_path).exists()


class TestAFailedMigrationWritesNoStampAndReruns:
    def test_a_missing_state_root_module_leaves_no_stamp(self, tmp_path) -> None:
        skill = _build_skill_tree(tmp_path, with_state_root=False)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result.returncode == 0, result.stderr
        assert _migrate_exec_count(counter) == 1
        assert not _stamp_path(tmp_path).exists(), (
            "a migration that could not import state_root must not stamp completion"
        )
        # No stamp -> the next SessionStart runs it again.
        result2 = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result2.returncode == 0, result2.stderr
        assert _migrate_exec_count(counter) == 1


class TestWritCacheDirSkipsEntirely:
    def test_execs_zero_times_and_writes_no_stamp(self, tmp_path) -> None:
        skill = _build_skill_tree(tmp_path)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        pinned = tmp_path / "pinned-cache"
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter,
                                cache_dir=str(pinned))
        assert result.returncode == 0, result.stderr
        assert _migrate_exec_count(counter) == 0, counter.read_text()
        assert not _stamp_path(tmp_path).exists()


class TestStampPathMatchesStateRoot:
    def test_under_an_absolute_xdg_state_home(self, tmp_path) -> None:
        sys.path.insert(0, str(REPO))
        try:
            from writ.shared.state_root import state_root
        finally:
            sys.path.remove(str(REPO))
        skill = _build_skill_tree(tmp_path)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        result = _run_bootstrap(skill, tmp_path, shim=shim, counter=counter)
        assert result.returncode == 0, result.stderr
        # state_root() reads os.environ directly; call it under the SAME override
        # the subprocess used, via monkeypatched-equivalent os.environ mutation.
        old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(tmp_path / "xdg")
        try:
            expected_root = state_root()
        finally:
            if old is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = old
        expected_stamp = Path(expected_root) / "state-migrate.stamp"
        assert expected_stamp == _stamp_path(tmp_path), (
            "test setup drifted: this file's own _stamp_path helper must name the "
            "same directory state_root() computes for the same XDG_STATE_HOME"
        )
        # The REAL claim: the bootstrap run above actually wrote a stamp THERE.
        assert expected_stamp.exists(), (
            f"expected a stamp at {expected_stamp} (state_root() + state-migrate.stamp) "
            f"after a first, unstamped SessionStart"
        )

    def test_under_a_relative_xdg_state_home_falls_back_to_home_local_state(self, tmp_path) -> None:
        skill = _build_skill_tree(tmp_path)
        shim = _shim_dir(tmp_path)
        counter = tmp_path / "counter"
        env = {k: v for k, v in os.environ.items()
               if k not in ("WRIT_CACHE_DIR", "CLAUDE_PLUGIN_ROOT")}
        env.update({"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": "relative/not/absolute"})
        shim_dir_env = f"{shim}{os.pathsep}{env['PATH']}"
        env["PATH"] = shim_dir_env
        counter.write_text("")
        env["WRIT_MIGRATE_COUNTER"] = str(counter)
        result = subprocess.run(
            ["bash", str(skill / BOOTSTRAP_REL)], input="{}", env=env,
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        expected = tmp_path / "home" / ".local" / "state" / "writ" / "state-migrate.stamp"
        assert expected.exists(), (
            f"a relative XDG_STATE_HOME must fall back to HOME/.local/state/writ: "
            f"{list((tmp_path / 'home').rglob('*'))}"
        )
