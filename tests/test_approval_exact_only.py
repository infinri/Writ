"""Only the exact words the user chose may mint authority.

Two predicates hand out authority from the user's typed text: `approval_match.is_approval`
mints the gate token that advances a phase, and `manual_test_grant.is_grant_phrase` mints
the manual-testing grant that lets production files in without a test. Both were far wider
than the words a user would deliberately choose.

`is_approval` accepted a seventeen-member set including `ok`, `y`, `go`, `yes` and
`continue`, retried after stripping prefixes like `sure `, applied Levenshtein distance <= 2
to short prompts (so the typo `except` minted), and matched seven regex shapes including
`approved and <instruction>`. `is_grant_phrase` tested its twelve phrases with `in`, i.e.
substring containment, so any of them fired from inside a longer sentence.

THE ACCIDENT THIS MODULE EXISTS FOR: the user's message asking for this change quoted both
phrases as examples, and both hooks fired. One printed an approval directive; the other
minted a real thirty-minute manual-testing grant from a sentence that was discussing the
phrase, not using it. `test_phrase_inside_a_sentence_does_not_grant` is that incident
stated as an assertion.

Why the mint matters more than the claim: a token minted with no phase gate pending carries
an EMPTY gate line, and the empty-gate token is exactly what the promotion route accepts as
a canon-write credential. So a casual `ok` left a durable credential in /tmp. Narrowing the
mint removes that without touching the token mechanism.

The embedded ASK tier is deliberately NOT narrowed. It advances nothing and mints nothing,
so it costs no authority, and it is what keeps the tightening liveable: a near-miss gets a
question instead of silence.

Loading matches this suite's convention (tests/test_approval_tiers.py): bin/lib is put on
sys.path, and the predicates are imported at module scope because both already exist. Only
their behaviour changes here, so there is no ImportError to scope per-test.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin", "lib"))

from approval_match import classify, is_approval  # noqa: E402
from manual_test_grant import is_grant_phrase  # noqa: E402

# Every phrase that minted a token before this cycle and must not any more. Kept as data
# rather than deleted: a future widening has to argue with this list instead of slipping
# through an absence of coverage.
FORMER_EXACT_PHRASES = [
    "approve", "lgtm", "proceed", "go ahead", "looks good", "ship it",
    "yes", "yep", "y", "ok", "okay", "go", "do it", "continue",
    "accepted", "accept",
]

# Shapes the deleted passes accepted: prefix-stripping, Levenshtein <= 2, and the
# approval-plus-instruction regex.
FORMER_ACCEPTED_SHAPES = [
    "ok approved",
    "sure proceed",
    "yeah, approved",
    "yes, proceed",
    "except",            # levenshtein("except", "accept") == 2
    "aproved",           # single-character typo
    "approved and also fix the other thing",
    "approved, then run the suite",
    "phase-a approved",
]

# The eleven grant phrases this cycle removes, leaving only "manual test approved".
FORMER_GRANT_PHRASES = [
    "manual testing approved",
    "manual tests approved",
    "manual verification approved",
    "approve manual testing",
    "approved for manual testing",
    "i will test manually",
    "i'll test manually",
    "i will test it manually",
    "i'll test it manually",
    "i will test this manually",
    "i'll test this manually",
]


class TestTheOnlyMintingPhrase:
    def test_approved_mints(self):
        assert is_approval("approved") is True

    @pytest.mark.parametrize("prompt", ["approved.", "approved!", "approved..."])
    def test_trailing_punctuation_still_mints(self, prompt):
        """A user typing a full stop has not changed their mind."""
        assert is_approval(prompt) is True

    @pytest.mark.parametrize("prompt", ["  approved  ", "Approved", "APPROVED"])
    def test_surrounding_whitespace_and_case_still_mint(self, prompt):
        """The hook lowercases and strips before calling; do not regress on that contract."""
        assert is_approval(prompt.strip().lower()) is True


class TestFormerVocabularyNoLongerMints:
    @pytest.mark.parametrize("prompt", FORMER_EXACT_PHRASES)
    def test_former_exact_phrase_does_not_mint(self, prompt):
        assert is_approval(prompt) is False

    @pytest.mark.parametrize("prompt", FORMER_ACCEPTED_SHAPES)
    def test_former_accepted_shape_does_not_mint(self, prompt):
        assert is_approval(prompt) is False

    def test_the_typo_that_used_to_mint_does_not(self):
        """levenshtein("except", "accept") == 2, which the deleted fuzzy pass admitted."""
        assert is_approval("except") is False


class TestClassifyTiers:
    def test_approved_is_exact(self):
        assert classify("approved") == "exact"

    @pytest.mark.parametrize("prompt", ["ok", "yes", "y", "go", "continue"])
    def test_ordinary_acknowledgement_is_none(self, prompt):
        """"none" means the hook does nothing at all: no directive, no telemetry row."""
        assert classify(prompt) == "none"

    def test_embedded_tier_is_preserved(self):
        """The ASK tier mints nothing, so narrowing the mint must not narrow it."""
        assert classify("ok remember we want to fix all our findings, approved") == "embedded"

    def test_negated_approval_is_not_an_approval(self):
        assert classify("this is not approved") != "exact"

    def test_question_about_approval_is_not_an_approval(self):
        assert classify("is this approved?") != "exact"


class TestGrantPhrase:
    def test_the_only_grant_phrase_grants(self):
        assert is_grant_phrase("manual test approved") is True

    def test_phrase_inside_a_sentence_does_not_grant(self):
        """The reported accident, as an assertion.

        The message that triggered it was asking for this very change, quoting the phrase
        as an example. Substring containment could not tell the two apart.
        """
        prompt = (
            "one more thing for approval gate, the token should only happen when i "
            "specifically say approved or if no tests/manual tests i should say "
            "manual test approved nothing else"
        )
        assert is_grant_phrase(prompt) is False

    @pytest.mark.parametrize("prompt", FORMER_GRANT_PHRASES)
    def test_removed_grant_phrase_does_not_grant(self, prompt):
        assert is_grant_phrase(prompt) is False

    @pytest.mark.parametrize("prompt", [
        "  manual test approved  ",
        "Manual Test Approved",
        "manual  test  approved",
    ])
    def test_whitespace_and_case_normalisation_still_grants(self, prompt):
        """Normalisation is kept; only the containment test becomes an equality test."""
        assert is_grant_phrase(prompt) is True


class TestFailClosed:
    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_is_approval_fails_closed(self, bad):
        assert is_approval(bad) is False

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_is_grant_phrase_fails_closed(self, bad):
        assert is_grant_phrase(bad) is False

    def test_neither_predicate_raises_on_odd_input(self):
        """Both are documented as pure and fail-closed; a defect must degrade to False."""
        for weird in ["\x00", "approved\napproved", "a" * 5000, "🙂"]:
            assert is_approval(weird) in (True, False)
            assert is_grant_phrase(weird) in (True, False)
