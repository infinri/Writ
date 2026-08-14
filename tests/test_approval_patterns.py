"""Tests for the approval detection module (bin/lib/approval_match.py).

Imports is_approval directly from the module (single source of truth).
The hook (auto-approve-gate.sh) delegates to the same module, so these
tests exercise the exact logic that runs in production.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin", "lib"))

from approval_match import is_approval  # noqa: E402  # RED until module exists


def _check_approval(prompt: str) -> bool:
    """Thin wrapper so existing call-sites keep working unchanged."""
    prompt_lower = prompt.lower().strip()
    return is_approval(prompt_lower)


# -- Exact matches (existing behavior) ---------------------------------------

class TestExactMatches:
    def test_approved_returns_true(self):
        assert _check_approval("approved")

    def test_approve_returns_true(self):
        assert _check_approval("approve")

    def test_lgtm_returns_true(self):
        assert _check_approval("lgtm")

    def test_proceed_returns_true(self):
        assert _check_approval("proceed")

    def test_go_ahead_returns_true(self):
        assert _check_approval("go ahead")

    def test_yes_returns_true(self):
        assert _check_approval("yes")

    def test_continue_returns_true(self):
        assert _check_approval("continue")

    def test_trailing_exclamation_approved_returns_true(self):
        assert _check_approval("approved!")

    def test_trailing_period_approved_returns_true(self):
        assert _check_approval("approved.")


# -- Prefix-tolerant patterns (existing behavior) ----------------------------

class TestPrefixPatterns:
    def test_ok_proceed_with_remaining_work_returns_true(self):
        """Friction log line 5: this exact phrase was missed."""
        assert _check_approval("ok proceed with remaining work")

    def test_sure_go_ahead_returns_true(self):
        assert _check_approval("sure, go ahead")

    def test_yeah_approved_continue_returns_true(self):
        assert _check_approval("yeah approved, continue with implementation")

    def test_okay_proceed_returns_true(self):
        assert _check_approval("okay proceed")

    def test_sure_approved_returns_true(self):
        assert _check_approval("sure, approved")

    def test_ok_continue_returns_true(self):
        assert _check_approval("ok continue")

    def test_yeah_go_ahead_returns_true(self):
        assert _check_approval("yeah go ahead")

    def test_yes_proceed_with_that_returns_true(self):
        assert _check_approval("yes proceed with that")

    def test_ok_looks_good_returns_true(self):
        assert _check_approval("ok looks good")


# -- New conjunction/comma pattern: accept tests (RED until pattern added) ---

class TestConjunctionPattern:
    def test_approved_and_push_returns_true(self):
        """Approval word + 'and' + short instruction -> accepted."""
        assert _check_approval("approved and push")

    def test_approved_comma_ship_it_returns_true(self):
        """Approval word + comma + short instruction -> accepted."""
        assert _check_approval("approved, ship it")

    def test_approved_then_commit_returns_true(self):
        """Approval word + 'then' + short instruction -> accepted."""
        assert _check_approval("approved then commit")

    def test_approve_and_merge_returns_true(self):
        """Approval word + 'and' + short instruction -> accepted."""
        assert _check_approval("approve and merge")


# -- Non-approval: must NOT match (existing + new governance guards) ----------

class TestNonApproval:
    def test_question_about_approval_returns_false(self):
        assert not _check_approval("how do I get this approved?")

    def test_code_with_approval_word_returns_false(self):
        assert not _check_approval("the proceed function needs to handle errors")

    def test_discussing_continue_returns_false(self):
        assert not _check_approval("add a continue statement in the loop")

    def test_empty_string_returns_false(self):
        assert not _check_approval("")

    def test_unrelated_prompt_returns_false(self):
        assert not _check_approval("refactor the database module")

    def test_question_with_ok_returns_false(self):
        assert not _check_approval("is it ok to delete the old migration files?")

    def test_go_in_sentence_returns_false(self):
        assert not _check_approval("where does this function go in the architecture?")

    def test_approve_the_design_before_merging_returns_false(self):
        """No conjunction/comma immediately after the approval word -> rejected."""
        assert not _check_approval("approve the design before merging")

    def test_approved_changes_need_review_returns_false(self):
        """No conjunction/comma immediately after the approval word -> rejected."""
        assert not _check_approval("approved changes need review")

    def test_is_this_approved_question_returns_false(self):
        """Fails ^ anchor (starts with 'is') and contains '?' -> rejected."""
        assert not _check_approval("is this approved?")

    def test_not_approved_returns_false(self):
        """Fails ^ anchor (starts with 'not') -> rejected."""
        assert not _check_approval("not approved")

    def test_how_do_i_get_this_approved_returns_false(self):
        """Fails ^ anchor (starts with 'how') -> rejected."""
        assert not _check_approval("how do I get this approved?")


# -- Hook/module agreement test ----------------------------------------------

class TestHookModuleAgreement:
    def test_hook_references_approval_match_module(self):
        """auto-approve-gate.sh must reference approval_match so hook and module
        are provably the same source of truth (no inline-regex regression)."""
        hook_path = os.path.join(
            os.path.dirname(__file__), "..", "hooks", "scripts", "auto-approve-gate.sh"
        )
        with open(hook_path) as f:
            content = f.read()
        assert "approval_match" in content, (
            "auto-approve-gate.sh does not reference approval_match; "
            "the hook may still be using the old inline detector instead of the module."
        )


# =============================================================================
# Cycle 1 (plan.md): embedded-tier cases, added beside the exact-tier ones
# above. No assertion above this line changes. classify() is the three-way
# replacement for the bare is_approval() boolean (bin/lib/approval_match.py);
# see tests/test_approval_tiers.py for the full tier matrix (the missed
# 2026-08-10 prompt, the deleted substring-scan misses, the negation/question/
# interrogative guards, and the is_approval-unchanged regression). This section
# stays structurally parallel to TestExactMatches/TestPrefixPatterns/
# TestNonApproval above so a reader comparing the two tiers side by side in one
# file sees the same shape twice, not two different test styles.
#
# RED today: classify does not exist on approval_match.py. _tier() imports it
# LOCALLY (not at module scope): a module-scope import would fail COLLECTION for this
# entire file, silently blocking every pre-existing exact-tier test above from running
# at all -- exactly the "no existing assertion changes" contract this section must not
# violate. Scoping the import to _tier() means only the NEW tests below fail (cleanly,
# on ImportError), and every test above keeps running exactly as it does today.
# =============================================================================


def _tier(prompt: str) -> str:
    """classify()'s counterpart to _check_approval above: same lower+strip
    normalization the hook applies before either function sees the prompt."""
    from approval_match import classify

    return classify(prompt.lower().strip())


class TestEmbeddedMatches:
    """Structural counterpart to TestExactMatches: a strong approval word present
    as a standalone token, in a prompt that is not itself an exact match."""

    def test_approved_buried_in_a_longer_sentence_is_embedded(self):
        assert not _check_approval("so i think that is approved, one more thing though")
        assert _tier("so i think that is approved, one more thing though") == "embedded"

    def test_ship_it_buried_in_a_longer_sentence_is_embedded(self):
        assert not _check_approval("ship it once the tests pass")
        assert _tier("ship it once the tests pass") == "embedded"

    def test_lgtm_buried_in_a_longer_sentence_is_embedded(self):
        assert not _check_approval("lgtm just double check the migration")
        assert _tier("lgtm just double check the migration") == "embedded"


class TestExactTierClassifiesAsExact:
    """Structural counterpart to TestPrefixPatterns: every existing exact-match
    fixture must classify as 'exact', not 'embedded' -- the tiers partition the
    same prompt space TestExactMatches/TestPrefixPatterns already cover."""

    def test_approved_is_exact(self):
        assert _tier("approved") == "exact"

    def test_ok_proceed_with_remaining_work_is_exact(self):
        assert _tier("ok proceed with remaining work") == "exact"

    def test_approved_and_push_is_exact(self):
        assert _tier("approved and push") == "exact"


class TestEmbeddedGuardsAgreeWithNonApproval:
    """Structural counterpart to TestNonApproval: every existing false case must
    ALSO classify as 'none', not 'embedded' -- introducing the embedded tier must
    not turn a prompt is_approval already rejects into a gate-confirmation prompt."""

    def test_question_about_approval_is_none(self):
        assert _tier("how do I get this approved?") == "none"

    def test_is_this_approved_question_is_none(self):
        assert _tier("is this approved?") == "none"

    def test_not_approved_is_none(self):
        assert _tier("not approved") == "none"

    def test_how_do_i_get_this_approved_is_none(self):
        assert _tier("how do I get this approved?") == "none"

    def test_unrelated_prompt_is_none(self):
        assert _tier("refactor the database module") == "none"
