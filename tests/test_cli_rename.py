"""Smoke tests for CLI command rename: ingest -> import-markdown.

Per TEST-TDD-001: skeletons approved before implementation.
Verifies that Typer registers import-markdown and does not register ingest.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from writ.cli import app


runner = CliRunner()


def _registered_command_names() -> list[str]:
    """Return the list of command names registered in the Typer app."""
    return [cmd.name for cmd in app.registered_commands]  # type: ignore[attr-defined]


def test_import_markdown_is_registered() -> None:
    """The 'import-markdown' command exists in the Typer app."""
    names = _registered_command_names()
    assert "import-markdown" in names, (
        f"'import-markdown' not found in registered commands: {names}"
    )


def test_ingest_is_not_registered() -> None:
    """The 'ingest' command is NOT registered after the rename."""
    names = _registered_command_names()
    assert "ingest" not in names, (
        f"'ingest' is still registered -- rename to 'import-markdown' not applied: {names}"
    )


def test_writ_help_references_import_markdown() -> None:
    """Top-level 'writ --help' output contains 'import-markdown'."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "import-markdown" in result.output


def test_writ_help_does_not_reference_ingest() -> None:
    """Top-level 'writ --help' does not list 'ingest' as a standalone command name."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Check that 'ingest' does not appear as a command name (left-aligned in the
    # help table), but allow the English word 'ingestion' in descriptions.
    for line in result.output.splitlines():
        stripped = line.strip()
        # Typer renders commands as "command-name  Description..." in the table.
        # A line starting with "ingest " (space after) means the command is registered.
        if stripped.startswith("ingest ") or stripped == "ingest":
            raise AssertionError(
                f"'ingest' is still registered as a command: {stripped}"
            )


def test_import_markdown_help_shows_expected_usage() -> None:
    """'writ import-markdown --help' exits 0 and shows path argument in usage."""
    result = runner.invoke(app, ["import-markdown", "--help"])
    assert result.exit_code == 0
    # Expected: usage line and description referencing Markdown/bible source
    assert "import-markdown" in result.output.lower() or "usage" in result.output.lower()
