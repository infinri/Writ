"""Coverage crediting, part 2b + item 3: a sub-agent's examined files and injected
rule ids roll up to its parent at SubagentStop.

Plan: .claude/plans/f7fc2b37-9a53-4011-a69f-e6b97f5e45fe/plan.md

A Magento investigate session froze 951 files, fanned out 54 sub-agents, and
coverage-rollup reported 950/950 while synthesis-gate reported 0 files examined --
because nothing moves a child's examined files (or the rule ids it was shown) to its
parent when the child stops. This pins capabilities 11 through 19: writ-subagent-stop.sh
calls the new `rollup-subagent <agent_id> <parent_session_id>` CLI command (backed by
`writ/session/subagent_rollup.py`, not yet created) to set-union the child's
pretool_queried_files + file citations into the parent's pretool_queried_files, and the
child's rule ids into a NEW parent field, `subagent_rule_ids`, which `_validate_phase_a`
then treats as citable alongside `always_on_rule_ids`.

Every case here is RED on HEAD: `writ-subagent-stop.sh` has no rollup block at all, the
`rollup-subagent` CLI command does not exist (`writ-session.py` reports "Unknown
command"), and `_validate_phase_a` does not read `subagent_rule_ids`. Per
TEST-REGRESSION-001, GREEN once 2b + item 3 land.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

# autouse: pins cwd to a sandbox so `mode set` / a stray cleanup cannot touch THIS
# repo's own gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
HOOK = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), os.pardir, "hooks", "scripts", "writ-subagent-stop.sh"
    )
)
HELPER = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, "bin", "lib", "writ-session.py")
)

REAL_RULE = "TEST-RULE-001"
REAL_ALWAYS_ON = "TEST-ALWAYS-ON-001"
INVENTED = "TOTALLY-MADE-UP-999"


# ---------------------------------------------------------------------------
# Shared subprocess helpers -- writ-session.py + writ-subagent-stop.sh, real
# cache files under WRIT_CACHE_DIR=tmp_path. No mocks of state.
# ---------------------------------------------------------------------------


def _env(cache_dir):
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    env["WRIT_FRICTION_LOG"] = os.path.join(str(cache_dir), "friction.log")
    return env


def _seed_mode(cache_dir, sid, mode):
    subprocess.run(
        [sys.executable, HELPER, "mode", "set", mode, sid],
        env=_env(cache_dir), check=True, capture_output=True, text=True,
    )


def _update(cache_dir, sid, *args):
    subprocess.run(
        [sys.executable, HELPER, "update", sid, *args],
        env=_env(cache_dir), check=True, capture_output=True, text=True,
    )


def _read(cache_dir, sid):
    r = subprocess.run(
        [sys.executable, HELPER, "read", sid],
        env=_env(cache_dir), check=True, capture_output=True, text=True,
    )
    return json.loads(r.stdout)


def _cache_path(cache_dir, sid):
    return os.path.join(str(cache_dir), f"writ-session-{sid}.json")


def _cache_exists(cache_dir, sid):
    return os.path.exists(_cache_path(cache_dir, sid))


def _rollup_cli(cache_dir, agent_id, parent_id):
    """Direct CLI call to the not-yet-existing `rollup-subagent` command. On HEAD
    this exits nonzero with "Unknown command", which is the RED failure the
    capability-16/17 tests below are pinned to surface."""
    return subprocess.run(
        [sys.executable, HELPER, "rollup-subagent", agent_id, parent_id],
        env=_env(cache_dir), capture_output=True, text=True, timeout=20,
    )


def _seed_child_with_cache_source(cache_dir, agent_id, parent_id, cache_source):
    """A real python subprocess calling the real `seed_subagent_cache`, never a
    hand-written cache file: `declared_scope=None` means no role-scope fetch, so
    nothing reaches port 8765. Parameters passed through argv (never interpolated
    into source text) so an id can never break the script it names."""
    script = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[4])\n"
        "from writ.session.subagent_seed import seed_subagent_cache\n"
        "ok = seed_subagent_cache(sys.argv[1], sys.argv[2], cache_source=sys.argv[3],\n"
        "                        role='writ-planner', role_source='envelope',\n"
        "                        declared_scope=None, default_mode='work')\n"
        "assert ok is True, f'seed_subagent_cache returned {ok!r}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, agent_id, parent_id, cache_source, REPO_ROOT],
        env=_env(cache_dir), capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr


def _seed_child_as_start(cache_dir, agent_id, parent_id):
    """Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item 5: the ONLY way a child's
    cache carries `cache_source: subagent_start` -- the real seeder, run for real,
    never a hand-written cache file standing in for it."""
    _seed_child_with_cache_source(cache_dir, agent_id, parent_id, "subagent_start")


def _seed_child_as_lazy(cache_dir, agent_id, parent_id):
    """The lazy-seed path: a hook noticed an agent nobody governed and seeded it
    anyway. Its cache carries `cache_source: lazy_seed`, never `subagent_start`."""
    _seed_child_with_cache_source(cache_dir, agent_id, parent_id, "lazy_seed")


def _run_stop_hook(cache_dir, *, agent_id, parent_session_id=..., agent_type="writ-planner"):
    """`parent_session_id=...` (sentinel default) sends session_id=agent_id (no real
    parent); pass an explicit value, or `None` to omit the key entirely
    (capability 15's missing-parent-id case)."""
    envelope = {"agent_id": agent_id, "agent_type": agent_type}
    if parent_session_id is None:
        pass
    elif parent_session_id is ...:
        envelope["session_id"] = agent_id
    else:
        envelope["session_id"] = parent_session_id
    return subprocess.run(
        ["bash", HOOK], input=json.dumps(envelope),
        capture_output=True, text=True, env=_env(cache_dir), timeout=20,
    )


def _plan(cited: list[str]) -> str:
    """A plan.md that passes every OTHER phase-a check (mirrors
    tests/test_always_on_citations.py's `_plan`), so only citations are under test."""
    ids = "\n".join(f"- [{rid}] why it applies here." for rid in cited)
    return (
        "# Plan: something\n\n"
        "## Files\n\n"
        "- `writ/example.py` (modify) -- because the thing needs doing.\n\n"
        "## Analysis\n\n"
        "The what and the why, with contracts and integration points.\n\n"
        "## Rules Applied\n\n"
        f"{ids}\n\n"
        "## Capabilities\n\n"
        "- [ ] the behavior is testable\n"
    )


def _validate_phase_a(root, session_id):
    from writ.session.approval_workflow import _validate_phase_a as impl
    return impl(str(root), session_id)


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    return root


@pytest.fixture(autouse=True)
def _writ_cache_dir_env(tmp_path, monkeypatch):
    """Every helper above spawns writ-session.py / the stop hook with
    WRIT_CACHE_DIR=tmp_path. The phase-a cases (18, 19) also call
    _validate_phase_a IN-PROCESS, which resolves WRIT_CACHE_DIR from this test
    process's own environment at call time -- without this, that call reads the
    default cache location and the hallucination check on an empty available set
    silently answers "no hallucination" no matter what the plan cites."""
    monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))


class TestStopHookUnionsExaminedFilesIntoParent:
    """Capability 11: writ-subagent-stop.sh for a seeded child of the parent unions
    the child's pretool_queried_files and its citation_log file refs into the
    parent's pretool_queried_files, keeping the parent's existing entries."""

    def test_child_files_and_citations_land_in_parent_alongside_existing_entries(
        self, tmp_path,
    ):
        _seed_mode(tmp_path, "parent-11", "investigate")
        _update(tmp_path, "parent-11", "--add-pretool-file", "/already/examined/by-parent.py")

        _seed_child_as_start(tmp_path, "child-11", "parent-11")
        _update(
            tmp_path, "child-11",
            "--add-pretool-file", "/child/opened.py",
            "--add-citation", json.dumps({"artifact_type": "file", "ref": "/child/cited.py"}),
        )

        r = _run_stop_hook(tmp_path, agent_id="child-11", parent_session_id="parent-11")
        assert r.returncode == 0, r.stderr

        parent = _read(tmp_path, "parent-11")
        assert set(parent["pretool_queried_files"]) == {
            "/already/examined/by-parent.py", "/child/opened.py", "/child/cited.py",
        }


class TestStopHookUnionsRuleIdsIntoSubagentRuleIds:
    """Capability 12: the same stop unions the child's loaded_rule_ids,
    loaded_rule_ids_by_phase buckets and always_on_rule_ids into the parent's
    subagent_rule_ids, and the parent's loaded_rule_ids is byte-for-byte unchanged."""

    def test_child_rule_ids_land_in_subagent_rule_ids_not_loaded_rule_ids(self, tmp_path):
        _seed_mode(tmp_path, "parent-12", "investigate")
        _update(tmp_path, "parent-12", "--add-rules", json.dumps(["PARENT-OWN-001"]))
        parent_loaded_before = _read(tmp_path, "parent-12")["loaded_rule_ids"]
        assert parent_loaded_before == ["PARENT-OWN-001"]

        _seed_child_as_start(tmp_path, "child-12", "parent-12")
        _update(
            tmp_path, "child-12",
            "--add-rules", json.dumps([REAL_RULE]),
            "--add-always-on-rules", json.dumps([REAL_ALWAYS_ON]),
        )

        r = _run_stop_hook(tmp_path, agent_id="child-12", parent_session_id="parent-12")
        assert r.returncode == 0, r.stderr

        parent = _read(tmp_path, "parent-12")
        assert parent["loaded_rule_ids"] == parent_loaded_before, (
            "loaded_rule_ids must be byte-for-byte unchanged: it doubles as the "
            "parent's own ranked-query exclude list"
        )
        assert set(parent.get("subagent_rule_ids", [])) == {REAL_RULE, REAL_ALWAYS_ON}


class TestStopHookRollupIsIdempotent:
    """Capability 13: running the stop hook twice for the same child leaves the
    parent's pretool_queried_files and subagent_rule_ids identical to after the
    first run (no duplicates)."""

    def test_second_run_changes_nothing(self, tmp_path):
        _seed_mode(tmp_path, "parent-13", "investigate")
        _seed_child_as_start(tmp_path, "child-13", "parent-13")
        _update(
            tmp_path, "child-13",
            "--add-pretool-file", "/child/idem.py",
            "--add-rules", json.dumps([REAL_RULE]),
        )

        r1 = _run_stop_hook(tmp_path, agent_id="child-13", parent_session_id="parent-13")
        assert r1.returncode == 0, r1.stderr
        after_first = _read(tmp_path, "parent-13")

        r2 = _run_stop_hook(tmp_path, agent_id="child-13", parent_session_id="parent-13")
        assert r2.returncode == 0, r2.stderr
        after_second = _read(tmp_path, "parent-13")

        assert after_second["pretool_queried_files"] == after_first["pretool_queried_files"]
        assert after_second.get("subagent_rule_ids") == after_first.get("subagent_rule_ids")


class TestChildCacheAbsent:
    """Capability 14: child cache absent (CACHE_STATE absent): the parent cache
    file is unchanged and no child cache file is created."""

    def test_no_child_cache_no_mutation_no_creation(self, tmp_path):
        _seed_mode(tmp_path, "parent-14", "investigate")
        before = _cache_path(tmp_path, "parent-14")
        with open(before, "rb") as f:
            parent_bytes_before = f.read()

        assert not _cache_exists(tmp_path, "never-seeded-child-14")
        r = _run_stop_hook(
            tmp_path, agent_id="never-seeded-child-14", parent_session_id="parent-14",
        )
        assert r.returncode == 0, r.stderr

        with open(before, "rb") as f:
            parent_bytes_after = f.read()
        assert parent_bytes_after == parent_bytes_before
        assert not _cache_exists(tmp_path, "never-seeded-child-14")


class TestMissingParentId:
    """Capability 15: envelope with no session_id (parent id missing): no cache is
    written for the parent and no file named for the empty id appears."""

    def test_no_parent_id_writes_no_cache_for_the_empty_id(self, tmp_path):
        _seed_mode(tmp_path, "child-15", "investigate")
        _update(tmp_path, "child-15", "--add-pretool-file", "/child/orphan.py")

        r = _run_stop_hook(tmp_path, agent_id="child-15", parent_session_id=None)
        assert r.returncode == 0, r.stderr

        assert not _cache_exists(tmp_path, "")
        assert not os.path.exists(os.path.join(str(tmp_path), "writ-session-.json"))


class TestChildOfADifferentParent:
    """Capability 16: a child whose parent_session_id names a different session:
    the given parent is untouched (rollup-subagent reports skipped/not_child_of_parent)."""

    def test_given_parent_is_untouched_via_the_hook(self, tmp_path):
        _seed_mode(tmp_path, "real-parent-16", "investigate")
        _seed_mode(tmp_path, "actual-parent-16", "investigate")
        _seed_child_as_start(tmp_path, "child-16", "real-parent-16")
        _update(tmp_path, "child-16", "--add-pretool-file", "/child/misattributed.py")

        r = _run_stop_hook(tmp_path, agent_id="child-16", parent_session_id="actual-parent-16")
        assert r.returncode == 0, r.stderr

        untouched = _read(tmp_path, "actual-parent-16")
        assert untouched["pretool_queried_files"] == []
        assert untouched.get("subagent_rule_ids", []) == []

    def test_rollup_subagent_cli_reports_skipped_not_child_of_parent(self, tmp_path):
        _seed_mode(tmp_path, "real-parent-16b", "investigate")
        _seed_mode(tmp_path, "actual-parent-16b", "investigate")
        _seed_child_as_start(tmp_path, "child-16b", "real-parent-16b")

        r = _rollup_cli(tmp_path, "child-16b", "actual-parent-16b")
        assert r.returncode == 0, r.stderr
        result = json.loads(r.stdout)
        assert result == {"status": "skipped", "reason": "not_child_of_parent"}


class TestParentAbsent:
    """Capability 17: parent cache file absent: rollup-subagent reports
    skipped/parent_absent and does not create it."""

    def test_rollup_subagent_cli_reports_skipped_parent_absent(self, tmp_path):
        # seed_subagent_cache's default_mode="work" lets this seed succeed even
        # though "never-existed-parent-17" has no cache file at all (`_read_cache`
        # of a missing file answers the empty default cache, with no mode to
        # inherit): the child is genuinely start-seeded and genuinely linked to a
        # parent id that never had a cache, which is exactly capability 17's case.
        _seed_child_as_start(tmp_path, "child-17", "never-existed-parent-17")

        assert not _cache_exists(tmp_path, "never-existed-parent-17")
        r = _rollup_cli(tmp_path, "child-17", "never-existed-parent-17")
        assert r.returncode == 0, r.stderr
        result = json.loads(r.stdout)
        assert result == {"status": "skipped", "reason": "parent_absent"}
        assert not _cache_exists(tmp_path, "never-existed-parent-17")


class TestPlanGateAcceptsRolledUpChildRuleIds:
    """Capability 18: a plan.md citing a rule id present only in a child's
    loaded_rule_ids is rejected as hallucinated by _validate_phase_a(parent) before
    that child's SubagentStop, and accepted after it."""

    def test_rejected_before_rollup_accepted_after(self, tmp_path, project):
        _seed_mode(tmp_path, "parent-18", "work")
        # A ranked rule the parent loaded itself: _validate_citations answers "no
        # hallucination" unconditionally when the available set is empty (absence
        # cannot be proven when nothing was captured), so without this the
        # "before" assertion below would pass for the wrong reason -- the
        # detector never engaging at all, exactly like
        # tests/test_always_on_citations.py's TestGateAcceptsInjectedAlwaysOnRules
        # docstring explains.
        _update(tmp_path, "parent-18", "--add-rules", json.dumps(["PARENT-18-RANKED-001"]))
        (project / "plan.md").write_text(_plan(["CHILD-ONLY-001"]))

        before = _validate_phase_a(project, "parent-18")
        assert before is not None and "hallucinated" in before
        assert "CHILD-ONLY-001" in before

        _seed_child_as_start(tmp_path, "child-18", "parent-18")
        _update(tmp_path, "child-18", "--add-rules", json.dumps(["CHILD-ONLY-001"]))
        r = _run_stop_hook(tmp_path, agent_id="child-18", parent_session_id="parent-18")
        assert r.returncode == 0, r.stderr

        after = _validate_phase_a(project, "parent-18")
        assert after is None, after


class TestPlanGateStillRejectsForeignAndInventedIds:
    """Capability 19: a rule id injected only into a child of a DIFFERENT parent is
    still rejected as hallucinated for this parent, and an invented id is still
    rejected."""

    def test_rule_from_a_different_parents_child_is_still_hallucinated_here(
        self, tmp_path, project,
    ):
        _seed_mode(tmp_path, "parent-19", "work")
        _update(tmp_path, "parent-19", "--add-rules", json.dumps(["PARENT-19-RANKED-001"]))
        _seed_mode(tmp_path, "other-parent-19", "work")
        _seed_child_as_start(tmp_path, "other-child-19", "other-parent-19")
        _update(tmp_path, "other-child-19", "--add-rules", json.dumps(["OTHER-PARENTS-CHILD-001"]))
        r = _run_stop_hook(
            tmp_path, agent_id="other-child-19", parent_session_id="other-parent-19",
        )
        assert r.returncode == 0, r.stderr
        # Sanity: it DID roll up into the other parent, so the negative below is a
        # real discrimination and not just "rollup never runs".
        other_parent = _read(tmp_path, "other-parent-19")
        assert "OTHER-PARENTS-CHILD-001" in other_parent.get("subagent_rule_ids", [])

        (project / "plan.md").write_text(_plan(["OTHER-PARENTS-CHILD-001"]))
        err = _validate_phase_a(project, "parent-19")
        assert err is not None and "hallucinated" in err
        assert "OTHER-PARENTS-CHILD-001" in err

    def test_an_invented_id_is_still_rejected(self, tmp_path, project):
        _seed_mode(tmp_path, "parent-19b", "work")
        _update(tmp_path, "parent-19b", "--add-rules", json.dumps(["PARENT-19B-RANKED-001"]))
        (project / "plan.md").write_text(_plan([INVENTED]))
        err = _validate_phase_a(project, "parent-19b")
        assert err is not None and "hallucinated" in err
        assert INVENTED in err


class TestForgedParentLinkAloneDoesNotGrantRollup:
    """Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item 5: `writ-session.py update
    --parent-session-id` is the FORGED shape -- anyone can set it, and it is not
    `cache_source`, which only `seed_subagent_cache` ever writes. A child linked to
    a parent ONLY that way (never seeded by the start hook) must be refused with
    skipped/child_not_start_seeded, and the parent's evidence must be unchanged,
    both through the CLI and through the real stop hook. RED at HEAD: the rollup
    checks only parent_session_id equality, so a forged link merges freely."""

    def test_forged_link_is_skipped_via_the_cli_and_leaves_the_parent_unchanged(
        self, tmp_path
    ):
        _seed_mode(tmp_path, "parent-forged-cli", "investigate")
        _seed_mode(tmp_path, "child-forged-cli", "investigate")
        _update(
            tmp_path, "child-forged-cli",
            "--parent-session-id", "parent-forged-cli", "--is-subagent", "true",
            "--add-pretool-file", "/child/forged.py",
            "--add-rules", json.dumps([REAL_RULE]),
        )

        r = _rollup_cli(tmp_path, "child-forged-cli", "parent-forged-cli")
        assert r.returncode == 0, r.stderr
        result = json.loads(r.stdout)
        assert result == {"status": "skipped", "reason": "child_not_start_seeded"}

        parent = _read(tmp_path, "parent-forged-cli")
        assert parent["pretool_queried_files"] == []
        assert parent.get("subagent_rule_ids", []) == []

    def test_forged_link_is_skipped_via_the_real_stop_hook_and_leaves_the_parent_unchanged(
        self, tmp_path
    ):
        _seed_mode(tmp_path, "parent-forged-hook", "investigate")
        _seed_mode(tmp_path, "child-forged-hook", "investigate")
        _update(
            tmp_path, "child-forged-hook",
            "--parent-session-id", "parent-forged-hook", "--is-subagent", "true",
            "--add-pretool-file", "/child/forged-hook.py",
            "--add-rules", json.dumps([REAL_RULE]),
        )

        r = _run_stop_hook(
            tmp_path, agent_id="child-forged-hook", parent_session_id="parent-forged-hook",
        )
        assert r.returncode == 0, r.stderr

        parent = _read(tmp_path, "parent-forged-hook")
        assert parent["pretool_queried_files"] == []
        assert parent.get("subagent_rule_ids", []) == []


class TestLazilySeededChildIsAlsoRefused:
    """A lazily seeded child (the start hook never ran for it, cache_source ==
    lazy_seed) is also refused, with the SAME reason: the plan's accepted
    consequence is that this population's rollup is exactly the population whose
    link Writ did not observe."""

    def test_lazy_seed_child_is_skipped_with_child_not_start_seeded(self, tmp_path):
        _seed_mode(tmp_path, "parent-lazy", "investigate")
        _seed_child_as_lazy(tmp_path, "child-lazy", "parent-lazy")
        _update(
            tmp_path, "child-lazy",
            "--add-pretool-file", "/child/lazy.py",
            "--add-rules", json.dumps([REAL_RULE]),
        )

        r = _rollup_cli(tmp_path, "child-lazy", "parent-lazy")
        assert r.returncode == 0, r.stderr
        result = json.loads(r.stdout)
        assert result == {"status": "skipped", "reason": "child_not_start_seeded"}

        parent = _read(tmp_path, "parent-lazy")
        assert parent["pretool_queried_files"] == []
        assert parent.get("subagent_rule_ids", []) == []
