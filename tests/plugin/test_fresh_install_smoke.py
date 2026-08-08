"""Fresh-install smoke test for the full plugin distribution pipeline (Phase D, integration).

This test is intentionally heavyweight and is skipped unless the env var
WRIT_INTEGRATION_TESTS=1 is explicitly set. It exercises the complete install
path: marketplace add, plugin install, bootstrap, and health check.

To run manually:
  1. Ensure claude CLI is installed and authenticated.
  2. Ensure Docker is running (for Neo4j).
  3. Set: export WRIT_INTEGRATION_TESTS=1
  4. Run: pytest tests/plugin/test_fresh_install_smoke.py -v

What the test does (the SHIPPED sequence: marketplace add, plugin install, one script):
  1. Clones the current git checkout to /tmp/writ-fresh-<uuid>/
  2. Runs: claude plugin marketplace add /tmp/writ-fresh-<uuid>
  3. Runs: claude plugin install writ@writ
  4. Reads installPath from `claude plugin list --json` (there is no `claude plugin path`
     subcommand and never has been; a $(...) wrapper around it expanded to the empty
     string and ran `bash /scripts/bootstrap-plugin.sh` -- see CHANGELOG 1.5.1)
  5. Runs: bash <plugin-install-dir>/scripts/bootstrap-plugin.sh
     ONE script: runtime + ~/.claude patch + slash commands. No separate patch step.
  6. Asserts: /health returns {"status":"healthy"}, read WITHOUT curl (the install
     claims no external tools, so the smoke test must not need one either)
  7. Asserts: ${CLAUDE_PLUGIN_DATA}/.venv/bin/python3 exists, settings.json carries the
     Writ entries, ~/.claude/CLAUDE.md exists, and the slash commands are installed
  8. Cleanup: removes marketplace, uninstalls plugin, deletes temp clone
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.plugin.conftest import REPO_ROOT


_INTEGRATION = os.environ.get("WRIT_INTEGRATION_TESTS", "") == "1"


@pytest.mark.integration
@pytest.mark.skipif(
    not _INTEGRATION,
    reason="Set WRIT_INTEGRATION_TESTS=1 to run heavyweight integration tests",
)
class TestFreshInstallSmoke:
    def test_fresh_install_marketplace_plugin_smoke(self, tmp_path: Path) -> None:
        """Full fresh-install pipeline: clone, marketplace add, plugin install, bootstrap, health check.

        See module docstring for manual execution instructions.
        This test exercises the entire Phase A-C delivery end-to-end.
        """
        # Step 1: Clone current checkout to a temp path
        clone_dir = Path(f"/tmp/writ-fresh-{uuid.uuid4().hex[:8]}")
        try:
            subprocess.run(
                ["git", "clone", str(REPO_ROOT), str(clone_dir)],
                check=True,
                capture_output=True,
                timeout=60,
            )

            # Step 2: Add marketplace
            subprocess.run(
                ["claude", "plugin", "marketplace", "add", str(clone_dir)],
                check=True,
                capture_output=True,
                timeout=30,
            )

            # Step 3: Install plugin
            subprocess.run(
                ["claude", "plugin", "install", "writ@writ"],
                check=True,
                capture_output=True,
                timeout=60,
            )

            # Step 4: Determine plugin install dir (installPath, NOT `claude plugin path`)
            result = subprocess.run(
                ["claude", "plugin", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            listing = json.loads(result.stdout or "[]")
            plugin_install_dir = Path(next(
                entry["installPath"] for entry in listing
                if str(entry.get("id", "")).split("@")[0] == "writ"
            ))

            # Step 5: ONE script -- runtime, ~/.claude patch and slash commands
            bootstrap = plugin_install_dir / "scripts" / "bootstrap-plugin.sh"
            assert bootstrap.exists(), f"bootstrap-plugin.sh not found at {bootstrap}"
            subprocess.run(
                ["bash", str(bootstrap)],
                check=True,
                capture_output=True,
                timeout=300,
            )

            # Step 6: Assert health endpoint, read through the install's own stdlib HTTP
            # shim rather than curl: the install requires no external tools, so neither
            # may its smoke test.
            health_result = subprocess.run(
                ["python3", str(plugin_install_dir / "bin" / "lib" / "writ_install.py"),
                 "http-get", "http://localhost:8765/health", "--fail"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert health_result.returncode == 0, (
                f"Writ health endpoint not reachable: {health_result.stderr}"
            )
            assert '"status"' in health_result.stdout and "healthy" in health_result.stdout, (
                f"Writ health endpoint did not return healthy status: {health_result.stdout}"
            )

            # Step 7: Assert venv exists
            plugin_data = Path(
                os.environ.get("CLAUDE_PLUGIN_DATA", str(Path.home() / ".cache" / "writ"))
            )
            venv_python = plugin_data / ".venv" / "bin" / "python3"
            assert venv_python.exists(), (
                f"Expected venv python3 at {venv_python} after bootstrap"
            )

            # Step 7b: the single-script promise -- global config and slash commands are
            # in place with no separate patch step. These used to be documented extra
            # commands, which is exactly what users missed.
            settings = Path.home() / ".claude" / "settings.json"
            assert settings.is_file(), f"{settings} not written by the bootstrap"
            allow = json.loads(settings.read_text()).get("permissions", {}).get("allow", [])
            assert any("writ-session.py" in entry for entry in allow), (
                "the Writ permission entries are missing: the bootstrap did not patch settings.json"
            )
            assert (Path.home() / ".claude" / "CLAUDE.md").is_file(), (
                "~/.claude/CLAUDE.md was not rendered by the bootstrap"
            )
            assert (Path.home() / ".claude" / "commands" / "writ-approve.md").is_file(), (
                "the slash commands were not installed by the bootstrap"
            )

        finally:
            # Cleanup: best-effort; do not raise on cleanup failure
            if clone_dir.exists():
                shutil.rmtree(clone_dir, ignore_errors=True)
            subprocess.run(
                ["claude", "plugin", "uninstall", "writ"],
                capture_output=True,
                timeout=30,
            )
            subprocess.run(
                ["claude", "plugin", "marketplace", "remove", "writ"],
                capture_output=True,
                timeout=30,
            )
