"""The rendered half of finding 1: what the user is actually told, driven as a subprocess.

`tests/test_refusal_phrases_are_live.py` proves the SOURCE literal names a live phrase.
That is the producer. This module pins the CHAIN: it triggers each gate-state refusal as a
real subprocess through the harness every other drill module uses, lifts the quoted phrase
out of the `permissionDecisionReason` the hook EMITTED, and asks the real
`manual_test_grant.is_grant_phrase` whether that phrase would grant anything. A refusal
whose emitted reason names a phrase nothing mints from is a deadlock for whoever meets it,
and a test that read the source instead of the emitted reason would be checking a model of
the refusal rather than the artifact the agent receives.

ONE PARSER, NOT A SECOND COPY. The phrase is extracted with `tests._inventory
.user_directed_phrases`, the same grammar the source scan uses. Two copies is how the
source arm and the live arm come to disagree about what a directive IS, and this repo has
paid for a duplicated parser twice.

THE POPULATION IS DERIVED from the census's own `ask the user` marker owners rather than
named here, so a fourth gate-state refusal declared later is driven without anyone
remembering to add it.

RED TODAY, and the red is the deliverable: all three refusals emit `manual testing
approved`, which `is_grant_phrase` rejects.
"""
from __future__ import annotations

import os
import sys

import pytest

from tests._inventory import user_directed_phrases
from tests.firedrill._census import ACTION_MARKERS, by_id
from tests.firedrill._harness import make_isolation, run_hook

SKILL_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)
BIN_LIB = os.path.join(SKILL_ROOT, "bin", "lib")
if BIN_LIB not in sys.path:
    sys.path.insert(0, BIN_LIB)

# The marker whose owners are exactly the refusals that name the manual-testing bypass.
_GRANT_REFUSAL_MARKER = "ask the user"

# A phrase no predicate in this repository accepts, for the conditionality proof.
SENTINEL_DEAD_PHRASE = "quokka testing approved"


def _grant_phrase_predicate():
    """The real `is_grant_phrase`, or a loud failure naming it."""
    import manual_test_grant

    predicate = getattr(manual_test_grant, "is_grant_phrase", None)
    if not callable(predicate):
        pytest.fail(
            "bin/lib/manual_test_grant.py has no callable is_grant_phrase, so nothing "
            "here can say whether a refusal's phrase would grant anything",
            pytrace=False,
        )
    return predicate


def _grant_refusal_params() -> list:
    """One parameter per census refusal declared to name the bypass, never empty."""
    owners = ACTION_MARKERS.get(_GRANT_REFUSAL_MARKER) if isinstance(ACTION_MARKERS, dict) else None
    if not owners:
        return [
            pytest.param(
                "<no owner declared>",
                id="ask-the-user-marker-has-no-owner",
            )
        ]
    return [pytest.param(refusal_id, id=refusal_id) for refusal_id in owners]


def _emitted_reason(tmp_path, refusal_id: str) -> str:
    """Trigger `refusal_id` as a real subprocess and return the reason it emitted."""
    entry = by_id(refusal_id)
    iso = make_isolation(tmp_path / refusal_id, session_id=f"grant-phrase-{refusal_id}")
    setup = entry.setup(iso)
    result = run_hook(
        entry.script, setup["envelope"], iso,
        extra_env=setup.get("extra_env"), cwd=setup.get("cwd"),
    )
    assert result.permission_decision() == "deny", (
        f"{refusal_id}: expected a deny from {entry.script}; got "
        f"{result.permission_decision()!r}, stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    return result.permission_reason()


class TestEveryGateStateRefusalNamesALivePhrase:
    """Capability 4. Each of the gate-state refusals, triggered for real, must quote a
    phrase `is_grant_phrase` accepts.

    TRIAGE RULE FOR A RED HERE: the refusal names a bypass the user cannot reach, which is
    the deadlock this cycle exists to close. Fix the REFUSAL text. Widening GRANT_PHRASES
    to make this green undoes an explicit user directive and restores the accident where a
    message merely DISCUSSING the phrase minted a real grant.
    """

    @pytest.mark.parametrize("refusal_id", _grant_refusal_params())
    def test_the_emitted_reason_quotes_a_phrase_the_real_predicate_accepts(
        self, tmp_path, refusal_id
    ) -> None:
        assert not refusal_id.startswith("<"), (
            "the census declares no owner for the "
            f"{_GRANT_REFUSAL_MARKER!r} marker, so this class drives nothing"
        )
        reason = _emitted_reason(tmp_path, refusal_id)
        phrases = user_directed_phrases(reason)

        assert phrases, (
            f"{refusal_id}: the emitted reason tells the user to type nothing at all, so "
            f"it names no way out: {reason!r}"
        )
        predicate = _grant_phrase_predicate()
        dead = [phrase for phrase in phrases if not predicate(phrase)]
        assert not dead, (
            f"{refusal_id} tells the user to reply {dead!r}, and the real is_grant_phrase "
            "accepts none of those, so a user who follows the instruction stays blocked "
            f"forever. Full reason: {reason!r}"
        )


class TestTheWriteTargetStateArmIsReachable:
    """Capability 5. The command-text guard matches first for anything whose TEXT names a
    state pattern, so the write-TARGET arm was undrivable and therefore undeclared. The
    census entry resolves a bare relative filename inside the isolated cache dir, which
    names none of the ten text patterns.
    """

    def test_a_redirect_resolving_into_the_cache_dir_is_denied_by_the_target_arm(
        self, tmp_path
    ) -> None:
        reason = _emitted_reason(tmp_path, "bash-write-state-target")

        assert "[ENF-GATE-STATE]" in reason, (
            f"the write-target arm did not emit the gate-state refusal: {reason!r}"
        )
        assert "it writes to Writ gate state" in reason, (
            "the refusal that fired is not the write-TARGET arm. 'it names Writ gate "
            "state' is the command-TEXT guard, which runs first and would make this "
            f"entry a duplicate of bash-write-state rather than a new site: {reason!r}"
        )

    def test_the_command_text_names_no_state_pattern(self, tmp_path) -> None:
        """The other half of the trigger's contract, asserted on the command itself: if the
        text ever spells a state pattern, the guard above claims it and this entry silently
        stops exercising the arm it was declared for.
        """
        entry = by_id("bash-write-state-target")
        iso = make_isolation(tmp_path, session_id="grant-phrase-target-text")
        setup = entry.setup(iso)
        command = setup["envelope"]["tool_input"]["command"]

        assert str(iso.cache_dir) not in command, (
            f"the trigger command spells the cache dir, so the command-text guard claims "
            f"it before the target arm is reached: {command!r}"
        )
        assert setup.get("cwd"), (
            "the entry declares no cwd, so the relative path resolves against the project "
            "root and never reaches gate state at all"
        )


class TestTheLivePhraseCheckIsConditional:
    """Capability 2's live-arm counterpart. A check that answered 'live' for anything, or
    that never found a phrase at all, would pass the class above on any tree. Both halves
    are proved on hand-built reason strings, never by editing a hook.
    """

    def test_a_hand_built_reason_quoting_a_dead_phrase_is_reported(self) -> None:
        reason = (
            "[ENF-GATE-STATE] Refusing this write: a manual-testing bypass is minted only "
            f'from the user\'s own words, so ask the user to reply "{SENTINEL_DEAD_PHRASE}".'
        )
        phrases = user_directed_phrases(reason)

        assert phrases == [SENTINEL_DEAD_PHRASE], (
            f"the extractor did not lift the quoted phrase out of the reason: {phrases!r}"
        )
        assert _grant_phrase_predicate()(SENTINEL_DEAD_PHRASE) is False, (
            "the sentinel phrase grants, so this case proves nothing"
        )

    def test_a_hand_built_reason_quoting_the_live_phrase_passes(self) -> None:
        import manual_test_grant

        live = manual_test_grant.GRANT_PHRASES[0]
        reason = (
            "[ENF-GATE-STATE] Refusing this write: a manual-testing bypass is minted only "
            f'from the user\'s own words, so ask the user to reply "{live}".'
        )
        phrases = user_directed_phrases(reason)

        assert phrases == [live], f"the extractor lifted {phrases!r} instead of [{live!r}]"
        assert _grant_phrase_predicate()(live) is True

    def test_a_reason_naming_no_phrase_yields_nothing(self) -> None:
        assert user_directed_phrases("[ENF-GATE-STATE] Refusing this write.") == []
