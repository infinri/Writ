"""Tests for the approval detection module (bin/lib/approval_match.py).

Imports is_approval directly from the module (single source of truth).
The hook (auto-approve-gate.sh) delegates to the same module, so these
tests exercise the exact logic that runs in production.
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin", "lib"))

from approval_match import is_approval  # noqa: E402  # RED until module exists


def _check_approval(prompt: str) -> bool:
    """Thin wrapper so existing call-sites keep working unchanged."""
    prompt_lower = prompt.lower().strip()
    return is_approval(prompt_lower)


# -- Exact matches (existing behavior) ---------------------------------------

class TestOnlyApprovedMints:
    """One phrase mints, by user directive (2026-08-23).

    These three classes used to assert that `approve`, `lgtm`, `proceed`, `go ahead`,
    `yes`, `continue`, the `ok <approval>` prefix forms, and `approved and <instruction>`
    all minted a token. They no longer do. The old fixtures are kept here as NEGATIVE
    assertions rather than deleted, so re-widening the vocabulary has to argue with a named
    test instead of slipping through absent coverage.

    The authoritative contract, including the grant surface, lives in
    tests/test_approval_exact_only.py. This class exists so this module's own fixtures stay
    honest, not to duplicate that one.
    """

    def test_approved_returns_true(self):
        assert _check_approval("approved")

    @pytest.mark.parametrize("prompt", [
        # former exact set
        "approve", "lgtm", "proceed", "go ahead", "looks good", "ship it",
        "yes", "yep", "y", "ok", "okay", "go", "do it", "continue",
        "accepted", "accept",
        # former prefix forms
        "ok proceed with remaining work", "sure, go ahead", "yeah approved, continue",
        "okay proceed", "sure approved", "ok continue", "yeah go ahead",
        "yes proceed with that", "ok looks good",
        # former conjunction forms
        "approved and push", "approved, ship it", "approved then commit",
        "approve and merge",
    ])
    def test_former_vocabulary_no_longer_mints(self, prompt):
        assert not _check_approval(prompt), prompt


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
    """The exact tier is now one phrase wide, so it partitions differently.

    `approved` is exact. The prefix and conjunction forms that used to be exact are
    embedded: they carry a strong approval word inside a longer sentence, so the hook ASKS
    rather than advancing, which mints nothing and is why that tier was left alone.
    """

    def test_approved_is_exact(self):
        assert _tier("approved") == "exact"

    @pytest.mark.parametrize("prompt", [
        "ok proceed with remaining work",
        "approved and push",
    ])
    def test_former_exact_forms_are_no_longer_exact(self, prompt):
        assert _tier(prompt) != "exact", prompt


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
