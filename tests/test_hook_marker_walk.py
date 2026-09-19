"""Guard for the Wave 3 hook marker-walk dedup, and for validate-exit-plan.sh's later
removal from that population (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482).

Originally four hooks inlined an identical project-root marker-walk (`python3 -c` block:
markers [composer.json, package.json, Cargo.toml, go.mod, pyproject.toml, .git], start
os.getcwd(), walk to '/', print dir or empty) despite sourcing common.sh's
detect_project_root. The dedup replaced each inline walk with
`$(detect_project_root "$(pwd)")`.

validate-exit-plan.sh has since moved OFF the bash marker walk entirely: the plan-format
gate must not go silently unenforced in a directory with no marker file, so the hook now
resolves its root through the python `resolve_project_root` call it already spawns for
`_validate_phase_a`, exactly as auto-approve-gate.sh already does for the phase-a gate
itself. MARKER_WALK_HOOKS below is the population that still uses the bash walk; it lost
this one member and the lost coverage is replaced, not dropped, by
TestValidateExitPlanResolvesThroughPython.

RED today (bash-walk population): the three remaining hooks still contain the inline
`markers = ['composer.json',...]` walk and do not call detect_project_root for
PROJECT_ROOT.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / "hooks" / "scripts"
COMMON = REPO / "bin" / "lib" / "common.sh"
# Renamed off its count (was FOUR) so the next addition or removal of a member cannot
# leave a lying identifier behind (plan.md, "The coverage that must not vanish").
MARKER_WALK_HOOKS = ["friction-logger.sh", "auto-approve-gate.sh", "writ-rag-inject.sh"]


def _run_detect(path: str) -> str:
    r = subprocess.run(
        ["bash", "-c", 'source "$1"; detect_project_root "$2"', "_", str(COMMON), path],
        capture_output=True, text=True, timeout=20,
    )
    return r.stdout.strip()


class TestHooksUseHelper:
    def test_four_hooks_call_detect_project_root(self) -> None:
        for h in MARKER_WALK_HOOKS:
            src = (HOOKS / h).read_text()
            assert "detect_project_root" in src, f"{h} must call detect_project_root"
            assert "markers = ['composer.json'" not in src, (
                f"{h} must not keep the inline marker-walk"
            )

    def test_hooks_pass_physical_pwd(self) -> None:
        # pwd -P (physical, symlink-resolved) matches the OLD inline os.getcwd(), so
        # PROJECT_ROOT stays byte-identical even when the cwd is reached via a symlink
        # (bare `pwd` is logical and would diverge). Behavior-preservation, not convenience.
        for h in MARKER_WALK_HOOKS:
            src = (HOOKS / h).read_text()
            assert 'detect_project_root "$(pwd -P)"' in src, (
                f"{h} must pass pwd -P (physical) for os.getcwd() parity"
            )

    def test_auto_approve_project_root_still_deferred(self) -> None:
        # POL-5b-3b: PROJECT_ROOT must be computed inside the approval conditional, never
        # unconditionally at column 0 (mirrors test_pol5b3b::test_project_root_not_unconditional).
        import re

        src = (HOOKS / "auto-approve-gate.sh").read_text()
        assert re.search(r"^PROJECT_ROOT=\$\(", src, re.M) is None, (
            "auto-approve must keep PROJECT_ROOT deferred (indented, inside the approval block)"
        )


class TestDetectProjectRootEquivalence:
    def test_finds_nearest_ancestor_with_marker(self, tmp_path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        assert _run_detect(str(sub)) == str(tmp_path)

    def test_empty_or_not_self_when_no_marker(self, tmp_path) -> None:
        # a bare dir with no marker: detect_project_root walks past it (to an ancestor with a
        # marker, or to '/' -> empty). Either way it must NOT claim the marker-less dir itself.
        bare = tmp_path / "no_markers"
        bare.mkdir()
        assert _run_detect(str(bare)) != str(bare)


class TestValidateExitPlanResolvesThroughPython:
    """The coverage validate-exit-plan.sh's removal from MARKER_WALK_HOOKS must not
    silently drop (plan.md, "The coverage that must not vanish"). Dropping the hook
    from the population above without adding this class would let the hook regress to
    the bash marker walk with nothing here catching it.

    Reddening mutation (capabilities.md): re-add
    `PROJECT_ROOT=$(detect_project_root "$(pwd -P)")` to validate-exit-plan.sh -- the
    hook resolves no project root in bash any more, so it must contain no
    `detect_project_root` call anywhere and must resolve through the python
    `resolve_project_root` call instead.
    """

    def test_hook_contains_no_detect_project_root_call(self) -> None:
        """The pin is on the CALL, not on the words.

        A bare `"detect_project_root" not in src` also forbids naming the function in
        a comment, which is the wrong thing to police: this hook's comments have to
        explain WHY it resolves in python while its siblings walk in bash, and that
        explanation is clearest when it can name what it is contrasting with. Strip
        the comments, then look for an invocation.
        """
        src = (HOOKS / "validate-exit-plan.sh").read_text()
        code = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        for invocation in ('detect_project_root "', "detect_project_root $", "$(detect_project_root"):
            assert invocation not in code, (
                "validate-exit-plan.sh must not resolve its root with the bash marker "
                "walk any more; a directory with no marker file must not silently "
                f"disable the plan-format gate (found {invocation!r})"
            )

    def test_hook_resolves_through_the_python_resolver(self) -> None:
        src = (HOOKS / "validate-exit-plan.sh").read_text()
        assert "resolve_project_root" in src, (
            "validate-exit-plan.sh must resolve its root through "
            "writ.session.locators.resolve_project_root, the same resolver "
            "auto-approve-gate.sh already defers to for the phase-a gate itself"
        )
