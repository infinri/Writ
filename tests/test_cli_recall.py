"""Decision Memory Phase 2 RECALL: tests for `writ recall` CLI command.

Every test here is RED until the implementer adds `recall_cmd` to writ/cli.py.
Tests fail on SystemExit/AttributeError (command not found in the typer app) or
AttributeError, never on a collection/import error.

CRITICAL isolation guarantee: NO test in this file touches Neo4j. The db
is patched via _writ_db (the async context manager in writ/cli.py) and
compile_recall is patched at its import site so no Neo4j driver is ever
opened. Mirrors the pattern in test_query_cli.py (CliRunner + monkeypatch).

Run: .venv/bin/python -m pytest tests/test_cli_recall.py

Capability map:
  [cli-recall-1]  writ recall prints the briefing when decisions exist
  [cli-recall-2]  writ recall prints a "no decisions" message when the project
                  has no captured decisions
  [cli-recall-3]  writ recall --full prints each decision with its rule statements
  [cli-recall-4]  writ recall prints an explicit message when the project cannot
                  be resolved (isolation cycle v2, Part 3)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from writ.cli import app


# ---------------------------------------------------------------------------
# Factories (TEST-FIXTURE-001)
# ---------------------------------------------------------------------------

def _decision_factory(
    decision_id: str = "DEC-CLI-001",
    title: str = "Add recall CLI command",
    rationale: str = "Users need a recall command.",
    planned_files: list[dict] | None = None,
    governing_rule_ids: list[str] | None = None,
    rule_statements: dict[str, str] | None = None,
    phase: str = "planning",
    ts: str = "2026-06-27T10:00:00+00:00",
) -> dict:
    """Minimal well-formed decision dict (already compiled by compile_recall)."""
    return {
        "decision_id": decision_id,
        "title": title,
        "rationale": rationale,
        "planned_files": planned_files if planned_files is not None else [],
        "governing_rule_ids": governing_rule_ids if governing_rule_ids is not None else ["PERF-BATCH-001"],
        "rule_statements": rule_statements if rule_statements is not None else {
            "PERF-BATCH-001": "Batch all DB reads into one call."
        },
        "phase": phase,
        "ts": ts,
    }


# ---------------------------------------------------------------------------
# Patch helpers
#
# _writ_db is an asynccontextmanager in writ/cli.py. We replace it with one
# that yields a fake db; compile_recall is patched at its import site inside
# writ.cli (the function-level import "from writ.session.recall import
# compile_recall" means we patch writ.session.recall.compile_recall).
# ---------------------------------------------------------------------------

runner = CliRunner()


def _patch_recall(payload: dict):
    """Context manager that patches compile_recall to return `payload`."""
    async def _canned(*args, **kwargs):
        return payload

    return patch("writ.session.recall.compile_recall", new=_canned)


def _fake_writ_db(project: str = "writ"):
    """Return a fake _writ_db asynccontextmanager that yields a stub db."""

    class _FakeDB:
        async def resolve_project_for_cwd(self, cwd: str) -> str:
            return project

    @asynccontextmanager
    async def _ctx():
        yield _FakeDB()

    return _ctx


# ---------------------------------------------------------------------------
# Tests: [cli-recall-1] prints the briefing
# ---------------------------------------------------------------------------

class TestRecallCmdPrintsBriefing:
    """[cli-recall-1]: writ recall prints the briefing when decisions exist."""

    def test_briefing_printed_to_stdout(self) -> None:
        # [cli-recall-1]: when compile_recall returns a non-empty briefing the
        # command must echo it to stdout.
        # RED: recall_cmd does not exist yet (SystemExit / "No such command").
        payload = {
            "briefing": "[Writ recall: recent decisions]\n- Add recall module [PERF-BATCH-001]",
            "decisions": [_decision_factory()],
        }
        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall"])

        assert result.exit_code == 0, (
            f"writ recall must exit 0; got {result.exit_code}\n{result.output}"
        )
        assert "[Writ recall: recent decisions]" in result.output, (
            f"briefing header must appear in output; got:\n{result.output!r}"
        )
        assert "Add recall module" in result.output, (
            f"decision title must appear in briefing; got:\n{result.output!r}"
        )

    def test_rule_ids_in_briefing_output(self) -> None:
        # [cli-recall-1]: the briefing must include governing rule ids.
        # RED: SystemExit.
        payload = {
            "briefing": "[Writ recall]\n- My decision [ERR-FALLBACK-001]",
            "decisions": [_decision_factory(governing_rule_ids=["ERR-FALLBACK-001"])],
        }
        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall"])

        assert result.exit_code == 0, (
            f"exit code must be 0; got {result.exit_code}\n{result.output}"
        )
        assert "ERR-FALLBACK-001" in result.output, (
            f"rule id must appear in output; got:\n{result.output!r}"
        )


# ---------------------------------------------------------------------------
# Tests: [cli-recall-2] "no decisions" message when empty
# ---------------------------------------------------------------------------

class TestRecallCmdNoDecisionsMessage:
    """[cli-recall-2]: writ recall prints a 'no decisions' message when empty."""

    def test_no_decisions_message_when_briefing_empty(self) -> None:
        # [cli-recall-2]: when compile_recall returns briefing="" and decisions=[]
        # the command must print the "no decisions captured" message instead of
        # silently exiting or printing nothing.
        # RED: SystemExit.
        payload = {"briefing": "", "decisions": []}

        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall"])

        assert result.exit_code == 0, (
            f"writ recall must exit 0 even with no decisions; got {result.exit_code}\n{result.output}"
        )
        # Must print SOMETHING informative -- not silently empty.
        assert len(result.output.strip()) > 0, (
            "writ recall must print a message when no decisions exist, not be silent"
        )
        # The message must indicate no decisions (not print an empty briefing block).
        lower_out = result.output.lower()
        assert "no decision" in lower_out or "nothing" in lower_out or "no decisions" in lower_out, (
            f"output must mention 'no decisions' when project has none; got:\n{result.output!r}"
        )

    def test_no_decisions_message_is_not_an_error(self) -> None:
        # [cli-recall-2]: the "no decisions" state is normal (new project),
        # not an error -- exit code must be 0.
        # RED: SystemExit.
        payload = {"briefing": "", "decisions": []}

        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall"])

        assert result.exit_code == 0, (
            f"'no decisions' must exit 0 (not an error); got {result.exit_code}"
        )


# ---------------------------------------------------------------------------
# Tests: [cli-recall-3] --full prints decisions with rule statements
# ---------------------------------------------------------------------------

class TestRecallCmdFullFlag:
    """[cli-recall-3]: writ recall --full prints each decision with rule statements."""

    def test_full_flag_prints_decision_title_and_id(self) -> None:
        # [cli-recall-3]: --full must include each decision's title and decision_id.
        # RED: SystemExit.
        decision = _decision_factory(
            decision_id="DEC-FULL-001",
            title="Implement recall route",
            rule_statements={"PERF-BATCH-001": "Batch all DB reads."},
        )
        payload = {
            "briefing": "[Writ recall]\n- Implement recall route [PERF-BATCH-001]",
            "decisions": [decision],
        }

        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall", "--full"])

        assert result.exit_code == 0, (
            f"writ recall --full must exit 0; got {result.exit_code}\n{result.output}"
        )
        assert "Implement recall route" in result.output, (
            f"--full must print the decision title; got:\n{result.output!r}"
        )
        assert "DEC-FULL-001" in result.output, (
            f"--full must print the decision_id; got:\n{result.output!r}"
        )

    def test_full_flag_prints_rule_statements(self) -> None:
        # [cli-recall-3]: --full must print each rule's statement text so the
        # user can see the rationale behind the rule-grounding.
        # RED: SystemExit.
        decision = _decision_factory(
            decision_id="DEC-RULES-001",
            title="Rule-grounded decision",
            governing_rule_ids=["ERR-FALLBACK-001"],
            rule_statements={"ERR-FALLBACK-001": "All error paths must be fail-open."},
        )
        payload = {
            "briefing": "[Writ recall]\n- Rule-grounded decision [ERR-FALLBACK-001]",
            "decisions": [decision],
        }

        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall", "--full"])

        assert result.exit_code == 0, (
            f"exit code must be 0; got {result.exit_code}\n{result.output}"
        )
        assert "ERR-FALLBACK-001" in result.output, (
            f"--full must print the rule id; got:\n{result.output!r}"
        )
        assert "All error paths must be fail-open" in result.output, (
            f"--full must print the rule statement text; got:\n{result.output!r}"
        )

    def test_full_flag_prints_multiple_decisions(self) -> None:
        # [cli-recall-3]: --full must print ALL kept decisions, not just the first.
        # RED: SystemExit.
        decisions = [
            _decision_factory(decision_id="DEC-MULTI-A", title="Decision Alpha"),
            _decision_factory(decision_id="DEC-MULTI-B", title="Decision Beta"),
        ]
        payload = {
            "briefing": "[Writ recall]\n- Decision Alpha\n- Decision Beta",
            "decisions": decisions,
        }

        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall", "--full"])

        assert result.exit_code == 0
        assert "Decision Alpha" in result.output, (
            f"--full must print first decision title; got:\n{result.output!r}"
        )
        assert "Decision Beta" in result.output, (
            f"--full must print second decision title; got:\n{result.output!r}"
        )

    def test_no_full_flag_does_not_print_rule_statements(self) -> None:
        # [cli-recall-3] contrast: without --full, rule statement body text must
        # NOT appear in the output (the briefing is compact -- rule ids only).
        # RED: SystemExit.
        decision = _decision_factory(
            decision_id="DEC-NOFULL-001",
            title="Compact briefing test",
            governing_rule_ids=["RULE-BRIEF-001"],
            rule_statements={"RULE-BRIEF-001": "This full statement text must not appear."},
        )
        payload = {
            "briefing": "[Writ recall]\n- Compact briefing test [RULE-BRIEF-001]",
            "decisions": [decision],
        }

        with patch("writ.cli._writ_db", new=_fake_writ_db()):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall"])  # no --full

        assert result.exit_code == 0
        assert "This full statement text must not appear" not in result.output, (
            f"without --full, rule statement body must not be printed; "
            f"got:\n{result.output!r}"
        )


# ---------------------------------------------------------------------------
# Tests: [cli-recall-4] unresolved project (isolation cycle v2, Part 3)
# ---------------------------------------------------------------------------

class TestRecallCmdUnresolvedProject:
    """[cli-recall-4]: resolve_project_for_cwd's default no longer returns
    "writ" for an unregistered repo -- it returns an empty string. Capability
    29's CLI half: "the CLI [degrades] to an explicit message" rather than
    silently querying for the empty-string project (which would either error
    or, worse, coincide with a real project literally named "").
    """

    def test_unresolved_project_prints_an_explicit_message(self) -> None:
        with patch("writ.cli._writ_db", new=_fake_writ_db(project="")):
            with _patch_recall({"briefing": "should not be reached", "decisions": []}):
                result = runner.invoke(app, ["recall"])

        assert result.exit_code == 0, (
            f"an unresolved project must not be treated as a CLI error; "
            f"got {result.exit_code}\n{result.output}"
        )
        lower_out = result.output.lower()
        assert "not registered" in lower_out or "no project" in lower_out or "unregistered" in lower_out, (
            f"output must explicitly say the project could not be resolved; "
            f"got:\n{result.output!r}"
        )

    def test_unresolved_project_never_calls_compile_recall(self) -> None:
        """The empty-string project must not reach compile_recall at all --
        querying decisions for project="" would either 404 or, worse, match
        every mis-derived record filed under no project."""
        compile_mock = AsyncMock()
        with patch("writ.cli._writ_db", new=_fake_writ_db(project="")):
            with patch("writ.session.recall.compile_recall", new=compile_mock):
                runner.invoke(app, ["recall"])

        compile_mock.assert_not_called()

    def test_a_resolved_project_is_unaffected_by_this_guard(self) -> None:
        """Anti-vacuity: the explicit-message path must not swallow the normal
        success case too."""
        payload = {"briefing": "[Writ recall]\n- ok", "decisions": []}
        with patch("writ.cli._writ_db", new=_fake_writ_db(project="proj-a")):
            with _patch_recall(payload):
                result = runner.invoke(app, ["recall"])

        assert "[Writ recall]" in result.output
