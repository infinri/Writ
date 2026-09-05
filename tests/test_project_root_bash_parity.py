"""The bash project-root walk against the LIVE python resolver it is meant to mirror.

Plan: .claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md, "The parity test".
Pins capabilities.md's marker-population, per-marker, intended-divergence, recorded-
divergence and anti-vacuity items.

WHY THE FROZEN `REFERENCE` SNIPPET IS GONE. The old header's justification was "the
original is being deleted: there will be nothing left to extract it from" -- that is
no longer true. The live walk is `writ.session.locators.resolve_project_root` and it
is importable. A frozen copy cannot drift toward the implementation while the
implementation drifts away from it, which is exactly how the old file went stale the
day it was written: it froze the pre-fix "" for the no-marker case while the real
python resolver had already grown a cwd-fallback tier, and the guard that should have
caught the divergence between the bash hook and the live resolver instead compared
bash against a copy of code nothing runs anymore.

THE HAZARD THIS FILE IS STILL BUILT AROUND: normalization. `os.path.abspath` (bash's
model) collapses `.`, `..` and repeated slashes but does NOT resolve symlinks.
`realpath` DOES resolve them, so the obvious one-line bash rewrite (`realpath -m`)
can return a different project root for a path under a symlink. That case stays a
joint assertion below (`TestTheSymlinkTrap`).

FOUR THINGS THIS FILE PINS (plan.md, "The parity test"):

  1. THE MARKER POPULATION, derived on both sides and asserted equal and non-empty
     (`TestMarkerPopulation`), with a behavioral case per marker
     (`TestEveryMarkerResolvesAlone`) so a marker that is listed but whose test can
     never be true still reddens.
  2. THE INTENDED DIVERGENCES (`TestIntendedDivergences`): no marker anywhere, a
     relative start, the filesystem root. Each is pinned explicitly rather than left
     uncovered, with a docstring saying the divergence is intended and why.
  3. ONE RECORDED DIVERGENCE THAT IS NOT ENDORSED (`TestRecordedNotEndorsedDivergence`):
     an absolute start containing `..`. Fixing it is out of this cycle's scope; it is
     pinned so the next person who normalizes one side has to look at it.
  4. PARITY ONLY WHERE PARITY IS CONTRACTED (`_assert_parity` below, and
     `TestAntiVacuity`). The comparison helper runs on absolute, already normalized
     starts -- what every live caller passes -- and asserts the python side resolved
     through the MARKER tier, so two empty (or two cwd-tier) answers can never compare
     equal and pass. The `.`, `..` and repeated-slash cases stop being parity
     assertions and become bash-only assertions against the fixture's known root
     (`TestBashOnlyNormalization`), because bash normalizes and live python does not,
     and inventing a normalizing oracle to compare them would be fabricating the
     equivalence rather than executing it.

Per ENF-SYS-005: the parity claim cannot be proven against a copy of one
implementation (the exact defect being removed), so both walks are EXECUTED and
compared here, never re-implemented.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from writ.session.locators import (  # noqa: E402
    PROJECT_ROOT_MARKERS,
    ROOT_FROM_CWD,
    ROOT_FROM_MARKER,
    ROOT_FROM_NONE,
    resolve_project_root,
)

_MARKER_LINE_RE = re.compile(r'-e\s+"\$path/([^"]+)"')


def _detect_project_root_body() -> str:
    """`detect_project_root`'s own body, sliced from its own source.

    Same technique `TestTheSpawnIsGone` already used against a fixed character
    window and outgrew: scan to the function's own closing brace, not a byte count,
    so growth in a neighbouring function can never spill into this one.
    """
    src = COMMON_SH.read_text()
    start = src.index("detect_project_root() {")
    end = src.index("\n}\n", start)
    return src[start:end]


def _bash_markers() -> list[str]:
    """The marker filenames bash's own `-e "$path/<marker>"` checks test for.

    A regex that stops matching must fail loudly, not silently empty both sides --
    asserted by the caller, not swallowed here.
    """
    return _MARKER_LINE_RE.findall(_detect_project_root_body())


def _bash_detect(start: str, cwd: str) -> str:
    proc = subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail\nsource {COMMON_SH} >/dev/null 2>&1\ndetect_project_root "$1"',
         "_", start],
        capture_output=True, text=True, timeout=60, cwd=cwd,
    )
    assert proc.returncode == 0, (
        f"detect_project_root aborted its caller (rc={proc.returncode}): {proc.stderr[:200]}"
    )
    return proc.stdout.strip()


def _assert_parity(start: str, cwd: str, label: str) -> str:
    """Bash's answer against the LIVE `resolve_project_root`, for an absolute,
    already-normalized start -- what every real caller passes.

    THE ANTI-VACUITY GUARD (capabilities.md, "the parity comparison cannot pass
    vacuously"): asserts the python side resolved through the MARKER tier before
    comparing strings. Without this, an accidentally-unmarked fixture would make
    bash answer "" and python answer (start, "cwd"), and a caller that compared
    `_bash_detect(...)` only against `resolve_project_root(...)[0]` (dropping the
    tier) would see "" == "" or two identical cwd-tier strings and pass for the
    wrong reason. Reddening mutation (capabilities.md): delete the pyproject.toml
    line from the `project` fixture, and every parity case that uses it fails
    loudly here instead of passing on two empty (or two cwd-tier) answers.
    """
    bash_got = _bash_detect(start, cwd)
    py_root, py_tier = resolve_project_root(start=start)
    assert py_tier == ROOT_FROM_MARKER, (
        f"{label}: python resolved via tier {py_tier!r}, not the marker tier "
        f"(root={py_root!r}). This fixture likely has no marker in its ancestry, "
        f"which would let an empty/empty or cwd/cwd pair compare equal and pass "
        f"vacuously -- fix the fixture rather than loosen this assertion."
    )
    assert bash_got == py_root, f"{label}: bash said {bash_got!r}, python said {py_root!r}"
    return bash_got


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A marked project root with a nested file, the shape every caller passes."""
    root = tmp_path / "proj"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "src" / "deep" / "mod.py").write_text("x = 1\n")
    return root


# --------------------------------------------------------------------------------- #
# 1. THE MARKER POPULATION
# --------------------------------------------------------------------------------- #


class TestMarkerPopulation:
    def test_population_is_equal_and_nonempty(self) -> None:
        """Reddening mutation (capabilities.md): add "Makefile" to
        PROJECT_ROOT_MARKERS, or delete the `|| [ -e "$path/go.mod" ]` clause from
        bash. Either moves one side without the other and this fails.
        """
        bash_markers = set(_bash_markers())
        python_markers = set(PROJECT_ROOT_MARKERS)
        assert bash_markers, "bash marker extraction found nothing; the regex stopped matching"
        assert python_markers, "PROJECT_ROOT_MARKERS is empty"
        assert bash_markers == python_markers, (
            f"marker populations diverge: bash={bash_markers!r} python={python_markers!r}"
        )


_UNION_MARKERS = sorted(set(_bash_markers()) | set(PROJECT_ROOT_MARKERS))
assert _UNION_MARKERS, "no markers derived from either implementation"


class TestEveryMarkerResolvesAlone:
    """Reddening mutation (capabilities.md): keep `go.mod` in bash's condition but
    make its test unsatisfiable (e.g. an extra ANDed clause that can never be true),
    so the population still matches while the behavior does not.
    """

    @pytest.mark.parametrize("marker", _UNION_MARKERS)
    def test_marker_alone_resolves_to_its_own_directory(self, tmp_path: Path, marker: str) -> None:
        solo = tmp_path / "solo"
        solo.mkdir()
        target = solo / marker
        if marker == ".git":
            target.mkdir()
        else:
            target.write_text("")

        bash_got = _bash_detect(str(solo), str(solo))
        assert bash_got == str(solo), f"bash did not resolve marker {marker!r} to its own dir"

        py_root, py_tier = resolve_project_root(start=str(solo))
        assert (py_root, py_tier) == (str(solo), ROOT_FROM_MARKER), (
            f"python did not resolve marker {marker!r} through the marker tier: "
            f"got {(py_root, py_tier)!r}"
        )


# --------------------------------------------------------------------------------- #
# Parity on real shapes: absolute, already-normalized starts.
# --------------------------------------------------------------------------------- #


class TestParityOnRealShapes:
    def test_a_nested_file_resolves_to_its_project_root(self, project) -> None:
        got = _assert_parity(str(project / "src" / "deep" / "mod.py"), str(project), "nested file")
        assert got == str(project)

    def test_the_project_root_itself_resolves_to_itself(self, project) -> None:
        """E-PROJROOT-BUG: callers pass the root directory itself (check-gates.sh,
        writ-session-end.sh, inject-tier-workflow.sh). Returning the parent, or "",
        silently skips gate checks."""
        assert _assert_parity(str(project), str(project), "root itself") == str(project)

    def test_a_missing_path_does_not_crash_either_side(self, project) -> None:
        _assert_parity(str(project / "does" / "not" / "exist.py"), str(project), "missing")

    @pytest.mark.parametrize("name", [
        "with space", "it's", "star*glob", "brack[et]", "semi;colon", "dollar$sign",
    ])
    def test_awkward_filenames_do_not_break_the_walk(self, project, name: str) -> None:
        """The python version passed the path via argv specifically so a quote or glob
        could not break it. The bash version must be equally unbothered."""
        d = project / "src" / name
        d.mkdir(parents=True, exist_ok=True)
        _assert_parity(str(d / "f.py"), str(project), f"awkward name {name!r}")


class TestBashOnlyNormalization:
    """`.`, `..` and repeated slashes: bash normalizes lexically before checking
    markers; live `resolve_project_root` does not normalize at all (see
    `TestRecordedNotEndorsedDivergence`). Comparing them through `_assert_parity`
    would either fail on a real, unrelated string divergence or require inventing a
    normalizing oracle to paper over it -- fabricating the equivalence rather than
    executing it. These are asserted against the fixture's KNOWN root instead,
    bash-only, exactly as the plan's "parity only where parity is contracted" item
    requires.
    """

    def test_a_dot_segment_is_collapsed(self, project) -> None:
        got = _bash_detect(str(project / "src" / "." / "deep"), str(project))
        assert got == str(project)

    def test_a_dotdot_segment_is_collapsed(self, project) -> None:
        got = _bash_detect(str(project / "src" / "deep" / ".." / ".."), str(project))
        assert got == str(project)

    def test_repeated_slashes_are_collapsed(self, project) -> None:
        got = _bash_detect(f"{project}//src///deep", str(project))
        assert got == str(project)


# --------------------------------------------------------------------------------- #
# 2. THE INTENDED DIVERGENCES
# --------------------------------------------------------------------------------- #


class TestIntendedDivergences:
    def test_no_marker_fallback_is_intended(self, tmp_path: Path) -> None:
        """INTENDED. `resolve_project_root`'s own docstring is the reason this
        diverges at all: without the cwd-fallback tier a directory with no marker
        resolved to "" and "the approval gate then refused every advance", so
        Writ's workflow was "unusable outside conventionally-marked repos". bash
        keeps answering "" for the same case; python answers (start, "cwd").
        Reddening mutation (capabilities.md): make bash fall back to the start
        path, or delete python's `return start, ROOT_FROM_CWD`.
        """
        bare = tmp_path / "no_markers_anywhere"
        bare.mkdir()

        bash_got = _bash_detect(str(bare), str(bare))
        assert bash_got == "", f"bash answered {bash_got!r} for an unmarked directory"

        py_root, py_tier = resolve_project_root(start=str(bare))
        assert (py_root, py_tier) == (str(bare), ROOT_FROM_CWD), (
            f"python answered {(py_root, py_tier)!r}; expected the cwd tier to "
            f"return the start path itself"
        )

    def test_relative_start_is_refused_by_python_and_resolved_by_bash(self, project) -> None:
        """INTENDED. bash resolves a relative start against its own $PWD, which the
        shell it runs in always has. `resolve_project_root` refuses a relative
        start outright rather than risk resolving against the DAEMON's own cwd
        (Writ's install directory, which carries .git, pyproject.toml AND a
        plan.md) -- no live caller sends a relative start; every one passes
        `pwd -P` or an already-absolute path. Reddening mutation (capabilities.md):
        replace python's `os.path.isabs` refusal with `os.path.abspath(start)`.
        """
        bash_got = _bash_detect("src/deep/mod.py", str(project))
        assert bash_got == str(project)

        py_root, py_tier = resolve_project_root(start="src/deep/mod.py")
        assert (py_root, py_tier) == ("", ROOT_FROM_NONE)

    def test_filesystem_root_is_intended(self) -> None:
        """INTENDED. bash's loop condition is `while [ "$path" != "/" ]`, so "/"
        itself is never tested and bash answers ""; `resolve_project_root` tests
        "/" before its own termination check and always answers something (here,
        the cwd tier, since "/" on the test host carries none of the six markers).
        Reddening mutation (capabilities.md): change bash's loop so it tests "/"
        before terminating.
        """
        for marker in PROJECT_ROOT_MARKERS:
            assert not os.path.exists(os.path.join("/", marker)), (
                f"the test host has /{marker}; this pin assumes '/' carries none "
                f"of the six markers and needs a different fixture on this machine"
            )

        bash_got = _bash_detect("/", "/")
        assert bash_got == ""

        py_root, py_tier = resolve_project_root(start="/")
        assert py_root == "/"
        assert py_tier == ROOT_FROM_CWD


# --------------------------------------------------------------------------------- #
# 3. ONE RECORDED DIVERGENCE THAT IS NOT ENDORSED
# --------------------------------------------------------------------------------- #


class TestRecordedNotEndorsedDivergence:
    def test_dotdot_bearing_start_diverges_in_string_not_directory(self, project) -> None:
        """RECORDED, NOT ENDORSED -- found while writing plan.md, not chosen as a
        goal. For an absolute start containing '..', bash normalizes lexically and
        returns the real root; `resolve_project_root` walks the un-normalized
        string and, because the OS itself resolves '..' when `os.path.exists`
        stats the joined path, returns the literal START once that join hits a
        marker. Same directory, two different STRINGS -- and the string is used as
        an identity in places that care (the log project scope, the encoded
        transcript directory). No live caller can reach this: every caller passes
        `pwd -P` or an already-absolute file path, and python refuses relative
        starts outright (see the relative-start pin above). Fixing it is not in
        this cycle; it needs its own evidence about which callers compare root
        strings. Reddening mutation (capabilities.md): add
        `os.path.normpath(start)` to `resolve_project_root`, and the two answers
        become equal.
        """
        sub = project / "sub"
        sub.mkdir()
        start = str(sub / "..")

        bash_got = _bash_detect(start, str(project))
        assert bash_got == str(project), "bash must still normalize '..' to the real root"

        py_root, py_tier = resolve_project_root(start=start)
        assert py_tier == ROOT_FROM_MARKER
        assert py_root == start, (
            "python must return the literal, un-normalized start once the OS-level "
            "'..' resolution finds a marker; if this now differs, someone changed "
            "resolve_project_root's normalization and this pin should be revisited"
        )
        assert py_root != bash_got, (
            "the two sides must name the same directory with DIFFERENT strings; "
            "if they are now byte-equal, the divergence this test records is closed "
            "and this test (not the production code) should be revised"
        )


# --------------------------------------------------------------------------------- #
# The symlink trap: neither walk resolves symlinks, a joint assertion.
# --------------------------------------------------------------------------------- #


class TestTheSymlinkTrap:
    """The case that separates a correct rewrite from a plausible one. Both walks
    are pinned here directly -- not one pinned against a copy of the other."""

    def test_a_symlinked_path_is_not_resolved(self, tmp_path) -> None:
        """`abspath` (bash's normalization model) does NOT resolve symlinks, so the
        walk goes up the LINK's parents, not the target's; `resolve_project_root`
        does no normalization at all and is symlink-blind for the same reason.
        A `realpath`-based rewrite of either side would return the other project.

        Layout: two marked projects. `real/` holds the file; `link/` is a symlink
        to it placed inside a DIFFERENT marked project. Walking the link path must
        find the linking project on BOTH walks.
        """
        real = tmp_path / "real_proj"
        (real / "pkg").mkdir(parents=True)
        (real / "pyproject.toml").write_text("[project]\n")
        (real / "pkg" / "mod.py").write_text("x = 1\n")

        other = tmp_path / "other_proj"
        other.mkdir()
        (other / "package.json").write_text("{}\n")
        link = other / "linked"
        link.symlink_to(real / "pkg")

        got = _assert_parity(str(link / "mod.py"), str(tmp_path), "symlinked path")
        assert got == str(other), (
            "the walk resolved the symlink; neither side should, so this must find "
            f"the linking project {other}, not the link target's project {real}"
        )


# --------------------------------------------------------------------------------- #
# A directory name may contain a newline on Linux.
# --------------------------------------------------------------------------------- #


class TestNewlineInAPathSegment:
    """A directory name may contain a newline on Linux, and that used to break the
    walk. The split was `IFS='/' read -r -a raw <<< "$path"`, and `read` consumes
    only the FIRST LINE of a here-string, so every segment after the newline was
    dropped and the walk returned "". Callers gate on an empty PROJECT_ROOT and skip
    their checks, so this was a silent enforcement hole. The split is parameter
    expansion now, which is byte-exact.
    """

    def test_a_marker_below_a_newline_segment_is_still_found(self, tmp_path) -> None:
        weird = tmp_path / "weird\ndir"
        (weird / ".git").mkdir(parents=True)
        (weird / "sub").mkdir()
        got = _assert_parity(str(weird / "sub"), str(tmp_path), "newline in a segment")
        assert got == str(weird), (
            "the walk did not resolve the project root under a newline-bearing "
            f"directory; got {got!r}"
        )

    def test_a_newline_path_with_no_marker_diverges_as_intended(self, tmp_path) -> None:
        """Folds the newline hazard into the no-marker-fallback divergence
        (`TestIntendedDivergences.test_no_marker_fallback_is_intended`) rather than
        asserting parity: with no marker anywhere, bash still answers "" (proving
        the newline split above did not accidentally make it answer something) and
        python's cwd tier answers (start, "cwd"), never "" for "" by coincidence.
        """
        weird = tmp_path / "no\nmarker"
        (weird / "sub").mkdir(parents=True)
        start = str(weird / "sub")

        assert _bash_detect(start, str(tmp_path)) == ""

        root, tier = resolve_project_root(start=start)
        assert (root, tier) == (start, ROOT_FROM_CWD)


# --------------------------------------------------------------------------------- #
# Anti-vacuity for the whole change.
# --------------------------------------------------------------------------------- #


class TestTheSpawnIsGone:
    def test_the_walk_no_longer_spawns_python(self) -> None:
        """Anti-vacuity for the whole change: every parity test above would still
        pass if the function kept calling python.

        Scans to the function's own closing brace rather than a fixed character
        window. The window version read 2000 characters, which spilled into the
        NEXT function once this one grew, and that neighbour legitimately spawns
        python: the test failed on a correct implementation for a reason that had
        nothing to do with it.
        """
        body = _detect_project_root_body()
        assert "python3" not in body, (
            "detect_project_root still spawns python, so the saving does not land"
        )
        assert "-e " in body, (
            "the body no longer looks like a marker walk; the extraction is probably "
            "grabbing the wrong span"
        )


class TestAntiVacuity:
    def test_assert_parity_fails_loudly_on_an_unmarked_fixture(self, tmp_path: Path) -> None:
        """THE VACUITY TRAP THE PLAN NAMES: two empty answers compare equal.
        Reddening mutation (capabilities.md): delete the `pyproject.toml` line from
        the `project` fixture, and every parity case fails loudly instead of
        passing on "" against "". Simulated directly here rather than mutating the
        shared fixture: `_assert_parity` against a deliberately unmarked directory
        must raise on the tier assertion, never silently pass on a "" == "" or
        cwd-tier-equals-cwd-tier comparison.
        """
        bare = tmp_path / "no_marker_here"
        bare.mkdir()
        with pytest.raises(AssertionError, match="marker tier"):
            _assert_parity(str(bare), str(bare), "deliberately unmarked")
