"""Tests for hooks/scripts/session-start-bootstrap.sh (Phase C).

Verifies the SessionStart hook script exists, uses explicit CLAUDE_PLUGIN_ROOT
resolution (not dirname walk), probes the expected services, and exits 0 in
all branches (graceful degradation).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.plugin.conftest import REPO_ROOT

SESSION_START_BOOTSTRAP = REPO_ROOT / "hooks" / "scripts" / "session-start-bootstrap.sh"


class TestSessionStartBootstrapExists:
    def test_session_start_bootstrap_script_exists(self) -> None:
        """hooks/scripts/session-start-bootstrap.sh must exist and be executable."""
        if not SESSION_START_BOOTSTRAP.exists():
            pytest.skip(
                "Phase C artifact hooks/scripts/session-start-bootstrap.sh not yet created"
            )
        assert SESSION_START_BOOTSTRAP.exists()
        assert os.access(SESSION_START_BOOTSTRAP, os.X_OK), (
            "hooks/scripts/session-start-bootstrap.sh must have the executable bit set"
        )


class TestSessionStartBootstrapContent:
    @pytest.fixture()
    def content(self) -> str:
        if not SESSION_START_BOOTSTRAP.exists():
            pytest.skip(
                "Phase C artifact hooks/scripts/session-start-bootstrap.sh not yet created"
            )
        return SESSION_START_BOOTSTRAP.read_text()

    def test_session_start_uses_explicit_plugin_root(self, content: str) -> None:
        """Script must set WRIT_DIR from ${CLAUDE_PLUGIN_ROOT}, not from dirname walk.

        The script lives at hooks/scripts/ (two levels deep), so a dirname walk
        would resolve to hooks/scripts rather than the repo root.
        """
        assert 'WRIT_DIR="${CLAUDE_PLUGIN_ROOT}"' in content or \
               "WRIT_DIR=${CLAUDE_PLUGIN_ROOT}" in content, (
            "session-start-bootstrap.sh must set WRIT_DIR from ${CLAUDE_PLUGIN_ROOT} explicitly"
        )
        # Should NOT use dirname-based resolution for WRIT_DIR
        assert 'dirname "$0"' not in content or "WRIT_DIR" not in content.split('dirname "$0"')[0].split('\n')[-1], (
            "session-start-bootstrap.sh must not use dirname walk to resolve WRIT_DIR"
        )

    def test_session_start_probes_venv(self, content: str) -> None:
        """The venv comes from the shared resolver and its python3 is probed before use."""
        assert 'writ_resolve_venv "${WRIT_DIR}"' in content, (
            "session-start-bootstrap.sh must take VENV_DIR from writ_resolve_venv"
        )
        assert '"${VENV_DIR}/bin/python3"' in content, (
            "session-start-bootstrap.sh venv probe must check for the python3 binary"
        )

    def test_session_start_probes_neo4j(self, content: str) -> None:
        """Script must contain a TCP probe for Neo4j bolt port 7687."""
        assert "7687" in content, (
            "session-start-bootstrap.sh must probe Neo4j bolt port 7687"
        )
        # Accept any of the common probe mechanisms
        has_probe = (
            "nc -z" in content
            or "curl" in content
            or "/dev/tcp/" in content
            or "bash -c" in content
        )
        assert has_probe, (
            "session-start-bootstrap.sh must probe port 7687 via nc -z, curl, or /dev/tcp/"
        )

    def test_session_start_ensures_server_via_shared_lib(self, content: str) -> None:
        """Server health/start is delegated to the flock-guarded shared lib (which probes /health)."""
        assert "writ-server-lib.sh" in content and "writ_ensure_server" in content, (
            "session-start-bootstrap.sh must source writ-server-lib.sh and call writ_ensure_server"
        )
        lib = (REPO_ROOT / "scripts" / "lib" / "writ-server-lib.sh").read_text()
        assert "/health" in lib, "the shared server lib must probe /health"

    def test_session_start_graceful_degradation(self, content: str) -> None:
        """Script must exit 0 in all branches; must not exit 1 for missing venv or Neo4j."""
        lines = content.splitlines()
        hard_exits = [
            line.strip() for line in lines
            if line.strip().startswith("exit 1")
        ]
        assert not hard_exits, (
            "session-start-bootstrap.sh must not call 'exit 1' — all degradation paths "
            f"must exit 0. Found: {hard_exits}"
        )