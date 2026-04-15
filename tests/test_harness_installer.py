"""Tests for scripts/install-harness-config.sh and the templates it consumes.

The installer renders $HOME-parameterized templates into ~/.claude/settings.json
and ~/.claude/CLAUDE.md. Tests use a tmp target dir via the WRIT_INSTALL_TARGET
env var (the installer honors this for testability) so no real files are touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


SKILL_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = SKILL_DIR / "templates"
INSTALLER = SKILL_DIR / "scripts" / "install-harness-config.sh"


# ---------------------------------------------------------------------------
# Template presence + content
# ---------------------------------------------------------------------------


class TestTemplatesExist:
    def test_settings_template_exists(self) -> None:
        assert (TEMPLATES_DIR / "settings.json").exists(), (
            "templates/settings.json must exist"
        )

    def test_claude_md_template_exists(self) -> None:
        assert (TEMPLATES_DIR / "CLAUDE.md").exists(), (
            "templates/CLAUDE.md must exist"
        )


class TestTemplatesAreParameterized:
    """Templates use $HOME, never a hardcoded home path."""

    def test_settings_has_no_hardcoded_home(self) -> None:
        content = (TEMPLATES_DIR / "settings.json").read_text()
        assert "/home/lucio.saldivar" not in content, (
            "templates/settings.json must not contain a hardcoded home path"
        )
        assert "$HOME" in content, (
            "templates/settings.json must use $HOME for home paths"
        )

    def test_claude_md_has_no_hardcoded_home(self) -> None:
        content = (TEMPLATES_DIR / "CLAUDE.md").read_text()
        assert "/home/lucio.saldivar" not in content, (
            "templates/CLAUDE.md must not contain a hardcoded home path"
        )

    def test_settings_is_valid_json_after_render(self, tmp_path: Path) -> None:
        """Rendered settings.json must parse as JSON."""
        env = {**os.environ, "HOME": str(tmp_path)}
        result = subprocess.run(
            ["envsubst", "$HOME"],
            input=(TEMPLATES_DIR / "settings.json").read_text(),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        parsed = json.loads(result.stdout)
        assert "hooks" in parsed, "rendered settings.json must have hooks key"


# ---------------------------------------------------------------------------
# Installer behavior
# ---------------------------------------------------------------------------


class TestInstallerBasic:
    def test_installer_script_exists_and_is_executable(self) -> None:
        assert INSTALLER.exists(), "scripts/install-harness-config.sh must exist"
        assert os.access(INSTALLER, os.X_OK), (
            "scripts/install-harness-config.sh must be executable"
        )

    def test_installer_writes_both_files(self, tmp_path: Path) -> None:
        """Installer writes settings.json and CLAUDE.md to the target dir."""
        env = {**os.environ, "HOME": str(tmp_path), "WRIT_INSTALL_TARGET": str(tmp_path)}
        subprocess.run([str(INSTALLER)], env=env, check=True, capture_output=True)
        assert (tmp_path / "settings.json").exists()
        assert (tmp_path / "CLAUDE.md").exists()

    def test_installer_substitutes_home_in_settings(self, tmp_path: Path) -> None:
        """$HOME placeholders in template must be replaced with the actual $HOME."""
        env = {**os.environ, "HOME": str(tmp_path), "WRIT_INSTALL_TARGET": str(tmp_path)}
        subprocess.run([str(INSTALLER)], env=env, check=True, capture_output=True)
        rendered = (tmp_path / "settings.json").read_text()
        assert str(tmp_path) in rendered, f"expected {tmp_path} to appear in rendered settings"
        assert "$HOME" not in rendered, "$HOME must be substituted, not left literal"


class TestInstallerBackup:
    def test_installer_backs_up_existing_files(self, tmp_path: Path) -> None:
        """Existing settings.json + CLAUDE.md get backed up with a timestamp suffix."""
        (tmp_path / "settings.json").write_text('{"old": true}')
        (tmp_path / "CLAUDE.md").write_text("# old content")
        env = {**os.environ, "HOME": str(tmp_path), "WRIT_INSTALL_TARGET": str(tmp_path)}
        subprocess.run([str(INSTALLER)], env=env, check=True, capture_output=True)
        backups = list(tmp_path.glob("settings.json.bak.*"))
        claude_backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
        assert len(backups) == 1, f"expected 1 settings backup, got {len(backups)}"
        assert len(claude_backups) == 1, f"expected 1 CLAUDE.md backup, got {len(claude_backups)}"
        assert backups[0].read_text() == '{"old": true}'


class TestInstallerIdempotent:
    def test_rerun_does_not_create_redundant_backup(self, tmp_path: Path) -> None:
        """When the target already matches the rendered template, no new backup is made."""
        env = {**os.environ, "HOME": str(tmp_path), "WRIT_INSTALL_TARGET": str(tmp_path)}
        subprocess.run([str(INSTALLER)], env=env, check=True, capture_output=True)
        first_run_backups = len(list(tmp_path.glob("*.bak.*")))
        subprocess.run([str(INSTALLER)], env=env, check=True, capture_output=True)
        second_run_backups = len(list(tmp_path.glob("*.bak.*")))
        assert second_run_backups == first_run_backups, (
            "idempotent re-run must not create additional backups"
        )


class TestInstallerDryRun:
    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        """--dry-run prints rendered content to stdout and does not modify the target."""
        env = {**os.environ, "HOME": str(tmp_path), "WRIT_INSTALL_TARGET": str(tmp_path)}
        result = subprocess.run(
            [str(INSTALLER), "--dry-run"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert not (tmp_path / "settings.json").exists()
        assert not (tmp_path / "CLAUDE.md").exists()
        assert "hooks" in result.stdout, "dry-run must print rendered settings to stdout"


class TestInstallerPreconditions:
    def test_installer_fails_cleanly_without_envsubst(self, tmp_path: Path) -> None:
        """If envsubst is unavailable on PATH, installer exits non-zero with a clear message."""
        # Build a PATH that excludes envsubst by pointing to an empty dir
        clean_bin = tmp_path / "bin"
        clean_bin.mkdir()
        # Include a minimal set of tools the script needs but NOT envsubst
        for tool in ["bash", "cp", "diff", "date", "mkdir", "cat", "printf"]:
            found = shutil.which(tool)
            if found:
                os.symlink(found, clean_bin / tool)
        env = {
            "HOME": str(tmp_path),
            "WRIT_INSTALL_TARGET": str(tmp_path),
            "PATH": str(clean_bin),
        }
        result = subprocess.run(
            [str(INSTALLER)], env=env, capture_output=True, text=True
        )
        assert result.returncode != 0, "installer must fail when envsubst is missing"
        combined = (result.stdout + result.stderr).lower()
        assert "envsubst" in combined, "error message must mention envsubst"
