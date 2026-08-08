"""Tests for scripts/bootstrap-plugin.sh (Phase C).

Verifies the plugin bootstrap script exists, is executable, uses the correct
CLAUDE_PLUGIN_DATA-based venv path, installs via pip install -e, checks
required prerequisites, and is idempotent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.plugin.conftest import REPO_ROOT

BOOTSTRAP_PLUGIN = REPO_ROOT / "scripts" / "bootstrap-plugin.sh"


class TestBootstrapPluginExists:
    def test_bootstrap_plugin_script_exists(self) -> None:
        """scripts/bootstrap-plugin.sh must exist and be executable."""
        if not BOOTSTRAP_PLUGIN.exists():
            pytest.skip("Phase C artifact scripts/bootstrap-plugin.sh not yet created")
        assert BOOTSTRAP_PLUGIN.exists(), "scripts/bootstrap-plugin.sh must exist"
        assert os.access(BOOTSTRAP_PLUGIN, os.X_OK), (
            "scripts/bootstrap-plugin.sh must have the executable bit set"
        )


class TestBootstrapPluginContent:
    @pytest.fixture()
    def content(self) -> str:
        if not BOOTSTRAP_PLUGIN.exists():
            pytest.skip("Phase C artifact scripts/bootstrap-plugin.sh not yet created")
        return BOOTSTRAP_PLUGIN.read_text()

    def test_bootstrap_plugin_uses_plugin_data_for_venv(self, content: str) -> None:
        """Venv path must use ${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv."""
        assert "${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}" in content, (
            "bootstrap-plugin.sh must use ${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ} for venv base"
        )
        assert "${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv" in content, (
            "bootstrap-plugin.sh venv path must be ${CLAUDE_PLUGIN_DATA:-$HOME/.cache/writ}/.venv"
        )

    def test_bootstrap_plugin_uses_pip_install_editable(self, content: str) -> None:
        """Script must use pip install -e referencing ${CLAUDE_PLUGIN_ROOT}."""
        assert "pip install -e" in content, (
            "bootstrap-plugin.sh must use 'pip install -e' for editable install"
        )
        assert "${CLAUDE_PLUGIN_ROOT}" in content, (
            "bootstrap-plugin.sh must reference ${CLAUDE_PLUGIN_ROOT} so upgrades rebind"
        )

    def test_bootstrap_plugin_installs_dev_extras(self, content: str) -> None:
        """After Finding D (Approach C, 2026-05-14), bootstrap-plugin.sh
        installs with [dev] extras so optimum is available for the ONNX
        export step. The [fallback] group (sentence-transformers) is
        intentionally NOT installed by default."""
        assert "[dev]" in content, (
            "bootstrap-plugin.sh must install -e '${WRIT_DIR}[dev]' (or "
            "equivalent) so optimum is available for the ONNX export step. "
            "If you intentionally moved to bare -e install, update this "
            "test AND the install contract in pyproject.toml."
        )

    def test_bootstrap_plugin_exports_onnx_model(self, content: str) -> None:
        """bootstrap-plugin.sh must produce the ONNX model on disk so the
        daemon takes the production ONNX path on first start. Without this,
        a fresh plugin install runs `writ serve` and the daemon refuses
        to start (see writ/retrieval/pipeline.py three-state ONNX contract,
        commit dae679a)."""
        assert "scripts/export_onnx.py" in content, (
            "bootstrap-plugin.sh must run scripts/export_onnx.py to produce "
            "the ONNX model before the daemon starts."
        )

    def test_bootstrap_plugin_checks_prerequisites(self, content: str) -> None:
        """Script must REQUIRE python3 >= 3.11 and docker -- and nothing else.

        This test used to assert that jq, curl and envsubst were required too. That was
        the defect, not the contract: all three were install-time-only conveniences (JSON
        merges, string substitution, HTTP), each now has a Python-stdlib fallback, and
        requiring them turned a working machine into a failed install. jq and curl are
        still named, as OPTIONAL accelerators; the gettext substitution step is gone.
        """
        lowered = content.lower()
        assert "python3" in lowered, (
            "bootstrap-plugin.sh must check for python3"
        )
        assert "3.11" in content or "3\\.11" in content, (
            "bootstrap-plugin.sh must verify python3 >= 3.11"
        )
        assert "docker" in lowered, (
            "bootstrap-plugin.sh must check for docker"
        )
        assert "require_tool jq" not in content, (
            "jq must not be a hard prerequisite: parsed_field/parsed_bool fall back to python3"
        )
        assert "require_tool curl" not in content, (
            "curl must not be a hard prerequisite: writ_http_get/writ_http_post fall back to urllib"
        )
        assert "optional_tool jq" in content and "optional_tool curl" in content, (
            "bootstrap-plugin.sh must still report jq and curl as optional accelerators, so a "
            "user can see the fast path is available (or not) without being blocked"
        )

    def test_bootstrap_plugin_is_the_only_step_a_plugin_install_needs(self, content: str) -> None:
        """One script run must leave settings.json patched, CLAUDE.md rendered and the
        slash commands installed -- the two steps users used to miss because they were
        documented separately, after the bootstrap."""
        assert "patch-global-config.sh" in content, (
            "bootstrap-plugin.sh must invoke patch-global-config.sh (permissions + "
            "statusLine + CLAUDE.md: the things a plugin manifest cannot ship)"
        )
        assert "install-user-commands.sh" in content, (
            "bootstrap-plugin.sh must invoke install-user-commands.sh so /writ-approve "
            "works from any project after one run"
        )

    def test_bootstrap_plugin_has_a_preflight_flag(self, content: str) -> None:
        """--preflight runs only the prerequisite checks, so the tool contract is
        testable without Docker, pip and an ONNX export."""
        assert "--preflight" in content

    def test_bootstrap_plugin_idempotent(self, content: str, tmp_path: Path) -> None:
        """Running bootstrap-plugin.sh twice must not fail; second run is a no-op or re-syncs.

        Note: This test is a structural check only. Full idempotency requires a
        shell sandbox with a real venv stub directory. To run manually:
          1. Set CLAUDE_PLUGIN_ROOT=/path/to/Writ
          2. Set CLAUDE_PLUGIN_DATA=/tmp/writ-idem-test
          3. Run bootstrap-plugin.sh twice; verify exit 0 both times.
        """
        pytest.skip(
            "requires shell sandbox: full idempotency test needs real venv stub; "
            "run manually per docstring instructions"
        )
