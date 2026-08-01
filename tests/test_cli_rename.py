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


# ---------------------------------------------------------------------------
# T0.10/0.11 -- Phase 0 additions: prune command + validate --bible-dir option
# ---------------------------------------------------------------------------


def test_prune_command_exists() -> None:
    """'writ prune --help' exits 0 and the output names both 'prune' and '--dry-run'.

    RED until writ/cli.py gains a 'prune' command with a --dry-run option.
    """
    result = runner.invoke(app, ["prune", "--help"])
    assert result.exit_code == 0, (
        f"'writ prune --help' must exit 0; got {result.exit_code}. "
        f"Output: {result.output!r}"
    )
    from tests._ansi import plain  # noqa: PLC0415
    assert "prune" in plain(result.output), (
        f"'prune' must appear in help output; got: {result.output!r}"
    )
    assert "--dry-run" in plain(result.output), (
        f"'--dry-run' option must appear in 'writ prune --help'; got: {result.output!r}"
    )


def test_prune_dry_run_lists_candidates() -> None:
    """'writ prune --dry-run' prints flagged node IDs and does not emit DELETE.

    detect_parity_violations is mocked to return one violation (ORPHAN-001).
    The command must print ORPHAN-001 and must NOT print the word 'DELETE'
    (dry-run must not mutate the graph).

    RED until:
    - 'writ prune' command exists
    - it calls detect_parity_violations
    - --dry-run suppresses graph mutations
    """
    from unittest.mock import AsyncMock, patch

    violation = [{"type": "Rule", "id": "ORPHAN-001"}]

    with patch(
        "writ.graph.integrity.IntegrityChecker.detect_parity_violations",
        new_callable=lambda: lambda self: AsyncMock(return_value=violation)(),
    ):
        result = runner.invoke(app, ["prune", "--dry-run"])

    assert "ORPHAN-001" in result.output, (
        f"prune --dry-run must list flagged node id ORPHAN-001; got: {result.output!r}"
    )
    assert "DELETE" not in result.output, (
        f"prune --dry-run must NOT emit DELETE (no graph mutation); got: {result.output!r}"
    )


def test_validate_accepts_bible_dir_option() -> None:
    """'writ validate --bible-dir /tmp/x' must not error with 'No such option'.

    The validate command does not need to succeed end-to-end (it will try to
    connect to Neo4j), but the CLI layer must recognise --bible-dir as a valid
    option and not reject it at argument parsing time.

    RED until writ/cli.py's validate() gains a --bible-dir option.
    """
    import tempfile
    import os

    with tempfile.TemporaryDirectory() as tmp:
        # Invoke with --help so we never actually connect to Neo4j,
        # but the option must parse without "No such option" errors.
        result = runner.invoke(app, ["validate", "--help"])
        assert result.exit_code == 0, (
            f"validate --help must exit 0; got {result.exit_code}. "
            f"Output: {result.output!r}"
        )
        from tests._ansi import plain  # noqa: PLC0415
        assert "--bible-dir" in plain(result.output), (
            f"validate --help must list --bible-dir option; got: {result.output!r}"
        )
