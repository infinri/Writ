"""Program item 3: the install side of the Neo4j security baseline.

Covers the compose file (text and rendered), the shell library functions that start, secure and
guard Neo4j (scripts/lib/writ-server-lib.sh), the bootstrap ordering, the systemd unit's
exit-status line, the carry-forward of the install's config file across a venv repoint, and the
documentation and example updates.

Every shell test drives the real bash under a tmp HOME with a FAKE `docker` and a FAKE venv `writ`
first on PATH / in VENV_DIR, so nothing reaches a docker daemon, a real venv or a real daemon. No
test reads or writes the real install's config file; the carry-forward tests build their own
install directories under tmp_path with content that holds no credential-shaped literal.

New names are imported inside each test so a missing implementation fails that test only.
"""
from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

from writ.config import DEFAULT_NEO4J_PASSWORD

REPO = Path(__file__).resolve().parent.parent
COMPOSE = REPO / "docker-compose.yml"
SERVER_LIB = REPO / "scripts" / "lib" / "writ-server-lib.sh"
VENV_LIB = REPO / "bin" / "lib" / "writ-venv.sh"
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
BOOTSTRAP_PLUGIN = REPO / "scripts" / "bootstrap-plugin.sh"
SERVICE_INSTALLER = REPO / "scripts" / "install-server-service.sh"
EXAMPLE = REPO / "writ.toml.example"
GITIGNORE = REPO / ".gitignore"

REMEDY = "writ neo4j set-password"

_STRIPPED_ENV = (
    "WRIT_VENV", "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "WRIT_NEO4J_PASSWORD",
    "WRIT_ALLOW_DEV_PASSWORD", "WRIT_HEALTH_CMD", "WRIT_SERVE_CMD", "VENV_DIR",
    "WRIT_NEO4J_HEALTHY_WAIT", "WRIT_DIR",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


_FAKE_WRIT = """
echo "$*" >> "__LOG__"
case "$1 $2" in
    "neo4j check") exit "${FAKE_CHECK:-0}" ;;
    "neo4j set-password") exit "${FAKE_SETPW:-0}" ;;
    "neo4j password")
        if [ -n "${FAKE_STORED:-}" ]; then printf '%s\\n' "$FAKE_STORED"; exit 0; fi
        exit 78 ;;
esac
exit 0
"""

_FAKE_DOCKER = """
echo "$*" >> "__LOG__"
if [ "$1" = "container" ] && [ "$2" = "inspect" ]; then
    case "$*" in
        *Labels*) printf '%s\\n' "${FAKE_PROJECT:-}"; exit 0 ;;
        *Health*) printf '%s\\n' "${FAKE_HEALTH:-}"; exit 0 ;;
    esac
    [ -n "${FAKE_CONTAINER:-}" ]
    exit $?
fi
if [ "$1" = "port" ]; then
    printf '%b' "${FAKE_PORTS:-}"
    exit 0
fi
if [ "$1" = "compose" ]; then
    echo "[${WRIT_NEO4J_PASSWORD-UNSET}]" >> "__COMPOSE_ENV__"
fi
exit 0
"""


class Sandbox:
    """A tmp HOME, a fake docker on PATH and a fake venv `writ`, each logging its argv."""

    def __init__(self, tmp_path: Path, **env: str) -> None:
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir(exist_ok=True)
        self.venv = tmp_path / "venv"
        self.writ_log = tmp_path / "writ.log"
        self.docker_log = tmp_path / "docker.log"
        self.compose_env = tmp_path / "compose.env"
        _exe(self.venv / "bin" / "writ", _FAKE_WRIT.replace("__LOG__", str(self.writ_log)))
        _exe(
            tmp_path / "fakebin" / "docker",
            _FAKE_DOCKER.replace("__LOG__", str(self.docker_log)).replace(
                "__COMPOSE_ENV__", str(self.compose_env)
            ),
        )
        base = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
        base["HOME"] = str(self.home)
        base["PATH"] = f"{tmp_path / 'fakebin'}:{os.environ['PATH']}"
        base.update(env)
        self.env = base

    def bash(self, script: str, timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", script], env=self.env, capture_output=True, text=True, timeout=timeout
        )

    def lib_call(self, call: str, **kwargs) -> subprocess.CompletedProcess:
        """Source the server library, run `call`, print its status as `rc=<n>` on the last line."""
        return self.bash(f'source "{SERVER_LIB}"; {call}; echo "rc=$?"', **kwargs)

    @staticmethod
    def rc(result: subprocess.CompletedProcess) -> int:
        return int(result.stdout.strip().splitlines()[-1].removeprefix("rc="))

    def writ_calls(self) -> list[str]:
        return self.writ_log.read_text().splitlines() if self.writ_log.exists() else []

    def docker_calls(self) -> list[str]:
        return self.docker_log.read_text().splitlines() if self.docker_log.exists() else []

    def compose_passwords(self) -> list[str]:
        return self.compose_env.read_text().splitlines() if self.compose_env.exists() else []


@pytest.fixture()
def sandbox(tmp_path):
    return Sandbox(tmp_path)


def _code_lines(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]


def _first_code_index(path: Path, needle: str) -> int:
    """Offset of the first NON-COMMENT line containing `needle` (comments mention these names)."""
    offset = 0
    for line in path.read_text().splitlines(keepends=True):
        if needle in line and not line.lstrip().startswith("#"):
            return offset
        offset += len(line)
    raise AssertionError(f"no code line containing {needle!r} in {path.name}")


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------


class TestComposeText:
    def test_both_ports_are_published_on_loopback_only(self):
        text = COMPOSE.read_text()
        mappings = re.findall(r'^\s*-\s*"([^"]*:(?:7474|7687))"', text, re.M)
        assert sorted(mappings) == ["127.0.0.1:7474:7474", "127.0.0.1:7687:7687"]

    def test_no_bare_port_mapping_survives(self):
        text = COMPOSE.read_text()
        assert '"7474:7474"' not in text
        assert '"7687:7687"' not in text

    def test_neo4j_auth_and_the_healthcheck_read_the_environment_variable(self):
        lines = COMPOSE.read_text().splitlines()
        auth = [line for line in lines if "NEO4J_AUTH" in line and not line.lstrip().startswith("#")]
        health = [line for line in lines if "cypher-shell" in line and not line.lstrip().startswith("#")]
        assert len(auth) == 1 and "${WRIT_NEO4J_PASSWORD:-" in auth[0]
        assert len(health) == 1 and "${WRIT_NEO4J_PASSWORD:-" in health[0]

    def test_the_development_default_appears_exactly_twice_both_as_fallbacks(self):
        text = COMPOSE.read_text()
        assert text.count(DEFAULT_NEO4J_PASSWORD) == 2
        assert text.count("${WRIT_NEO4J_PASSWORD:-" + DEFAULT_NEO4J_PASSWORD + "}") == 2


def _compose_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "compose", "version"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _compose_available(), reason="docker compose is not installed")
class TestComposeRendered:
    def _render(self, **env: str) -> str:
        base = {k: v for k, v in os.environ.items() if k != "WRIT_NEO4J_PASSWORD"}
        base.update(env)
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "config"],
            env=base, capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    def test_the_rendered_ports_are_bound_to_loopback_twice(self):
        rendered = self._render(WRIT_NEO4J_PASSWORD=uuid.uuid4().hex)
        assert rendered.count("host_ip: 127.0.0.1") == 2

    def test_an_exported_value_reaches_the_auth_variable_and_the_healthcheck(self):
        value = uuid.uuid4().hex
        rendered = self._render(WRIT_NEO4J_PASSWORD=value)
        assert f"NEO4J_AUTH: neo4j/{value}" in rendered
        assert rendered.count(value) == 2, "the value must reach NEO4J_AUTH and the healthcheck"
        assert DEFAULT_NEO4J_PASSWORD not in rendered

    def test_an_unset_variable_falls_back_to_the_development_default(self):
        rendered = self._render()
        assert f"NEO4J_AUTH: neo4j/{DEFAULT_NEO4J_PASSWORD}" in rendered


# ---------------------------------------------------------------------------
# writ_neo4j_start
# ---------------------------------------------------------------------------


class TestNeo4jStartPassword:
    def _start(self, sandbox: Sandbox, **extra: str) -> subprocess.CompletedProcess:
        sandbox.env.update(extra)
        return sandbox.lib_call(f'VENV_DIR="{sandbox.venv}"; writ_neo4j_start "{COMPOSE}"')

    def test_the_exported_password_is_handed_to_compose(self, sandbox):
        exported = uuid.uuid4().hex
        result = self._start(sandbox, WRIT_NEO4J_PASSWORD=exported, FAKE_STORED=uuid.uuid4().hex)
        assert sandbox.rc(result) == 0, result.stderr
        assert sandbox.compose_passwords() == [f"[{exported}]"]

    def test_the_stored_password_is_used_when_nothing_is_exported(self, sandbox):
        stored = uuid.uuid4().hex
        result = self._start(sandbox, FAKE_STORED=stored)
        assert sandbox.rc(result) == 0, result.stderr
        assert sandbox.compose_passwords() == [f"[{stored}]"]

    def test_compose_gets_an_empty_value_when_there_is_none(self, sandbox):
        """Empty, so the compose file's own fallback applies (the CLI prints nothing, exit 78)."""
        result = self._start(sandbox)
        assert sandbox.rc(result) == 0, result.stderr
        assert sandbox.compose_passwords() == ["[]"]

    def test_the_compose_invocation_keeps_its_pinned_form(self, sandbox):
        self._start(sandbox)
        assert f"compose -f {COMPOSE} up -d neo4j" in sandbox.docker_calls()
        assert 'docker compose -f "$1" up -d neo4j' in SERVER_LIB.read_text()

    def test_an_existing_container_is_started_and_compose_is_not_run(self, sandbox):
        result = self._start(sandbox, FAKE_CONTAINER="1", FAKE_STORED=uuid.uuid4().hex)
        assert sandbox.rc(result) == 0, result.stderr
        assert sandbox.compose_passwords() == []
        assert "start writ-neo4j" in sandbox.docker_calls()


# ---------------------------------------------------------------------------
# writ_neo4j_secure
# ---------------------------------------------------------------------------


_LOOPBACK_PORTS = "7474/tcp -> 127.0.0.1:7474\\n7687/tcp -> 127.0.0.1:7687\\n"


class TestNeo4jSecure:
    def _secure(self, sandbox: Sandbox, **extra: str) -> subprocess.CompletedProcess:
        sandbox.env.update(extra)
        return sandbox.lib_call(f'writ_neo4j_secure "{COMPOSE}" "{sandbox.venv}/bin/writ"')

    def test_a_private_password_returns_0_and_changes_nothing(self, sandbox):
        result = self._secure(sandbox, FAKE_CHECK="0", FAKE_PROJECT="writ", FAKE_HEALTH="healthy",
                              FAKE_PORTS=_LOOPBACK_PORTS)
        assert sandbox.rc(result) == 0, result.stderr
        assert sandbox.writ_calls() == ["neo4j check"]
        assert not any(call.startswith("compose") for call in sandbox.docker_calls())

    @pytest.mark.parametrize("ports", [
        "7474/tcp -> 0.0.0.0:7474\\n7687/tcp -> 0.0.0.0:7687\\n",
        "7474/tcp -> 127.0.0.1:7474\\n7687/tcp -> [::]:7687\\n",
    ], ids=["all-interfaces", "one-public-port"])
    def test_a_rerun_re_creates_a_container_whose_ports_are_not_loopback_only(
        self, sandbox, ports
    ):
        """A previous run changed the password, then the re-create or healthy wait failed. The
        password check now passes, so only the container's own state says the job is unfinished."""
        stored = uuid.uuid4().hex
        result = self._secure(sandbox, FAKE_CHECK="0", FAKE_PROJECT="writ", FAKE_HEALTH="healthy",
                              FAKE_PORTS=ports, FAKE_STORED=stored)
        assert sandbox.rc(result) == 10, result.stderr
        assert not any("set-password" in call for call in sandbox.writ_calls())
        assert f"compose -f {COMPOSE} up -d neo4j" in sandbox.docker_calls()
        assert sandbox.compose_passwords() == [f"[{stored}]"]

    def test_a_rerun_re_creates_a_loopback_container_whose_healthcheck_fails(self, sandbox):
        """A fresh install's container is loopback-bound from the start but its healthcheck holds
        the password it was created with; unhealthy after a change means it was never re-created."""
        stored = uuid.uuid4().hex
        sandbox.env.update(FAKE_CHECK="0", FAKE_PROJECT="writ", FAKE_PORTS=_LOOPBACK_PORTS,
                           FAKE_STORED=stored, WRIT_NEO4J_HEALTHY_WAIT="0")
        result = sandbox.lib_call(
            f'FAKE_HEALTH=unhealthy writ_neo4j_secure "{COMPOSE}" "{sandbox.venv}/bin/writ"'
        )
        assert f"compose -f {COMPOSE} up -d neo4j" in sandbox.docker_calls()
        assert sandbox.compose_passwords() == [f"[{stored}]"]
        assert sandbox.rc(result) == 1  # the fake keeps answering unhealthy after the re-create

    def test_a_rerun_against_an_older_project_container_still_returns_11(self, sandbox):
        result = self._secure(sandbox, FAKE_CHECK="0", FAKE_PROJECT="180",
                              FAKE_PORTS="7687/tcp -> 0.0.0.0:7687\\n")
        assert sandbox.rc(result) == 11, result.stderr
        assert not any(call.startswith("compose") for call in sandbox.docker_calls())

    def test_no_container_means_nothing_to_finish(self, sandbox):
        result = self._secure(sandbox, FAKE_CHECK="0")
        assert sandbox.rc(result) == 0, result.stderr
        assert not any(call.startswith("compose") for call in sandbox.docker_calls())

    def test_set_password_is_called_with_if_default_so_concurrent_bootstraps_rotate_once(
        self, sandbox
    ):
        self._secure(sandbox, FAKE_CHECK="78", FAKE_PROJECT="writ", FAKE_HEALTH="healthy",
                     FAKE_STORED=uuid.uuid4().hex)
        assert "neo4j set-password --wait 90 --lock-timeout 180 --if-default" in sandbox.writ_calls()

    def test_a_replaced_password_and_a_healthy_recreated_container_returns_10(self, sandbox):
        stored = uuid.uuid4().hex
        result = self._secure(
            sandbox, FAKE_CHECK="78", FAKE_PROJECT="writ", FAKE_HEALTH="healthy", FAKE_STORED=stored
        )
        assert sandbox.rc(result) == 10, result.stderr
        assert "neo4j set-password --wait 90 --lock-timeout 180 --if-default" in sandbox.writ_calls()
        assert f"compose -f {COMPOSE} up -d neo4j" in sandbox.docker_calls()
        assert sandbox.compose_passwords() == [f"[{stored}]"]

    def test_set_password_runs_before_compose(self, sandbox):
        self._secure(sandbox, FAKE_CHECK="78", FAKE_PROJECT="writ", FAKE_HEALTH="healthy",
                     FAKE_STORED=uuid.uuid4().hex)
        calls = sandbox.writ_calls()
        assert calls.index("neo4j set-password --wait 90 --lock-timeout 180 --if-default") < calls.index("neo4j password")

    def test_a_failed_set_password_returns_1_and_never_composes(self, sandbox):
        result = self._secure(sandbox, FAKE_CHECK="78", FAKE_SETPW="1", FAKE_PROJECT="writ")
        assert sandbox.rc(result) == 1
        assert sandbox.compose_passwords() == []
        assert not any(call.startswith("compose") for call in sandbox.docker_calls())

    def test_an_older_project_container_returns_11_and_is_left_alone(self, sandbox):
        result = self._secure(sandbox, FAKE_CHECK="78", FAKE_PROJECT="180",
                              FAKE_STORED=uuid.uuid4().hex)
        assert sandbox.rc(result) == 11, result.stderr
        assert sandbox.compose_passwords() == []
        assert not any(call.startswith("compose") for call in sandbox.docker_calls())
        assert "Securing an existing install" in result.stderr
        assert "180" in result.stderr

    def test_a_container_that_never_turns_healthy_returns_1_naming_the_logs(self, sandbox):
        result = self._secure(
            sandbox, FAKE_CHECK="78", FAKE_PROJECT="writ", FAKE_HEALTH="starting",
            FAKE_STORED=uuid.uuid4().hex, WRIT_NEO4J_HEALTHY_WAIT="0",
        )
        assert sandbox.rc(result) == 1
        assert "docker logs" in result.stderr


# ---------------------------------------------------------------------------
# writ_dev_password_blocks_start and the shared start routine
# ---------------------------------------------------------------------------


class TestStartGuard:
    def _blocks(self, sandbox: Sandbox, venv: str | None = None, **extra: str):
        sandbox.env.update(extra)
        target = venv if venv is not None else str(sandbox.venv)
        return sandbox.lib_call(f'VENV_DIR="{target}"; writ_dev_password_blocks_start')

    def test_a_refused_password_blocks_the_start_and_prints_the_remedy_and_the_opt_in(
        self, sandbox
    ):
        result = self._blocks(sandbox, FAKE_CHECK="78")
        assert sandbox.rc(result) == 0
        assert REMEDY in result.stderr
        assert "WRIT_ALLOW_DEV_PASSWORD=1" in result.stderr
        assert DEFAULT_NEO4J_PASSWORD not in result.stderr

    @pytest.mark.parametrize("check_status", ["0", "1"])
    def test_any_other_outcome_fails_open_and_prints_nothing(self, sandbox, check_status):
        result = self._blocks(sandbox, FAKE_CHECK=check_status)
        assert sandbox.rc(result) == 1
        assert result.stderr == ""

    def test_no_venv_cli_fails_open_and_prints_nothing(self, sandbox, tmp_path):
        result = self._blocks(sandbox, venv=str(tmp_path / "no-such-venv"))
        assert sandbox.rc(result) == 1
        assert result.stderr == ""
        assert sandbox.writ_calls() == []

    def test_the_library_compares_against_the_shared_exit_code_constant(self):
        from writ.config import DEV_REFUSAL_EXIT_CODE

        assert f"-eq {DEV_REFUSAL_EXIT_CODE}" in SERVER_LIB.read_text()

    def test_ensure_server_prints_the_notice_and_never_launches_serve(self, sandbox, tmp_path):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        install = tmp_path / "install"
        install.mkdir()
        sandbox.env.update(
            FAKE_CHECK="78", WRIT_HEALTH_CMD="false", WRIT_PORT=str(port),
            WRIT_HOST="127.0.0.1", WRIT_DIR=str(install), VENV_DIR=str(sandbox.venv),
            WRIT_LOG=str(tmp_path / "server.log"), WRIT_CACHE_DIR=str(tmp_path / "cache"),
            XDG_STATE_HOME=str(tmp_path / "xdg"),
        )
        result = sandbox.lib_call("writ_ensure_server", timeout=90)
        assert sandbox.rc(result) == 0, result.stderr
        assert REMEDY in result.stderr
        assert not any(call.startswith("serve") for call in sandbox.writ_calls())


# ---------------------------------------------------------------------------
# bootstraps, service unit, .gitignore
# ---------------------------------------------------------------------------


class TestBootstrapOrdering:
    @pytest.mark.parametrize("path", [BOOTSTRAP, BOOTSTRAP_PLUGIN], ids=lambda p: p.name)
    def test_the_password_is_secured_after_neo4j_starts_and_before_the_corpus_import(self, path):
        start = _first_code_index(path, "writ_neo4j_start")
        secure = _first_code_index(path, "writ_neo4j_secure")
        ingest = _first_code_index(path, "import-cypher")
        assert start < secure < ingest

    @pytest.mark.parametrize("path", [BOOTSTRAP, BOOTSTRAP_PLUGIN], ids=lambda p: p.name)
    def test_a_failure_to_secure_exits_1_before_the_import(self, path):
        text = path.read_text()
        secure = _first_code_index(path, "writ_neo4j_secure")
        ingest = _first_code_index(path, "import-cypher")
        assert "exit 1" in text[secure:ingest]

    def test_the_plugin_bootstrap_carries_the_config_before_its_first_pip_install(self):
        carry = _first_code_index(BOOTSTRAP_PLUGIN, "writ_carry_config")
        pip = _first_code_index(BOOTSTRAP_PLUGIN, "pip install")
        assert carry < pip

    def test_the_service_unit_does_not_restart_a_refused_start(self):
        from writ.config import DEV_REFUSAL_EXIT_CODE

        assert f"RestartPreventExitStatus={DEV_REFUSAL_EXIT_CODE}" in SERVICE_INSTALLER.read_text()

    def test_the_set_password_temp_file_is_gitignored(self):
        assert ".writ.toml.*" in GITIGNORE.read_text().splitlines()


# ---------------------------------------------------------------------------
# writ_venv_repoint carries the install's config file forward
# ---------------------------------------------------------------------------


def _fake_venv(venv: Path, serves: str) -> Path:
    """A venv whose python3 prints the `writ` dir in <venv>/serves, and whose `-m pip install -e
    <root>` records <root>/writ as what it now serves."""
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "serves").write_text(serves)
    _exe(venv / "bin" / "python3", f'''
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
    for a; do last="$a"; done
    (cd "$last" && printf '%s/writ' "$(pwd -P)") > "{venv}/serves"
    exit 0
fi
s="$(cat "{venv}/serves")"
[ -n "$s" ] || exit 1
printf '%s\\n' "$s"
''')
    return venv


class TestConfigCarriedAcrossARepoint:
    CONTENT = "[logs]\nkeep = 3\n"

    def _repoint(self, tmp_path: Path, new_content: str | None):
        old, new = tmp_path / "old", tmp_path / "new"
        (old / "writ").mkdir(parents=True)
        (new / "writ").mkdir(parents=True)
        (old / "writ.toml").write_text(self.CONTENT)
        (old / "writ.toml").chmod(0o644)
        if new_content is not None:
            (new / "writ.toml").write_text(new_content)
        venv = _fake_venv(tmp_path / "venv", str((old / "writ").resolve()))
        env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_ENV}
        env["HOME"] = str(tmp_path / "home")
        result = subprocess.run(
            ["bash", "-c", f'source "{VENV_LIB}"; writ_venv_repoint "{venv}" "{new}"'],
            env=env, capture_output=True, text=True, timeout=60,
        )
        return result, new / "writ.toml"

    def test_a_missing_config_is_copied_with_identical_bytes_and_mode_0600(self, tmp_path):
        result, carried = self._repoint(tmp_path, None)
        assert result.returncode == 0, result.stderr
        assert carried.read_text() == self.CONTENT
        assert stat.S_IMODE(carried.stat().st_mode) == 0o600

    def test_an_existing_config_is_never_overwritten(self, tmp_path):
        existing = "[logs]\nkeep = 9\n"
        result, kept = self._repoint(tmp_path, existing)
        assert result.returncode == 0, result.stderr
        assert kept.read_text() == existing

    def test_the_carry_function_is_defined_by_the_venv_library(self):
        assert "writ_carry_config()" in VENV_LIB.read_text()


# ---------------------------------------------------------------------------
# example file and documentation
# ---------------------------------------------------------------------------


class TestExampleAndDocs:
    def test_the_example_parses_and_its_neo4j_section_has_no_password(self):
        parsed = tomllib.loads(EXAMPLE.read_text())
        assert "password" not in parsed.get("neo4j", {})
        assert parsed["neo4j"]["uri"]

    def test_the_example_does_not_carry_the_development_password(self):
        assert DEFAULT_NEO4J_PASSWORD not in EXAMPLE.read_text()

    @pytest.mark.parametrize(
        "path",
        [REPO / "docs" / "install.md", REPO / "SECURITY.md", REPO / "HANDBOOK.md"],
        ids=lambda p: p.name,
    )
    def test_the_docs_name_the_set_password_command(self, path):
        assert REMEDY in path.read_text()

    @pytest.mark.parametrize(
        "path",
        [REPO / "docs" / "install.md", REPO / "docs" / "reference" / "configuration.md",
         COMPOSE, REPO / "HANDBOOK.md", REPO / "SECURITY.md",
         REPO / "docs" / "adr" / "ADR-neo4j-password-baseline.md"],
        ids=lambda p: p.name,
    )
    def test_no_doc_recommends_a_persistent_export_of_the_password(self, path):
        """An exported value outlives the compose call and overrides writ.toml after the next
        rotation; the documented form scopes it to the one command."""
        assert "export WRIT_NEO4J_PASSWORD" not in path.read_text()

    def test_security_says_who_can_read_the_password_through_docker(self):
        text = " ".join((REPO / "SECURITY.md").read_text().split())
        for phrase in ("docker inspect", "NEO4J_AUTH", "healthcheck", "process list",
                       "never on the host", "root-equivalent"):
            assert phrase in text, phrase

    def test_the_docs_describe_concurrent_runs(self):
        adr = (REPO / "docs" / "adr" / "ADR-neo4j-password-baseline.md").read_text()
        install = (REPO / "docs" / "install.md").read_text()
        for text in (adr, install):
            assert "--if-default" in text
            assert ".writ.toml.lock" in text

    def test_the_decision_record_exists(self):
        adr = REPO / "docs" / "adr" / "ADR-neo4j-password-baseline.md"
        assert adr.is_file()
        assert REMEDY in adr.read_text()
