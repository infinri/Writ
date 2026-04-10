"""Tests for approval pattern detection in auto-approve-gate.sh.

Validates the approval detector by running the same Python logic the hook uses.
The detector source is extracted from auto-approve-gate.sh at test time so tests
always match the current hook implementation.
"""

import re
import subprocess
import pytest


def _extract_detector_source() -> str:
    """Extract the approval detector Python source from auto-approve-gate.sh."""
    with open(".claude/hooks/auto-approve-gate.sh") as f:
        content = f.read()
    # The detector is between IS_APPROVAL=$(python3 -c " and " "$PROMPT_LOWER"
    match = re.search(
        r'IS_APPROVAL=\$\(python3 -c "(.*?)" "\$PROMPT_LOWER"',
        content,
        re.DOTALL,
    )
    assert match, "Could not extract approval detector from auto-approve-gate.sh"
    return match.group(1)


_DETECTOR_SOURCE = _extract_detector_source()


def _check_approval(prompt: str) -> bool:
    """Run the approval detector against a prompt."""
    prompt_lower = prompt.lower().strip()
    result = subprocess.run(
        ["python3", "-c", _DETECTOR_SOURCE, prompt_lower],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip() == "yes"


# -- Exact matches (existing behavior) ---------------------------------------

class TestExactMatches:
    def test_approved(self):
        assert _check_approval("approved")

    def test_approve(self):
        assert _check_approval("approve")

    def test_lgtm(self):
        assert _check_approval("lgtm")

    def test_proceed(self):
        assert _check_approval("proceed")

    def test_go_ahead(self):
        assert _check_approval("go ahead")

    def test_yes(self):
        assert _check_approval("yes")

    def test_continue(self):
        assert _check_approval("continue")

    def test_trailing_punctuation(self):
        assert _check_approval("approved!")

    def test_trailing_period(self):
        assert _check_approval("approved.")


# -- Prefix-tolerant patterns (new behavior) ----------------------------------

class TestPrefixPatterns:
    def test_ok_proceed_with_remaining_work(self):
        """Friction log line 5: this exact phrase was missed."""
        assert _check_approval("ok proceed with remaining work")

    def test_sure_go_ahead(self):
        assert _check_approval("sure, go ahead")

    def test_yeah_approved_continue(self):
        assert _check_approval("yeah approved, continue with implementation")

    def test_okay_proceed(self):
        assert _check_approval("okay proceed")

    def test_sure_approved(self):
        assert _check_approval("sure, approved")

    def test_ok_continue(self):
        assert _check_approval("ok continue")

    def test_yeah_go_ahead(self):
        assert _check_approval("yeah go ahead")

    def test_yes_proceed_with_that(self):
        assert _check_approval("yes proceed with that")

    def test_ok_looks_good(self):
        assert _check_approval("ok looks good")


# -- Non-approval: must NOT match ---------------------------------------------

class TestNonApproval:
    def test_question_about_approval(self):
        assert not _check_approval("how do I get this approved?")

    def test_code_with_approval_word(self):
        assert not _check_approval("the proceed function needs to handle errors")

    def test_discussing_continue(self):
        assert not _check_approval("add a continue statement in the loop")

    def test_empty_string(self):
        assert not _check_approval("")

    def test_unrelated_prompt(self):
        assert not _check_approval("refactor the database module")

    def test_question_with_ok(self):
        assert not _check_approval("is it ok to delete the old migration files?")

    def test_go_in_sentence(self):
        assert not _check_approval("where does this function go in the architecture?")
