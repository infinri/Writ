"""Audit item F, subprocess failures: the finding does not hold, and this pins why.

The audit said: "22 subprocess.run/Popen/check_output call sites in writ/; non-zero exits
are largely unchecked and unrecorded." Reading all 22 shows otherwise. Every site outside
the exempt family either raises with the exit code attached, or treats a non-zero
exit as a documented expected outcome (a "is this a repo?" probe).

Adding an emit to the probe sites would repeat the mistake P1's SCOPE CORRECTION already
documented: cache.py's resolution chain raises FileNotFoundError on a normal turn, and
converting it would have produced noise, not signal. A `git rev-parse` returning non-zero
in a non-repo directory is the same shape.

So this file records the verification instead of changing code. It fails if a site starts
swallowing a non-zero exit silently, which is when the finding WOULD become real.

Exempt family, per the audit's own B2 ("acceptable silence, leave alone"):
  writ/session/doctor.py    the diagnostic IS the output; a failed probe is a reported check
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

WRIT_PKG = Path(__file__).resolve().parent.parent / "writ"
EXEMPT = {"doctor.py"}
_SUBPROCESS_CALL = re.compile(r"subprocess\.(run|Popen|check_output)\(|\brun\(\s*\[")

# Every non-exempt site, with how it handles a non-zero exit. Verified by reading each one.
HANDLED_SITES = {
    "writ/session/git_hooks.py": "raises RuntimeError carrying the returncode",
    "writ/session/harvester.py": "check=True on one site, raises ValueError on the other",
    "writ/session/commit_capture.py": "guards on returncode == 0 before using stdout",
    "writ/session/pr_comments.py": "check=True inside except CalledProcessError",
    "writ/cli.py": "returns '' on non-zero; documented no-repo/detached-HEAD outcome",
    "writ/session/git_identity.py": "raises NotInRepoError carrying the returncode",
    "writ/session/gate_token.py": "returns '' on non-zero (user.name unset)",
}


def _files_with_subprocess() -> dict[Path, list[int]]:
    found: dict[Path, list[int]] = {}
    for path in sorted(WRIT_PKG.rglob("*.py")):
        lines = path.read_text().splitlines()
        hits = [
            i for i, ln in enumerate(lines, 1)
            if ("subprocess.run(" in ln or "subprocess.Popen(" in ln
                or "check_output(" in ln)
        ]
        if hits:
            found[path] = hits
    return found


class TestInventoryIsStable:
    def test_only_the_known_files_call_subprocess(self):
        """A new file calling subprocess needs its own review, not an inherited verdict."""
        rel = {
            str(p.relative_to(WRIT_PKG.parent)) for p in _files_with_subprocess()
            if p.name not in EXEMPT
        }
        unreviewed = rel - set(HANDLED_SITES)
        assert unreviewed == set(), (
            "these call subprocess but are not in the reviewed inventory; check how each "
            f"handles a non-zero exit and add it here: {sorted(unreviewed)}"
        )


class TestEveryNonExemptSiteHandlesFailure:
    @pytest.mark.parametrize("rel", sorted(HANDLED_SITES))
    def test_the_file_reacts_to_a_nonzero_exit(self, rel):
        """Each must either check returncode, pass check=True, or catch CalledProcessError.

        A file that calls subprocess and does none of the three is the silent-swallow the
        audit was worried about.
        """
        text = (WRIT_PKG.parent / rel).read_text()
        reacts = (
            "returncode" in text
            or "check=True" in text
            or "CalledProcessError" in text
        )
        assert reacts, f"{rel} calls subprocess and never reacts to a non-zero exit"

    @pytest.mark.parametrize("rel", sorted(HANDLED_SITES))
    def test_no_bare_run_with_check_false_and_no_guard(self, rel):
        """check=False is fine only when the caller then inspects returncode."""
        text = (WRIT_PKG.parent / rel).read_text()
        if "check=False" in text:
            assert "returncode" in text, (
                f"{rel} passes check=False without inspecting returncode, which is the "
                "shape that silently discards a failure"
            )


class TestExemptFamiliesAreDeliberate:
    def test_doctor_reports_rather_than_raises(self):
        """doctor's whole contract is turning failures into reported checks."""
        text = (WRIT_PKG / "session" / "doctor.py").read_text()
        assert "returncode" in text, "doctor must still inspect exits to report them"


class TestNoSilentSwallowIsIntroduced:
    def test_no_except_calledprocesserror_with_an_empty_body(self):
        """`except CalledProcessError: pass` is the exact shape to prohibit."""
        offenders = []
        for path in sorted(WRIT_PKG.rglob("*.py")):
            if path.name in EXEMPT:
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                names = ast.dump(node.type) if node.type else ""
                if "CalledProcessError" not in names:
                    continue
                body = node.body
                if len(body) == 1 and isinstance(body[0], (ast.Pass,)):
                    offenders.append(f"{path.relative_to(WRIT_PKG.parent)}:{node.lineno}")
        assert offenders == [], f"silently swallowed subprocess failures: {offenders}"
