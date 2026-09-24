"""Install-location and install-version couplings, removed.

Three couplings, one module: which venv a script uses (bin/lib/writ-venv.sh), which install
the venv's `writ` package comes from (writ_venv_repoint at SessionStart), and which Compose
project owns the Neo4j container (writ_neo4j_start + `name: writ`). Every test drives the real
bash under a tmp HOME with a fake `docker` and fake venv interpreters, so nothing here reaches
the docker daemon or a real venv.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
VENV_LIB = REPO / "bin" / "lib" / "writ-venv.sh"
SERVER_LIB = REPO / "scripts" / "lib" / "writ-server-lib.sh"
SHIM = REPO / "bin" / "writ"
SESSION_START = REPO / "hooks" / "scripts" / "session-start-bootstrap.sh"
ENSURE = REPO / "scripts" / "ensure-server.sh"
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
BOOTSTRAP_PLUGIN = REPO / "scripts" / "bootstrap-plugin.sh"
TEST_GRAPH = REPO / "scripts" / "test-graph.sh"
COMPOSE = REPO / "docker-compose.yml"

VENV_CALLERS = [
    SHIM,
    ENSURE,
    REPO / "scripts" / "stop-server.sh",
    BOOTSTRAP,
    BOOTSTRAP_PLUGIN,
    REPO / "scripts" / "install-server-service.sh",
    SESSION_START,
    REPO / "hooks" / "scripts" / "writ-rag-inject.sh",
]

_CLEARED = ("WRIT_VENV", "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT")


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _fake_venv(venv: Path, serves: str = "") -> Path:
    """A venv whose python3 prints the `writ` dir recorded in <venv>/serves (exit 1 when empty,
    like a failed import), and whose `-m pip install ... -e <root>` logs its argv to
    <venv>/pip.log and records <root>/writ as what it now serves."""
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "serves").write_text(serves)
    _exe(venv / "bin" / "python3", f'''
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    echo "$*" >> "{venv}/pip.log"
    if [ -n "${{FAKE_PIP_FAIL:-}}" ]; then echo "pip: boom" >&2; exit 1; fi
    [ -n "${{FAKE_PIP_SLEEP:-}}" ] && sleep "$FAKE_PIP_SLEEP"
    for a; do last="$a"; done
    (cd "$last" && printf '%s/writ' "$(pwd -P)") > "{venv}/serves"
    exit 0
fi
s="$(cat "{venv}/serves")"
[ -n "$s" ] || exit 1
printf '%s\\n' "$s"
''')
    return venv


def _serves(venv: Path) -> str:
    return (venv / "serves").read_text()


def _env(home: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _CLEARED}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def _bash(script: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)


def _resolve(install: Path, home: Path, **extra: str) -> tuple[int, str]:
    r = _bash(f'source "{VENV_LIB}"; writ_resolve_venv "{install}"; rc=$?; printf "%s\\n%s" "$rc" "${{VENV_DIR:-}}"',
              _env(home, **extra))
    rc, venv = r.stdout.split("\n", 1)
    return int(rc), venv


def _plugin_install(home: Path, market: str = "writ", plugin: str = "writ", version: str = "1.9.0") -> Path:
    root = home / ".claude" / "plugins" / "cache" / market / plugin / version
    (root / "writ").mkdir(parents=True)
    return root


def _data_venv(home: Path, name: str = "writ-writ") -> Path:
    return home / ".claude" / "plugins" / "data" / name / ".venv"


class TestVenvResolution:
    def test_writ_venv_beats_everything(self, tmp_path):
        home, install = tmp_path / "home", tmp_path / "clone"
        _fake_venv(install / ".venv")
        _fake_venv(tmp_path / "data" / ".venv")
        mine = _fake_venv(tmp_path / "mine")
        got = _resolve(install, home, WRIT_VENV=str(mine), CLAUDE_PLUGIN_DATA=str(tmp_path / "data"))
        assert got == (0, str(mine))

    def test_plugin_data_beats_the_install_venv(self, tmp_path):
        home, install = tmp_path / "home", tmp_path / "clone"
        _fake_venv(install / ".venv")
        data = _fake_venv(tmp_path / "data" / ".venv")
        assert _resolve(install, home, CLAUDE_PLUGIN_DATA=str(tmp_path / "data")) == (0, str(data))

    def test_a_clone_uses_its_own_venv(self, tmp_path):
        install = tmp_path / "clone"
        own = _fake_venv(install / ".venv")
        assert _resolve(install, tmp_path / "home") == (0, str(own))

    def test_a_plugin_install_finds_its_data_dir_with_no_loader_env(self, tmp_path):
        """The Bash tool exports neither plugin variable; the path shape is all there is."""
        home = tmp_path / "home"
        install = _plugin_install(home)
        data = _fake_venv(_data_venv(home))
        assert _resolve(install, home) == (0, str(data))

    def test_the_data_dir_id_is_sanitized_like_claude_codes(self, tmp_path):
        home = tmp_path / "home"
        install = _plugin_install(home, market="my.market")
        data = _fake_venv(_data_venv(home, "writ-my-market"))
        assert _resolve(install, home) == (0, str(data))

    def test_a_plugin_install_falls_back_to_the_legacy_cache_venv(self, tmp_path):
        home = tmp_path / "home"
        install = _plugin_install(home)
        legacy = _fake_venv(home / ".cache" / "writ" / ".venv")
        assert _resolve(install, home) == (0, str(legacy))

    def test_the_legacy_cache_venv_never_serves_a_clone(self, tmp_path):
        home, install = tmp_path / "home", tmp_path / "clone"
        install.mkdir()
        _fake_venv(home / ".cache" / "writ" / ".venv")
        assert _resolve(install, home) == (1, str(install / ".venv"))

    def test_a_missing_plugin_venv_names_the_data_dir(self, tmp_path):
        home = tmp_path / "home"
        install = _plugin_install(home)
        assert _resolve(install, home) == (1, str(_data_venv(home)))

    def test_a_path_short_of_a_version_is_not_plugin_shaped(self, tmp_path):
        install = tmp_path / "plugins" / "cache" / "writ" / "writ"
        install.mkdir(parents=True)
        assert _resolve(install, tmp_path / "home") == (1, str(install / ".venv"))


def _shim_install(home: Path) -> Path:
    install = _plugin_install(home)
    (install / "bin" / "lib").mkdir(parents=True)
    shutil.copy2(SHIM, install / "bin" / "writ")
    shutil.copy2(VENV_LIB, install / "bin" / "lib" / "writ-venv.sh")
    return install


class TestShim:
    def test_the_shim_finds_the_plugin_venv_with_no_plugin_env(self, tmp_path):
        home = tmp_path / "home"
        install = _shim_install(home)
        _exe(_data_venv(home) / "bin" / "python3", 'echo "ran: $*"\n')
        r = subprocess.run([str(install / "bin" / "writ"), "status"], env=_env(home), cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "ran: -m writ.cli status"

    def test_a_missing_venv_names_where_it_belongs_and_the_bootstrap(self, tmp_path):
        home = tmp_path / "home"
        install = _shim_install(home)
        r = subprocess.run([str(install / "bin" / "writ"), "status"], env=_env(home), cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 1
        assert str(_data_venv(home)) in r.stderr
        assert "bootstrap-plugin.sh" in r.stderr


class TestCallSites:
    @pytest.mark.parametrize("path", VENV_CALLERS, ids=lambda p: p.name)
    def test_every_venv_caller_uses_the_one_resolver(self, path):
        assert 'writ_resolve_venv "$' in path.read_text(), f"{path.name} resolves the venv on its own"

    @pytest.mark.parametrize("path", VENV_CALLERS, ids=lambda p: p.name)
    def test_no_caller_hand_rolls_a_venv_path(self, path):
        offenders = [line.strip() for line in path.read_text().splitlines()
                     if re.match(r"\s*(VENV_DIR|VENV_PY|VENV_WRIT)=.*\.venv", line)
                     or re.match(r"\s*for cand in .*\.venv", line)]
        assert offenders == [], f"{path.name}: {offenders}"


class TestDoctorVenv:
    def test_doctor_probes_the_venv_it_runs_under(self, monkeypatch):
        from writ.session import doctor
        monkeypatch.setattr(sys, "prefix", "/some/plugin/data/.venv")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        assert doctor._venv_python() == Path("/some/plugin/data/.venv/bin/python3")

    def test_outside_a_venv_it_falls_back_to_the_clone_venv(self, monkeypatch):
        from writ.session import doctor
        monkeypatch.setattr(sys, "prefix", "/usr")
        monkeypatch.setattr(sys, "base_prefix", "/usr")
        assert doctor._venv_python() == doctor._PACKAGE_ROOT / ".venv" / "bin" / "python3"


def _repoint(venv: Path, install: Path, home: Path, **extra: str) -> subprocess.CompletedProcess:
    return _bash(f'source "{VENV_LIB}"; writ_venv_repoint "{venv}" "{install}"', _env(home, **extra))


def _install(tmp_path: Path) -> Path:
    install = tmp_path / "inst"
    (install / "writ").mkdir(parents=True)
    return install


class TestVenvServesInstall:
    def test_a_venv_serving_this_install_is_left_alone(self, tmp_path):
        install = _install(tmp_path)
        venv = _fake_venv(tmp_path / "venv", serves=f"{install.resolve()}/writ")
        r = _repoint(venv, install, tmp_path / "home")
        assert r.returncode == 0, r.stderr
        assert not (venv / "pip.log").exists()

    def test_a_venv_serving_a_dev_checkout_is_repointed(self, tmp_path):
        """The measured defect: the plugin venv imported the developer checkout."""
        install = _install(tmp_path)
        venv = _fake_venv(tmp_path / "venv", serves="/home/someone/workspaces/Writ/writ")
        r = _repoint(venv, install, tmp_path / "home")
        assert r.returncode == 0, r.stderr
        log = (venv / "pip.log").read_text()
        assert "--no-deps" in log and f"-e {install}" in log
        assert _serves(venv) == f"{install.resolve()}/writ"
        assert "/home/someone/workspaces/Writ/writ" in r.stderr

    def test_an_upgrade_repoints_the_shared_venv_at_the_new_version(self, tmp_path):
        home = tmp_path / "home"
        old = _plugin_install(home, version="1.8.0")
        new = _plugin_install(home, version="1.9.0")
        venv = _fake_venv(_data_venv(home), serves=f"{old.resolve()}/writ")
        r = _repoint(venv, new, home)
        assert r.returncode == 0, r.stderr
        assert _serves(venv) == f"{new.resolve()}/writ"

    def test_a_venv_that_cannot_import_writ_is_repointed(self, tmp_path):
        """What the shared venv looks like once the old version dir has been pruned."""
        install = _install(tmp_path)
        venv = _fake_venv(tmp_path / "venv", serves="")
        r = _repoint(venv, install, tmp_path / "home")
        assert r.returncode == 0, r.stderr
        assert _serves(venv) == f"{install.resolve()}/writ"

    def test_a_failed_repoint_is_reported_with_the_bootstrap_command(self, tmp_path):
        install = _install(tmp_path)
        venv = _fake_venv(tmp_path / "venv", serves="/elsewhere/writ")
        r = _repoint(venv, install, tmp_path / "home", FAKE_PIP_FAIL="1")
        assert r.returncode == 1
        assert "pip: boom" in r.stderr
        assert f"bash {install}/scripts/bootstrap-plugin.sh" in r.stderr

    def test_an_explicit_writ_venv_is_warned_about_not_rewritten(self, tmp_path):
        install = _install(tmp_path)
        venv = _fake_venv(tmp_path / "venv", serves="/elsewhere/writ")
        r = _repoint(venv, install, tmp_path / "home", WRIT_VENV=str(venv))
        assert r.returncode == 1
        assert not (venv / "pip.log").exists()
        assert "WRIT_VENV" in r.stderr

    def test_two_session_starts_at_once_reinstall_only_once(self, tmp_path):
        """Two windows opened together after an upgrade must not run two pip installs into one
        venv: the second waits for the first, sees the venv already serves the install, and
        leaves it alone."""
        install = _install(tmp_path)
        venv = _fake_venv(tmp_path / "venv", serves="/elsewhere/writ")
        env = _env(tmp_path / "home", FAKE_PIP_SLEEP="1")
        cmd = ["bash", "-c", f'source "{VENV_LIB}"; writ_venv_repoint "{venv}" "{install}"']
        procs = [subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True) for _ in range(2)]
        for p in procs:
            _, err = p.communicate(timeout=30)
            assert p.returncode == 0, err
        assert len((venv / "pip.log").read_text().splitlines()) == 1
        assert _serves(venv) == f"{install.resolve()}/writ"

    def test_a_hung_reinstall_is_abandoned_not_waited_on(self, tmp_path):
        """SessionStart must not hang on a pip that stalls (an offline build-backend fetch)."""
        install = _install(tmp_path)
        venv = _fake_venv(tmp_path / "venv", serves="/elsewhere/writ")
        r = _repoint(venv, install, tmp_path / "home", FAKE_PIP_SLEEP="20",
                     WRIT_REPOINT_TIMEOUT="1")
        assert r.returncode == 1
        assert f"bash {install}/scripts/bootstrap-plugin.sh" in r.stderr

    def test_the_probe_ignores_the_callers_cwd(self):
        """Without -I, `python -c` puts the cwd first on sys.path, so a session opened inside a
        Writ checkout would import that checkout and report a false match."""
        assert '/bin/python3" -I -c' in VENV_LIB.read_text()


def _session_start_tree(tmp_path: Path) -> Path:
    """A fake install holding exactly what the hook needs up to its Neo4j probe."""
    skill = tmp_path / "skill"
    for rel in ("hooks/scripts/session-start-bootstrap.sh", "bin/lib/writ-venv.sh"):
        (skill / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, skill / rel)
    (skill / "writ").mkdir()
    return skill


def _run_session_start(skill: Path, data: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = _env(tmp_path / "home", CLAUDE_PLUGIN_ROOT=str(skill), CLAUDE_PLUGIN_DATA=str(data),
               WRIT_NO_AUTOSTART="1", WRIT_NEO4J_HOST="127.0.0.1", WRIT_NEO4J_PORT="1",
               XDG_STATE_HOME=str(tmp_path / "xdg"), WRIT_CACHE_DIR=str(tmp_path / "cache"))
    return subprocess.run(["bash", str(skill / "hooks" / "scripts" / "session-start-bootstrap.sh")],
                          input="{}", env=env, capture_output=True, text=True, timeout=30)


class TestSessionStartRepair:
    def test_session_start_repoints_a_venv_serving_another_install(self, tmp_path):
        skill = _session_start_tree(tmp_path)
        venv = _fake_venv(tmp_path / "data" / ".venv", serves="/elsewhere/writ")
        r = _run_session_start(skill, tmp_path / "data", tmp_path)
        assert r.returncode == 0, r.stderr
        assert f"-e {skill}" in (venv / "pip.log").read_text()
        assert _serves(venv) == f"{skill.resolve()}/writ"

    def test_session_start_leaves_a_matching_venv_alone(self, tmp_path):
        skill = _session_start_tree(tmp_path)
        venv = _fake_venv(tmp_path / "data" / ".venv", serves=f"{skill.resolve()}/writ")
        r = _run_session_start(skill, tmp_path / "data", tmp_path)
        assert r.returncode == 0, r.stderr
        assert not (venv / "pip.log").exists()


    def test_session_start_uses_the_loaders_resolver_over_its_own_tree(self, tmp_path):
        """The hook's own dirname walk is only a fallback. When ${CLAUDE_PLUGIN_ROOT} holds the
        resolver, that copy runs, even if the script itself was run from another tree."""
        staged = _session_start_tree(tmp_path / "staged")
        root = _session_start_tree(tmp_path / "root")
        lib = root / "bin" / "lib" / "writ-venv.sh"
        lib.write_text('echo "ROOT-RESOLVER" >&2\n' + lib.read_text())
        _fake_venv(tmp_path / "data" / ".venv", serves=f"{root.resolve()}/writ")
        env = _env(tmp_path / "home", CLAUDE_PLUGIN_ROOT=str(root),
                   CLAUDE_PLUGIN_DATA=str(tmp_path / "data"), WRIT_NO_AUTOSTART="1",
                   WRIT_NEO4J_HOST="127.0.0.1", WRIT_NEO4J_PORT="1",
                   XDG_STATE_HOME=str(tmp_path / "xdg"), WRIT_CACHE_DIR=str(tmp_path / "cache"))
        r = subprocess.run(["bash", str(staged / "hooks" / "scripts" / "session-start-bootstrap.sh")],
                           input="{}", env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert "ROOT-RESOLVER" in r.stderr


class TestBootstrapVerifiesPackage:
    @pytest.mark.parametrize("path", [BOOTSTRAP, BOOTSTRAP_PLUGIN], ids=lambda p: p.name)
    def test_the_bootstrap_verifies_the_package_it_installed(self, path):
        text = path.read_text()
        assert 'writ_venv_serves "$VENV_DIR" "$WRIT_DIR"' in text
        assert text.index("pip install --quiet") < text.index('writ_venv_serves "$VENV_DIR" "$WRIT_DIR"')


def _docker_env(tmp_path: Path, **extra: str) -> tuple[dict, Path]:
    """A fake `docker` first on PATH: logs each argv line, answers `container inspect` from
    FAKE_CONTAINER, and fails every other verb with a daemon-style error under FAKE_DOCKER_FAIL."""
    bin_dir, log = tmp_path / "fakebin", tmp_path / "docker.log"
    _exe(bin_dir / "docker", f'''
echo "$*" >> "{log}"
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then
    [ -n "${{FAKE_CONTAINER:-}}" ]
    exit $?
fi
if [ -n "${{FAKE_DOCKER_FAIL:-}}" ]; then
    echo "Error response from daemon: boom" >&2
    exit 1
fi
exit 0
''')
    return _env(tmp_path / "home", PATH=f"{bin_dir}:{os.environ['PATH']}", **extra), log


def _neo4j_start(tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    env, log = _docker_env(tmp_path, **extra)
    r = _bash(f'source "{SERVER_LIB}"; writ_neo4j_start "{COMPOSE}"', env)
    return r, (log.read_text().splitlines() if log.exists() else [])


class TestNeo4jStart:
    def test_an_existing_container_is_started_not_composed(self, tmp_path):
        """Whatever project label it carries (a 1.8.0 container says "180")."""
        r, calls = _neo4j_start(tmp_path, FAKE_CONTAINER="1")
        assert r.returncode == 0, r.stderr
        assert calls == ["container inspect writ-neo4j", "start writ-neo4j"]

    def test_no_container_means_compose_up(self, tmp_path):
        r, calls = _neo4j_start(tmp_path)
        assert r.returncode == 0, r.stderr
        assert calls == ["container inspect writ-neo4j", f"compose -f {COMPOSE} up -d neo4j"]

    def test_a_docker_failure_is_surfaced(self, tmp_path):
        r, _ = _neo4j_start(tmp_path, FAKE_CONTAINER="1", FAKE_DOCKER_FAIL="1")
        assert r.returncode != 0
        assert "Error response from daemon: boom" in r.stderr

    def test_the_compose_project_is_named_writ(self):
        """Without it Compose names the project after the install dir (1.8.0 -> "180")."""
        assert re.search(r"^name:\s*writ\s*$", COMPOSE.read_text(), re.M)

    @pytest.mark.parametrize("path", [ENSURE, BOOTSTRAP, BOOTSTRAP_PLUGIN], ids=lambda p: p.name)
    def test_production_start_paths_use_the_helper_and_swallow_nothing(self, path):
        text = path.read_text()
        assert "writ_neo4j_start" in text
        swallowed = [line.strip() for line in text.splitlines()
                     if "up -d neo4j" in line and ("|| true" in line or "2>&1" in line)]
        assert swallowed == []

    def test_ensure_server_stays_non_fatal_and_prints_the_docker_error(self, tmp_path):
        """Waits out ensure-server's 10s bolt loop against a closed port; the daemon half is
        neutralized by WRIT_HEALTH_CMD=true, so nothing is started."""
        env, _ = _docker_env(tmp_path, FAKE_CONTAINER="1", FAKE_DOCKER_FAIL="1",
                             NEO4J_PORT="1", WRIT_HOST="127.0.0.1", WRIT_PORT="1",
                             WRIT_HEALTH_CMD="true", WRIT_LOG=str(tmp_path / "server.log"),
                             XDG_STATE_HOME=str(tmp_path / "xdg"), WRIT_CACHE_DIR=str(tmp_path / "cache"))
        r = subprocess.run(["bash", str(ENSURE)], env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "Error response from daemon: boom" in r.stderr
        assert "could not start Neo4j" in r.stderr

    def test_the_test_graph_container_is_not_compose_managed(self):
        """scripts/test-graph.sh creates writ-test-neo4j with `docker run`, so the project-name
        defect cannot reach it and its naming stays as it is."""
        text = TEST_GRAPH.read_text()
        assert 'CONTAINER_NAME="writ-test-neo4j"' in text
        assert "docker compose" not in text
