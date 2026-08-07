"""The bash project-root walk must answer exactly what the python scan answered.

Pins the capabilities.md section "The bash project-root walk answers exactly what the
python scan answered".

Why this exists: detect_project_root spawns python to walk up a directory tree checking
for six marker files. It runs twice per file write (once for the target, once for the
temp file) and again on the per-prompt path, at ~13ms of interpreter startup each. Bash
does the same walk with `[ -e ]` and `${path%/*}` for no processes at all.

THE REFERENCE IS THE IMPLEMENTATION BEING REPLACED, copied here verbatim from
bin/lib/common.sh at f8631e8 (the commit before this change). A copy is the right call
only because the original is being deleted: there will be nothing left to extract it
from, so the test carries the old behavior forward as the definition of correct.

THE HAZARD THIS FILE IS BUILT AROUND: normalization. `os.path.abspath` collapses `.`,
`..`, and repeated slashes but does NOT resolve symlinks. `realpath` DOES resolve them,
so the obvious one-line bash rewrite (`realpath -m`) can return a different project root
for a path under a symlink. That case gets its own test rather than a comment.

Per ENF-PROC-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

MARKERS = ["composer.json", "package.json", "Cargo.toml", "go.mod", "pyproject.toml", ".git"]

# Verbatim from bin/lib/common.sh at f8631e8. Do not "improve" it: it defines correct.
REFERENCE = '''
import os, sys
markers = ["composer.json","package.json","Cargo.toml","go.mod","pyproject.toml",".git"]
path = os.path.abspath(sys.argv[1])
while path != "/":
    if any(os.path.exists(os.path.join(path, m)) for m in markers):
        print(path); sys.exit(0)
    path = os.path.dirname(path)
print("")
'''


def _python_reference(start: str, cwd: str) -> str:
    return subprocess.run(
        ["python3", "-c", REFERENCE, start],
        capture_output=True, text=True, timeout=60, cwd=cwd,
    ).stdout.strip()


def _bash_impl(start: str, cwd: str) -> str:
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
    want = _python_reference(start, cwd)
    got = _bash_impl(start, cwd)
    assert got == want, f"{label}: python said {want!r}, bash said {got!r}"
    return got


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A marked project root with a nested file, the shape every caller passes."""
    root = tmp_path / "proj"
    (root / "src" / "deep").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "src" / "deep" / "mod.py").write_text("x = 1\n")
    return root


class TestParityOnRealShapes:
    def test_a_nested_file_resolves_to_its_project_root(self, project) -> None:
        got = _assert_parity(str(project / "src" / "deep" / "mod.py"), str(project), "nested file")
        assert got == str(project)

    def test_the_project_root_itself_resolves_to_itself(self, project) -> None:
        """E-PROJROOT-BUG: callers pass the root directory itself (check-gates.sh,
        writ-session-end.sh, inject-tier-workflow.sh). Returning the parent, or "",
        silently skips gate checks."""
        assert _assert_parity(str(project), str(project), "root itself") == str(project)

    def test_a_relative_path_resolves_against_the_working_directory(self, project) -> None:
        _assert_parity("src/deep/mod.py", str(project), "relative path")

    def test_a_dot_segment_is_collapsed(self, project) -> None:
        _assert_parity(str(project / "src" / "." / "deep"), str(project), "dot segment")

    def test_a_dotdot_segment_is_collapsed(self, project) -> None:
        _assert_parity(str(project / "src" / "deep" / ".." / ".."), str(project), "dotdot")

    def test_repeated_slashes_are_collapsed(self, project) -> None:
        _assert_parity(f"{project}//src///deep", str(project), "repeated slashes")

    def test_a_path_with_no_marker_anywhere_returns_empty(self, tmp_path) -> None:
        bare = tmp_path / "nomarker" / "sub"
        bare.mkdir(parents=True)
        # tmp_path has no marker above it, so both must answer "".
        assert _assert_parity(str(bare), str(tmp_path), "no marker") == ""

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


class TestTheSymlinkTrap:
    """The case that separates a correct rewrite from a plausible one."""

    def test_a_symlinked_path_is_not_resolved(self, tmp_path) -> None:
        """`abspath` does NOT resolve symlinks, so the walk goes up the LINK's parents,
        not the target's. A `realpath`-based rewrite would return the other project.

        Layout: two marked projects. `real/` holds the file; `link/` is a symlink to it
        placed inside a DIFFERENT marked project. Walking the link path must find the
        linking project, because that is what abspath + dirname does.
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
            "the walk resolved the symlink; abspath does not, so this must find the "
            f"linking project {other}, not the link target's project {real}"
        )


class TestNewlineInAPathSegment:
    """A directory name may contain a newline on Linux, and that used to break the walk.

    Found by review. The split was `IFS='/' read -r -a raw <<< "$path"`, and `read`
    consumes only the FIRST LINE of a here-string, so every segment after the newline
    was dropped and the walk returned "". Callers gate on an empty PROJECT_ROOT and skip
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

    def test_a_newline_path_with_no_marker_still_returns_empty(self, tmp_path) -> None:
        """Anti-vacuity: the fix must not make the walk answer for paths that have no
        marker, which would be a different bug wearing the same result."""
        weird = tmp_path / "no\nmarker"
        (weird / "sub").mkdir(parents=True)
        assert _assert_parity(str(weird / "sub"), str(tmp_path), "newline, no marker") == ""


class TestTheSpawnIsGone:
    def test_the_walk_no_longer_spawns_python(self) -> None:
        """Anti-vacuity for the whole change: every parity test above would still pass
        if the function kept calling python.

        Scans to the function's own closing brace rather than a fixed character window.
        The window version read 2000 characters, which spilled into the NEXT function
        once this one grew, and that neighbour legitimately spawns python: the test
        failed on a correct implementation for a reason that had nothing to do with it.
        """
        src = COMMON_SH.read_text()
        start = src.index("detect_project_root() {")
        end = src.index("\n}\n", start)
        body = src[start:end]
        assert "python3" not in body, (
            "detect_project_root still spawns python, so the saving does not land"
        )
        assert "-e " in body, (
            "the body no longer looks like a marker walk; the extraction is probably "
            "grabbing the wrong span"
        )

    def test_the_reference_and_the_implementation_disagree_about_nothing(
        self, project
    ) -> None:
        """Guards the guard: if the reference itself stopped running (bad -c source,
        missing python), every parity assertion would compare "" to "" and pass."""
        assert _python_reference(str(project / "src"), str(project)) == str(project), (
            "the python reference produced no answer, so the parity tests are vacuous"
        )
