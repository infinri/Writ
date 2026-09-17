"""The test-skeletons gate, which was measured to be unable to refuse.

Finding 2 of plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, measured:
`_validate_test_skeletons(".", "no-such-session-xyz-12345")` returns None. The validator
checks `files_written` from the session cache and then, UNCONDITIONALLY, globs the whole
project for anything that looks like a test file with a test method, so on any repo with
one pre-existing test it is a rubber stamp. It was run as a pre-flight before four
approvals in one session and reported as protection.

WHAT THE GATE SHOULD REQUIRE WHEN A SESSION ID IS SUPPLIED: the test files the APPROVED
PLAN names. The plan is the artifact the user approved and `_find_plan_md(project_root,
session_id or None)` is the exact resolution phase-a used, so the two gates judge the SAME
plan rather than two different ideas of one.

THE FOURTH ARM IS WHAT KEEPS THE REFUSAL HONEST. A live manual-testing grant passes the
gate, because without it a docs-only or config-only cycle has no way out at all, and a
refusal that names no way out is finding 1's defect reproduced inside finding 2's fix.

RED TODAY, and the red is the deliverable: every refusal case below returns None, because
the whole-project glob answers for a session it was never scoped to.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BIN_LIB = os.path.join(SKILL_ROOT, "bin", "lib")
if BIN_LIB not in sys.path:
    sys.path.insert(0, BIN_LIB)

from tests._inventory import user_directed_phrases  # noqa: E402

# This module asserts that ONE friction row was written and what it says, so it must opt out
# of the autouse WRIT_FRICTION_LOG redirect: that variable collapses every typed stream into
# one file, and read_streams (what the assertion reads back with) does not honour it. The
# replacement isolation is per-test, exactly as tests/test_approval_evidence.py does it.
pytestmark = pytest.mark.no_friction_isolation

_TEST_BODY = "def test_something():\n    assert True\n"


def _sid(label: str) -> str:
    return f"skelgate-{label}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A project root that ALREADY carries a passing test module, plus an isolated cache.

    The pre-existing test is the whole point: it is what the measured whole-project glob
    answers with, so a gate that still passes here is passing on somebody else's work.
    """
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_pre_existing.py").write_text(_TEST_BODY)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(cache_dir))
    return root


def _write_plan(root: Path, session_id: str, files_section: str) -> Path:
    """The session-scoped plan.md, at the path `_find_plan_md` resolves for this session."""
    plan_dir = root / ".claude" / "plans" / session_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_dir / "plan.md"
    plan.write_text(
        "# Plan\n\n## Files\n\n"
        + files_section
        + "\n\n## Analysis\n\nwhy\n\n## Rules Applied\n\nNo matching rules.\n\n"
        "## Capabilities\n\n- [ ] does the thing\n"
    )
    return plan


def _validate(root: Path, session_id: str):
    from writ.session.approval_workflow import _validate_test_skeletons

    return _validate_test_skeletons(str(root), session_id)


def _predicates():
    import approval_match
    import manual_test_grant

    return {
        "is_approval": approval_match.is_approval,
        "is_replan_request": approval_match.is_replan_request,
        "is_override": approval_match.is_override,
        "is_grant_phrase": manual_test_grant.is_grant_phrase,
    }


class TestASessionIdThatNeverExistedIsRefused:
    """Capability 6, the measured case. No cache, no grant, no plan under that session.

    This is the assertion the pre-flight should have been making: it reported protection
    for a session that never existed, on a repo carrying a pre-existing test.
    """

    def test_an_unknown_session_is_refused_on_a_repo_full_of_tests(self, repo) -> None:
        err = _validate(repo, "no-such-session-xyz-12345")

        assert err is not None, (
            "a session id that never existed passed the test-skeletons gate on a repo "
            "whose only test file was written by somebody else; the gate cannot refuse"
        )

    def test_the_refusal_names_the_plan_it_could_not_resolve(self, repo) -> None:
        err = _validate(repo, "no-such-session-xyz-12345")

        assert err and "plan" in err.lower(), (
            "the refusal must name the artifact it judged, because the user cannot act on "
            f"a refusal that does not say what was missing: {err!r}"
        )


class TestThePlanNamedTestFileMustBeOnDisk:
    """Capability 7. The plan is the artifact the user approved, so the paths it names are
    what this gate requires, and the refusal names the path that is missing."""

    def test_a_planned_test_file_that_does_not_exist_is_refused_by_name(self, repo) -> None:
        sid = _sid("absent")
        _write_plan(repo, sid, "- `tests/test_thing.py` (create) -- the new skeleton")

        err = _validate(repo, sid)

        assert err is not None, (
            "the plan names a test file that is not on disk and the gate passed anyway, "
            "which is the pre-existing test module answering for work nobody did"
        )
        assert "tests/test_thing.py" in err, (
            f"the refusal must name the missing path so the user can act on it: {err!r}"
        )

    def test_a_planned_test_file_with_no_test_method_is_refused_by_name(self, repo) -> None:
        """An empty file at the right path is not a skeleton. The gate's stated job is a
        test METHOD signature, and a path check alone would approve a touch."""
        sid = _sid("empty")
        (repo / "tests" / "test_thing.py").write_text("# nothing here yet\n")
        _write_plan(repo, sid, "- `tests/test_thing.py` (create) -- the new skeleton")

        err = _validate(repo, sid)

        assert err is not None, (
            "a planned test file carrying no test method passed the gate"
        )
        assert "tests/test_thing.py" in err, (
            f"the refusal must name the path that carries no test method: {err!r}"
        )


class TestAPlanThatNamesNoTestFileIsRefusedWithLiveWaysOut:
    """Capability 8. A docs-only or config-only cycle is refused, and the refusal names two
    ways out that BOTH work today.

    THE WAYS OUT ARE JUDGED BY THE REAL PREDICATES, never by comparing the message against
    a copy of a phrase list. This is the cross-check that finding 2's own refusal did not
    repeat finding 1's defect in the same cycle that fixes it.
    """

    def _refusal(self, repo) -> str:
        sid = _sid("notests")
        _write_plan(
            repo, sid,
            "- `docs/guide.md` (modify) -- a prose change\n"
            "- `writ/session/thing.py` (modify) -- a config constant",
        )
        err = _validate(repo, sid)
        assert err is not None, (
            "a plan that names no test file at all passed the test-skeletons gate"
        )
        return err

    def test_a_plan_with_no_test_file_is_refused(self, repo) -> None:
        assert self._refusal(repo)

    def test_the_refusal_quotes_phrases_the_real_predicates_accept(self, repo) -> None:
        err = self._refusal(repo)
        phrases = user_directed_phrases(err)

        assert phrases, (
            f"the refusal tells the user to type nothing, so it names no way out: {err!r}"
        )
        predicates = _predicates()
        dead = [
            phrase for phrase in phrases
            if not any(predicate(phrase) for predicate in predicates.values())
        ]
        assert not dead, (
            f"the refusal tells the user to reply {dead!r}, which no live minting "
            f"predicate accepts, so this gate names a way out that does not exist: {err!r}"
        )

    def test_both_ways_out_are_named_and_each_is_live(self, repo) -> None:
        """Two ways out, not one: re-open planning to add the test file, or concede hand
        verification. Each is checked against the predicate that actually fires for it."""
        err = self._refusal(repo)
        phrases = user_directed_phrases(err)
        predicates = _predicates()

        assert any(predicates["is_replan_request"](p) for p in phrases), (
            "the refusal names no way to re-open planning and add the test file to "
            f"## Files; the phrases it quotes are {phrases!r}"
        )
        assert any(predicates["is_grant_phrase"](p) for p in phrases), (
            "the refusal names no way for the user to concede hand verification, so a "
            f"cycle with no runnable tests is deadlocked; it quotes {phrases!r}"
        )


class TestAPlanThatNamesAnExistingTestModulePasses:
    """Capability 9. Several cycles modify `tests/test_*.py` rather than creating one, and
    a `(modify)` bullet satisfies this arm the same way a `(create)` bullet does."""

    def test_a_modify_bullet_naming_an_existing_test_module_passes(self, repo) -> None:
        sid = _sid("modify")
        _write_plan(
            repo, sid,
            "- `tests/test_pre_existing.py` (modify) -- one more class\n"
            "- `writ/session/thing.py` (modify) -- the production change",
        )

        assert _validate(repo, sid) is None, (
            "a cycle whose plan names an EXISTING test module carrying test methods was "
            "refused, which makes every modify-rather-than-create cycle un-approvable"
        )


class TestTheSessionCacheArmIsUnchanged:
    """Capability 10. `files_written` was already session-scoped, so it was never the
    defect and must keep passing byte for byte."""

    def test_a_test_file_written_this_session_passes(self, repo) -> None:
        import json

        sid = _sid("written")
        written = repo / "tests" / "test_written_now.py"
        written.write_text(_TEST_BODY)
        cache_path = Path(os.environ["WRIT_CACHE_DIR"]) / f"writ-session-{sid}.json"
        cache_path.write_text(json.dumps({"files_written": [str(written)]}))

        assert _validate(repo, sid) is None, (
            "a test file written during this session no longer passes the gate"
        )


class TestALiveManualTestingGrantPasses:
    """Capability 11. The grant is minted only from the user's own typed words, expires in
    thirty minutes, and already admits arbitrary production files at the test-first gate,
    which is strictly wider authority than advancing this one gate. Honoring it here
    concedes nothing new, and it is what keeps the refusal above from being a deadlock.
    """

    def _mint(self, session_id: str):
        import manual_test_grant

        grant = manual_test_grant.mint(session_id, manual_test_grant.GRANT_PHRASES[0])
        assert grant is not None, "the real minter refused its own declared grant phrase"
        assert manual_test_grant.active(session_id) is not None, (
            "the grant was written but reads back as inactive, so this fixture proves "
            "nothing about the arm under test"
        )
        return grant

    def test_a_live_grant_passes_a_cycle_whose_plan_names_no_test_file(self, repo) -> None:
        sid = _sid("granted")
        _write_plan(repo, sid, "- `docs/guide.md` (modify) -- a prose change")
        self._mint(sid)

        assert _validate(repo, sid) is None, (
            "a live manual-testing grant did not pass the test-skeletons gate, so a "
            "docs-only cycle has no way out and the refusal above is a deadlock"
        )

    def test_one_friction_row_records_that_the_gate_passed_by_grant(
        self, repo, monkeypatch
    ) -> None:
        from writ.shared.logging import read_streams

        sid = _sid("grantrow")
        project = f"skelgate-{uuid.uuid4().hex[:8]}"
        monkeypatch.setenv("WRIT_LOG_PROJECT", project)
        _write_plan(repo, sid, "- `docs/guide.md` (modify) -- a prose change")
        self._mint(sid)

        assert _validate(repo, sid) is None

        rows = [
            row for row in read_streams(project, ["friction", "audit"])
            if row.get("session") == sid
        ]
        assert len(rows) == 1, (
            "passing a gate on the user's concession is a governance event and must leave "
            f"exactly one row saying so; got {rows!r}"
        )
        blob = repr(rows[0]).lower()
        assert "grant" in blob, (
            f"the row does not record that the gate passed by GRANT: {rows[0]!r}"
        )
        assert "test-skeletons" in blob, (
            f"the row does not name the gate it let through: {rows[0]!r}"
        )


class TestTheNoSessionTierIsUnchanged:
    """Capability 12. That tier has no session to scope to, so its behavior is not a defect
    there, and every session-less caller keeps working."""

    def test_no_session_id_still_passes_on_a_populated_repo(self, repo) -> None:
        assert _validate(repo, "") is None, (
            "the project-wide tier changed behavior for a caller that supplies no session "
            "id, which is not this cycle's defect and breaks every such caller"
        )


class TestTheFirstEverCycleIsNotADeadlock:
    """The failure direction, asserted rather than argued: a gate that refuses legitimate
    work is worse than the rubber stamp it replaces.

    The plan names `tests/test_thing.py` (create). Until that file exists with a test
    method the gate REFUSES and names the path; writing it clears the gate; and writing it
    is never blocked, because `tests/` is in the gate-category exclusions. The last half is
    driven through the REAL write gate, not read off the exclusions list, because the list
    is the producer and `_can_write_check` is the consumer.
    """

    def test_writing_the_planned_test_file_clears_the_refusal(self, repo) -> None:
        sid = _sid("firstever")
        _write_plan(repo, sid, "- `tests/test_thing.py` (create) -- the new skeleton")
        planned = repo / "tests" / "test_thing.py"

        assert _validate(repo, sid) is not None, "the absent skeleton was not refused"

        planned.write_text(_TEST_BODY)

        assert _validate(repo, sid) is None, (
            "writing exactly the test file the approved plan names did not clear the "
            "gate, so the refusal names a way out that does not work"
        )

    def test_the_planned_test_file_is_writable_while_both_gates_are_pending(
        self, repo
    ) -> None:
        from writ.session.gates import _can_write_check

        sid = _sid("writable")
        cache = {
            "mode": "work",
            "current_phase": "testing",
            "gates_approved": [],
            "project_root": str(repo),
        }
        verdict = _can_write_check(
            sid,
            {"tool_input": {"file_path": str(repo / "tests" / "test_thing.py")}},
            skill_dir=SKILL_ROOT,
            cache=cache,
        )

        assert verdict["can_write"] is True, (
            "the test file this gate demands cannot be written while the gate is pending, "
            f"which is a deadlock rather than a control: {verdict!r}"
        )

    def test_a_source_file_is_still_blocked_while_both_gates_are_pending(
        self, repo
    ) -> None:
        """The non-vacuity half: an allow-everything gate would pass the case above while
        proving nothing about the exclusion."""
        from writ.session.gates import _can_write_check

        sid = _sid("blocked")
        cache = {
            "mode": "work",
            "current_phase": "testing",
            "gates_approved": [],
            "project_root": str(repo),
        }
        verdict = _can_write_check(
            sid,
            {"tool_input": {"file_path": str(repo / "writ" / "thing.py")}},
            skill_dir=SKILL_ROOT,
            cache=cache,
        )

        assert verdict["can_write"] is False, (
            "a source write was allowed with neither gate approved, so the case above "
            f"says nothing about the tests/ exclusion: {verdict!r}"
        )
