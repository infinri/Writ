"""Tests for the non-tech-user bootstrap: scripts/bootstrap.sh + docker-compose.yml.

These are source-inspection tests (no real Docker/pip invocation). They verify
that the bootstrap script contains every required section and that the supporting
infrastructure files exist with the expected shape. Shell execution is exercised
only where a mock PATH + tmp dirs keep it safe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
BOOTSTRAP = SCRIPTS_DIR / "bootstrap.sh"
COMPOSE_FILE = SKILL_DIR / "docker-compose.yml"
ENSURE_SERVER = SCRIPTS_DIR / "ensure-server.sh"
INSTALL_SKILL = SCRIPTS_DIR / "install-skill.sh"


# ---------------------------------------------------------------------------
# File presence + executability
# ---------------------------------------------------------------------------


class TestFilePresence:
    def test_bootstrap_exists(self) -> None:
        assert BOOTSTRAP.exists(), "scripts/bootstrap.sh must exist"

    def test_bootstrap_is_executable(self) -> None:
        assert os.access(BOOTSTRAP, os.X_OK), "scripts/bootstrap.sh must be executable"

    def test_docker_compose_exists(self) -> None:
        assert COMPOSE_FILE.exists(), "docker-compose.yml must exist at repo root"

    def test_install_skill_is_deleted(self) -> None:
        assert not INSTALL_SKILL.exists(), (
            "scripts/install-skill.sh must be deleted — stale and superseded "
            "by install-harness-config.sh + bootstrap.sh"
        )


# ---------------------------------------------------------------------------
# bootstrap.sh contains all required sections (source inspection)
# ---------------------------------------------------------------------------


class TestBootstrapSections:
    """Each required step must leave an identifiable marker in the script source."""

    @pytest.fixture
    def content(self) -> str:
        return BOOTSTRAP.read_text()

    def test_bootstrap_uses_strict_mode(self, content: str) -> None:
        """set -euo pipefail must be present for fail-fast behavior."""
        assert "set -euo pipefail" in content

    def test_bootstrap_checks_python_prerequisite(self, content: str) -> None:
        assert "python3" in content and ("3.11" in content or "3\\.11" in content), (
            "bootstrap.sh must check for python3 >= 3.11"
        )

    def test_bootstrap_checks_docker_prerequisite(self, content: str) -> None:
        assert "docker" in content.lower()
        # Must verify docker daemon is reachable, not just the binary
        assert "docker info" in content or "docker ps" in content, (
            "bootstrap.sh must verify the Docker daemon is running"
        )

    def test_bootstrap_checks_envsubst_prerequisite(self, content: str) -> None:
        assert "envsubst" in content, (
            "bootstrap.sh must check for envsubst (used by install-harness-config.sh)"
        )

    def test_bootstrap_creates_venv(self, content: str) -> None:
        assert "venv" in content and ".venv" in content, (
            "bootstrap.sh must create and use .venv"
        )

    def test_bootstrap_installs_deps(self, content: str) -> None:
        assert "pip install" in content and "-e ." in content, (
            "bootstrap.sh must run `pip install -e .` for editable install"
        )

    def test_bootstrap_invokes_harness_installer(self, content: str) -> None:
        assert "install-harness-config.sh" in content, (
            "bootstrap.sh must invoke install-harness-config.sh"
        )

    def test_bootstrap_creates_rule_and_agent_symlinks(self, content: str) -> None:
        assert "ln -sf" in content or "ln -s" in content, (
            "bootstrap.sh must create symlinks for rules/agents"
        )
        assert "rules" in content and "agents" in content, (
            "bootstrap.sh must handle both rules/ and agents/ directories"
        )

    def test_bootstrap_starts_neo4j_via_compose(self, content: str) -> None:
        assert "docker compose up" in content or "docker-compose up" in content, (
            "bootstrap.sh must start Neo4j via docker compose, not raw docker run"
        )

    def test_bootstrap_waits_for_neo4j(self, content: str) -> None:
        # Waits for bolt port or healthcheck
        assert "7687" in content, "bootstrap.sh must wait for Neo4j bolt port"

    def test_bootstrap_ingests_rules(self, content: str) -> None:
        assert "import-markdown" in content, (
            "bootstrap.sh must run `writ import-markdown`"
        )

    def test_bootstrap_starts_daemon(self, content: str) -> None:
        assert "writ serve" in content, "bootstrap.sh must start the Writ daemon"

    def test_bootstrap_waits_for_daemon_health(self, content: str) -> None:
        assert "/health" in content, "bootstrap.sh must check daemon /health endpoint"

    def test_bootstrap_prints_ready_banner(self, content: str) -> None:
        lowered = content.lower()
        assert "ready" in lowered or "writ is ready" in lowered, (
            "bootstrap.sh must print a 'ready' banner at the end"
        )


# ---------------------------------------------------------------------------
# docker-compose.yml shape
# ---------------------------------------------------------------------------


class TestDockerCompose:
    @pytest.fixture
    def content(self) -> str:
        return COMPOSE_FILE.read_text()

    def test_declares_neo4j_service(self, content: str) -> None:
        assert "neo4j" in content, "docker-compose.yml must declare a neo4j service"

    def test_exposes_bolt_port(self, content: str) -> None:
        assert "7687" in content, "docker-compose.yml must expose bolt port 7687"

    def test_has_named_volume(self, content: str) -> None:
        assert "volumes" in content and ("/data" in content or "data:" in content), (
            "docker-compose.yml must declare a named volume for /data"
        )

    def test_has_healthcheck(self, content: str) -> None:
        assert "healthcheck" in content, (
            "docker-compose.yml must declare a healthcheck for Neo4j"
        )

    def test_has_restart_policy(self, content: str) -> None:
        assert "restart" in content, (
            "docker-compose.yml must declare a restart policy"
        )


# ---------------------------------------------------------------------------
# ensure-server.sh must use docker compose, not raw docker run
# ---------------------------------------------------------------------------


class TestEnsureServerMigration:
    def test_ensure_server_uses_compose(self) -> None:
        content = ENSURE_SERVER.read_text()
        assert "docker compose" in content or "docker-compose" in content, (
            "ensure-server.sh must use docker compose (not raw `docker run`) for Neo4j"
        )

    def test_ensure_server_not_raw_docker_run(self) -> None:
        content = ENSURE_SERVER.read_text()
        # Raw `docker run -d ... neo4j:5` invocation should be gone
        assert "docker run -d" not in content or "neo4j:5" not in content, (
            "ensure-server.sh must not use raw `docker run -d neo4j:5`; use compose"
        )


# ---------------------------------------------------------------------------
# Bootstrap prerequisite-check behavior (runtime, but sandboxed via PATH)
# ---------------------------------------------------------------------------


class TestBootstrapPrerequisiteChecks:
    """Run bootstrap.sh with a stripped PATH and verify it fails cleanly."""

    def _run_with_limited_path(
        self, tmp_path: Path, include_tools: list[str]
    ) -> subprocess.CompletedProcess:
        """Run bootstrap with a PATH containing only the tools we specify."""
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        for tool in include_tools:
            found = shutil.which(tool)
            if found:
                (fake_bin / tool).symlink_to(found)
        env = {"HOME": str(tmp_path), "PATH": str(fake_bin)}
        return subprocess.run(
            [str(BOOTSTRAP)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_fails_cleanly_when_python3_missing(self, tmp_path: Path) -> None:
        # bash is needed to run the script itself, but python3 is omitted
        result = self._run_with_limited_path(tmp_path, ["bash", "docker", "git"])
        assert result.returncode != 0, "bootstrap must fail when python3 is missing"
        combined = (result.stdout + result.stderr).lower()
        assert "python" in combined, "error message must mention python"

    def test_fails_cleanly_when_docker_missing(self, tmp_path: Path) -> None:
        result = self._run_with_limited_path(tmp_path, ["bash", "python3", "git"])
        assert result.returncode != 0, "bootstrap must fail when docker is missing"
        combined = (result.stdout + result.stderr).lower()
        assert "docker" in combined, "error message must mention docker"

    def test_fails_cleanly_when_git_missing(self, tmp_path: Path) -> None:
        result = self._run_with_limited_path(tmp_path, ["bash", "python3", "docker"])
        assert result.returncode != 0, "bootstrap must fail when git is missing"
        combined = (result.stdout + result.stderr).lower()
        assert "git" in combined, "error message must mention git"

    def test_fails_cleanly_when_envsubst_missing(self, tmp_path: Path) -> None:
        result = self._run_with_limited_path(
            tmp_path, ["bash", "python3", "docker", "git"]
        )
        assert result.returncode != 0, "bootstrap must fail when envsubst is missing"
        combined = (result.stdout + result.stderr).lower()
        assert "envsubst" in combined, "error message must mention envsubst"


# ---------------------------------------------------------------------------
# README has Quick Start + Troubleshooting
# ---------------------------------------------------------------------------


class TestReadme:
    @pytest.fixture
    def content(self) -> str:
        return (SKILL_DIR / "README.md").read_text()

    def test_readme_has_quick_start(self, content: str) -> None:
        lowered = content.lower()
        assert "quick start" in lowered or "quickstart" in lowered, (
            "README.md must have a Quick Start section"
        )

    def test_readme_references_bootstrap_script(self, content: str) -> None:
        assert "bootstrap.sh" in content, (
            "README.md Quick Start must reference scripts/bootstrap.sh"
        )

    def test_readme_has_troubleshooting(self, content: str) -> None:
        lowered = content.lower()
        assert "troubleshooting" in lowered or "common errors" in lowered, (
            "README.md must have a troubleshooting section"
        )
