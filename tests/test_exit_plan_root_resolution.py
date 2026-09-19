"""The plan-format gate must not be disabled by a missing marker file.

Plan: .claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md. Runs the REAL
`hooks/scripts/validate-exit-plan.sh` as a subprocess (ENF-SYS-005: the hook's
behavior cannot be proven by unit-testing its python block) in the directory shapes
plan.md's Analysis and capabilities.md enumerate: an unmarked directory with no
plan.md, the same unmarked directory holding a VALID session-scoped plan.md, a
subdirectory of a marked repo whose ROOT holds the valid plan, a marked repo with no
plan.md, and the deleted-cwd skip. Every assertion reads the emitted permission
decision on stdout and the row found by globbing the isolated log root (never the
path the hook believes it wrote to -- plan.md's own Chain check: `resolve_project`
can file a row under `_unresolved` when the cwd has vanished, so a test that trusted
a computed scope name would miss the very row it exists to find).

MEASURED STATUS AS OF THIS WRITE (hook untouched, production files not yet
modified -- this cycle only writes tests):

  - Capability 1 (unmarked, no plan.md -> deny): RED. Today's bash-only
    `detect_project_root` finds no marker in an unmarked directory and the hook's
    `[ -z "$PROJECT_ROOT" ] && exit 0` guard exits silently: rc=0, no stdout, no
    audit row. Measured directly against the unmodified hook.
  - Capability 2 (unmarked, valid session-scoped plan.md -> allow): RED, same
    reason -- the hook never reaches plan validation at all in an unmarked
    directory today, so the valid plan is never read.
  - Capability 3 (subdirectory of a marked repo, root plan valid -> allow): GREEN
    today. Bash's own marker walk already resolves a marked repo correctly
    independent of the python resolver the fix introduces, and the plan's own
    Analysis names this the over-deletion guard: it must stay green through the fix.
  - Capability 4 (marked repo, no plan.md -> deny + exitplanmode_denial row): GREEN
    today, for the same reason as 3 -- bash's marker walk already works for marked
    repos. This is the "marked-deny case GREEN" the dispatch brief measured by hand.
  - Capability 5 (deleted cwd -> exit 0, no decision, one exitplanmode_skipped row):
    RED. Today's hook has no `exitplanmode_skipped` event at all, and
    `writ/shared/logging.py`'s `STREAM_MAP` does not yet carry that event name
    either, so even a hook that emitted it today would misfile it to friction, not
    audit. Measured: a deleted cwd today produces rc=0, empty stdout, and no row of
    any kind (the stale `$PWD` bash falls back to walks past every marker and the
    hook exits via the same silent guard as capability 1).

HARNESS: `tests.firedrill._harness` (Isolation/build_env/write_cache/run_hook),
already proven for hook-subprocess tests in `tests/test_debug_lens_predicate.py`,
`tests/test_blackbox_census.py` and `tests/test_blackbox_record_schema.py`, and for
this exact hook in `tests/firedrill/_census.py::_setup_validate_exit_plan`.
`make_isolation`'s `project_root` always carries a `.git` marker (by design, so
every OTHER drill case in the suite resolves it); the unmarked-directory
capabilities below deliberately use a sibling directory instead so the marker tier
never fires. `write_cache` seeds `mode: work` and `loaded_rule_ids: [TEST-CI-001]`
directly as a cache FILE (never mutated in-process), so `_validate_phase_a`'s
citation check does not read PLAN_CONTENT's own `## Rules Applied` citation as
hallucinated.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.firedrill._harness import build_env, make_isolation, run_hook, write_cache

# WRIT_FRICTION_LOG collapses every typed stream into one file (autouse fixture in
# tests/conftest.py), which would make every "landed on the audit stream" assertion
# below vacuous. build_env() already strips it from the child env defensively; this
# marker documents the same intent at the module level (established convention,
# tests/test_debug_lens_predicate.py).
pytestmark = pytest.mark.no_friction_isolation

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "validate-exit-plan.sh"

PLAN_CONTENT = """\
## Files
- `service.py` (modify) -- implement the thing

## Analysis
Implement the thing with care and verify behavior.

## Rules Applied
- TEST-CI-001: all tests pass before merge.

## Capabilities
- [ ] the thing works
"""

_ENVELOPE_TOOL_INPUT: dict = {}


def _envelope(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": "ExitPlanMode",
        "tool_input": _ENVELOPE_TOOL_INPUT,
    }


def _rows(log_root: Path, stream: str) -> list[dict]:
    """Every row in every `<stream>.jsonl` under `log_root`, however deep.

    Globs rather than trusting a computed project-scope path, so the assertion
    cannot pass or fail on the finder's own idea of where the row should be.
    `read_streams(project, [stream])` needs the scope name in advance; this does
    not, so a row filed under any scope is still found and counted.

    Under this harness the scope is deterministic: `_harness.build_env` sets
    WRIT_LOG_PROJECT, so rows land under `iso.log_project` even in the deleted-cwd
    case. The glob is a safety net against that changing, not proof that a row can
    land elsewhere today.
    """
    out = []
    for path in log_root.rglob(f"{stream}.jsonl"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _seed(iso, session_id: str) -> None:
    write_cache(iso, {"mode": "work", "loaded_rule_ids": ["TEST-CI-001"]},
                session_id=session_id)


class TestUnmarkedDirectoryNoPlan:
    """Reddening mutation (capabilities.md #1): restore
    `PROJECT_ROOT=$(detect_project_root "$(pwd -P)")` and the
    `[ -z "$PROJECT_ROOT" ] && exit 0` guard, and the hook emits nothing.
    """

    def test_denies_with_the_plan_format_reason(self, tmp_path) -> None:
        sid = "unmarked-noplan"
        iso = make_isolation(tmp_path, session_id=sid)
        _seed(iso, sid)
        unmarked = iso.tmp_path / "unmarked"
        unmarked.mkdir()

        result = run_hook("validate-exit-plan.sh", _envelope(sid), iso, cwd=unmarked)

        assert result.permission_decision() == "deny", (
            f"expected a deny in an unmarked directory with no plan.md; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "plan.md not found" in result.permission_reason()
        denials = [
            r for r in _rows(iso.log_root, "audit")
            if r.get("session") == sid and r.get("event") == "exitplanmode_denial"
        ]
        assert denials, (
            f"no exitplanmode_denial row landed in the audit stream: "
            f"{_rows(iso.log_root, 'audit')}"
        )


class TestUnmarkedDirectoryValidSessionPlan:
    """Reddening mutation (capabilities.md #2): make the python block validate
    against a nonexistent root so every plan reads as missing, and a valid plan
    is denied.
    """

    def test_allows_and_emits_the_allow_directive(self, tmp_path) -> None:
        sid = "unmarked-validplan"
        iso = make_isolation(tmp_path, session_id=sid)
        _seed(iso, sid)
        unmarked = iso.tmp_path / "unmarked"
        plan_dir = unmarked / ".claude" / "plans" / sid
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan.md").write_text(PLAN_CONTENT)

        result = run_hook("validate-exit-plan.sh", _envelope(sid), iso, cwd=unmarked)

        assert result.permission_decision() is None, (
            f"an allow expresses no permissionDecision key at all; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "approved to write code" in result.permission_reason()
        allows = [
            r for r in _rows(iso.log_root, "audit")
            if r.get("session") == sid and r.get("event") == "exitplanmode_allow"
        ]
        assert allows, (
            f"no exitplanmode_allow row landed in the audit stream: "
            f"{_rows(iso.log_root, 'audit')}"
        )


class TestMarkedRepoSubdirectoryMarkerTierWins:
    """Reddening mutation (capabilities.md #3): replace the `resolve_project_root`
    call with `root = sys.argv[2]`, and the subdirectory has no plan so the hook
    denies.
    """

    def test_allows_from_the_repo_roots_plan(self, tmp_path) -> None:
        sid = "marked-subdir"
        iso = make_isolation(tmp_path, session_id=sid)
        _seed(iso, sid)
        (iso.project_root / "plan.md").write_text(PLAN_CONTENT)
        subdir = iso.project_root / "src" / "deep"
        subdir.mkdir(parents=True)

        result = run_hook("validate-exit-plan.sh", _envelope(sid), iso, cwd=subdir)

        assert result.permission_decision() is None, (
            f"expected an allow from the repo root's plan.md, since the marker "
            f"tier must win over the cwd tier; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
        assert "approved to write code" in result.permission_reason()
        allows = [
            r for r in _rows(iso.log_root, "audit")
            if r.get("session") == sid and r.get("event") == "exitplanmode_allow"
        ]
        assert allows, (
            f"no exitplanmode_allow row landed in the audit stream: "
            f"{_rows(iso.log_root, 'audit')}"
        )


class TestMarkedRepoNoPlan:
    """Reddening mutation (capabilities.md #4): make the python block exit 3
    unconditionally, and the deny plus the row disappear.
    """

    def test_denies_and_writes_the_denial_row(self, tmp_path) -> None:
        sid = "marked-noplan"
        iso = make_isolation(tmp_path, session_id=sid)
        _seed(iso, sid)

        result = run_hook(
            "validate-exit-plan.sh", _envelope(sid), iso, cwd=iso.project_root,
        )

        assert result.permission_decision() == "deny", (
            f"expected a deny in a marked repo with no plan.md; "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "plan.md not found" in result.permission_reason()
        denials = [
            r for r in _rows(iso.log_root, "audit")
            if r.get("session") == sid and r.get("event") == "exitplanmode_denial"
        ]
        assert denials, (
            f"no exitplanmode_denial row landed in the audit stream: "
            f"{_rows(iso.log_root, 'audit')}"
        )


class TestDeletedWorkingDirectory:
    """Reddening mutation (capabilities.md #5): delete the `log_friction_event`
    call from the skip branch, and the skip is silent again.
    """

    def _run_with_vanished_cwd(self, iso, sid: str) -> subprocess.CompletedProcess:
        """Reproduces a process whose cwd has been unlinked out from under it.

        `cd "$dir" && rmdir "$dir"` inside the SAME shell that then `exec`s the
        hook: the directory exists at chdir time (satisfying subprocess.run's own
        requirement that its `cwd=` argument exist), then is removed while it is
        still that shell's current directory, so `pwd -P` inside the hook fails
        with ENOENT exactly as it would for a directory deleted out from under a
        live Claude Code process. Measured directly: `pwd -P` after this sequence
        prints nothing and exits 1.
        """
        deleted = iso.tmp_path / "vanishes"
        deleted.mkdir()
        env = build_env(iso)
        wrapper = 'cd "$1" && rmdir "$1" && exec bash "$2"'
        return subprocess.run(
            ["bash", "-c", wrapper, "_", str(deleted), str(HOOK)],
            input=json.dumps(_envelope(sid)),
            capture_output=True, text=True, env=env, timeout=30,
        )

    def test_exits_zero_expresses_no_decision_and_writes_one_skipped_row(
        self, tmp_path
    ) -> None:
        sid = "deleted-cwd"
        iso = make_isolation(tmp_path, session_id=sid)
        _seed(iso, sid)

        proc = self._run_with_vanished_cwd(iso, sid)

        assert proc.returncode == 0, (
            f"a vanished cwd must not abort the hook under set -e; "
            f"rc={proc.returncode} stderr={proc.stderr!r}"
        )
        try:
            doc = json.loads(proc.stdout)
        except (json.JSONDecodeError, ValueError):
            doc = None
        decision = ((doc or {}).get("hookSpecificOutput") or {}).get("permissionDecision")
        assert decision is None, (
            f"a vanished cwd must express no permission decision at all "
            f"(neither allow nor deny -- the daemon's advance route is the last "
            f"judge); stdout={proc.stdout!r}"
        )

        skipped = [
            r for r in _rows(iso.log_root, "audit")
            if r.get("session") == sid and r.get("event") == "exitplanmode_skipped"
        ]
        assert len(skipped) == 1, (
            f"expected exactly one exitplanmode_skipped row on the audit stream, "
            f"found {len(skipped)}: {_rows(iso.log_root, 'audit')}"
        )
        assert skipped[0].get("reason") == "no_root", (
            f"expected reason 'no_root' for an unresolvable root; got {skipped[0]!r}"
        )
