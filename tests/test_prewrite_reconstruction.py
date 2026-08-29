"""Tier 1 (plan.md `dfacff61-23d5-474e-846c-2e2f0f0ea482`): the `replace_all`
reconstruction fix, and the control that decides whether the post-write analyzer
run may ever be dropped as a duplicate of the pre-write one.

TWO INDEPENDENT THINGS LIVE HERE, and neither substitutes for the other:

1. `TestReplaceAllReconstructionShapes` pins the fix AT THE RECONSTRUCTION LEVEL.
   `hooks/scripts/pre-validate-file.sh` builds its pre-write temp file from an inline
   python heredoc, not a standalone module, so this extracts that heredoc's REAL
   source text verbatim (never a hand copy that could drift from the fix) and runs
   it as a subprocess against a controlled `old_string`/`new_string`/`replace_all`
   payload. `_reconstruction_snippet()` fails loudly if the surrounding bash
   structure it anchors on ever moves, rather than silently testing nothing.

2. `TestPathSensitivityControl` is THE MOST IMPORTANT TEST IN THIS CYCLE: it can
   REFUTE the plan's central claim, not just illustrate it. The claim is that the
   pre-write run (on a temp file under /tmp) and the post-write run (on the target
   path) are DIFFERENT COMPUTATIONS, because `bin/run-analysis.sh`'s language
   analyzers resolve their own configuration from the analysed file's own
   directory, which is why the post-write run stays and is not removed as a
   duplicate. Both halves are asserted for real:
     - no linter config anywhere -> identical bytes at a project path and a /tmp
       path AGREE (the byte claim; already confirmed, not re-litigated here).
     - a `ruff.toml` that changes the verdict -> the SAME two runs DISAGREE (the
       computation claim). If this second half ever comes out equal, the plan's
       premise is REFUTED, and the assertion message says so explicitly.
   Both tests in that class skip when `ruff` is not available, per this suite's
   existing skip convention (`shutil.which(...) is None` + a reason naming the
   missing tool).

Run:
  .venv/bin/python -m pytest tests/test_prewrite_reconstruction.py -v

RED PHASE: `TestReplaceAllReconstructionShapes.test_replace_all_true_replaces_
every_occurrence` fails today (measured: the current hardcoded count of `1`
reconstructs `ZXaXa`, not `ZXZXZ`). The `false` and `absent` shapes already pass
today by construction (ABSENT IS NOT EMPTY: neither one is `is True`) and must
keep passing after the fix. `TestPathSensitivityControl`'s two tests pin an
ALREADY-TRUE fact about this machine's `bin/run-analysis.sh` and `ruff`; they are
not gated on any production change in this plan and should already pass.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PRE_VALIDATE_FILE = REPO / "hooks" / "scripts" / "pre-validate-file.sh"
RUN_ANALYSIS = REPO / "bin" / "run-analysis.sh"

# A single unused import: the smallest source that trips ruff's F401 (unused-import),
# with no other analyzer (php/js/xml/rust/go/graphql analyzers do not run on .py, and
# the cross-language regex scanners do not fire on this content, measured directly
# against this exact string before this test was written).
_UNUSED_IMPORT_SOURCE = "import os\n"


def _find_ruff() -> str | None:
    """`ruff` resolved from PATH, falling back to this checkout's own venv.

    The venv fallback matters because the suite's own run command
    (`.venv/bin/python -m pytest ...`) does not itself put `.venv/bin` on PATH, and
    `bin/run-analysis.sh` shells out to a bare `ruff`, exactly the fact
    `tests/firedrill/_harness.py::build_env` already works around for hook
    subprocesses. This module is not part of the firedrill and builds its own env.
    """
    found = shutil.which("ruff")
    if found:
        return found
    venv_ruff = REPO / ".venv" / "bin" / "ruff"
    return str(venv_ruff) if venv_ruff.is_file() else None


RUFF = _find_ruff()


def _env_with_ruff_on_path() -> dict:
    env = dict(os.environ)
    ruff_dir = str(Path(RUFF).resolve().parent)
    env["PATH"] = f"{ruff_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_analysis(project_root: Path, file_path: Path) -> list[dict]:
    """Invoke the REAL `bin/run-analysis.sh` (never stubbed, per ENF-SYS-005) exactly as
    both `pre-validate-file.sh` and `validate-file.sh` do: `--project-root <target's
    project root> <file>`. Returns the parsed JSON findings array."""
    result = subprocess.run(
        ["bash", str(RUN_ANALYSIS), "--project-root", str(project_root), str(file_path)],
        capture_output=True,
        text=True,
        env=_env_with_ruff_on_path(),
        timeout=30,
    )
    try:
        findings = json.loads(result.stdout)
    except json.JSONDecodeError:
        pytest.fail(
            f"bin/run-analysis.sh did not print a JSON array: stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )
    assert isinstance(findings, list), (
        f"bin/run-analysis.sh's output parsed but was not a list: {findings!r}"
    )
    return findings


def _findings_modulo_file(findings: list[dict]) -> frozenset:
    """The findings set with the `file` field stripped, so two runs against
    different paths compare on everything ELSE a finding carries (line, severity,
    rule, tool, message)."""
    return frozenset(
        tuple(sorted((k, v) for k, v in f.items() if k != "file"))
        for f in findings
    )


def _has_f401(findings: list[dict]) -> bool:
    return any("F401" in str(f.get("tool", "")) for f in findings)


@pytest.mark.skipif(
    RUFF is None,
    reason="ruff not found on PATH or in .venv/bin; cannot run the analyzer "
    "path-sensitivity control",
)
class TestPathSensitivityControl:
    """THE CONTROL ON THIS PLAN'S OWN CENTRAL CLAIM (plan.md's Analysis, Tier 1).
    Both halves are asserted; the second one is written so it can genuinely fail."""

    def test_identical_bytes_agree_when_the_project_declares_no_linter_config(
        self, tmp_path
    ) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        project_file = project_dir / "thing.py"
        project_file.write_text(_UNUSED_IMPORT_SOURCE)

        tmp_copy_dir = Path(tempfile.mkdtemp(prefix="claude-preval-test-"))
        tmp_copy = tmp_copy_dir / "thing.py"
        tmp_copy.write_text(_UNUSED_IMPORT_SOURCE)
        try:
            project_findings = _run_analysis(project_dir, project_file)
            tmp_findings = _run_analysis(project_dir, tmp_copy)

            assert _has_f401(project_findings), (
                "precondition failed: the project-path run reported no F401 for an "
                f"unused import, so the agreement check below would be vacuous: "
                f"{project_findings!r}"
            )

            project_set = _findings_modulo_file(project_findings)
            tmp_set = _findings_modulo_file(tmp_findings)
            assert project_set == tmp_set, (
                "identical bytes at a project path and a /tmp path disagreed with NO "
                f"linter config present anywhere: project={project_findings!r} "
                f"tmp={tmp_findings!r}"
            )
        finally:
            shutil.rmtree(tmp_copy_dir, ignore_errors=True)

    def test_identical_bytes_disagree_once_a_ruff_toml_changes_the_verdict(
        self, tmp_path
    ) -> None:
        """If this ever passes with `project_set == tmp_set`, the plan's premise --
        that the post-write run is the only one that sees a project's own linter
        configuration, is REFUTED on this machine. Do not respond to that by
        building the skip anyway: stop, and return the measured result to the user
        before touching `pre-validate-file.sh` or `validate-file.sh` on that basis.
        """
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        project_file = project_dir / "thing.py"
        project_file.write_text(_UNUSED_IMPORT_SOURCE)
        (project_dir / "ruff.toml").write_text('[lint]\nignore = ["F401"]\n')

        tmp_copy_dir = Path(tempfile.mkdtemp(prefix="claude-preval-test-"))
        tmp_copy = tmp_copy_dir / "thing.py"
        tmp_copy.write_text(_UNUSED_IMPORT_SOURCE)
        try:
            project_findings = _run_analysis(project_dir, project_file)
            tmp_findings = _run_analysis(project_dir, tmp_copy)

            assert not _has_f401(project_findings), (
                "precondition failed: the project's own ruff.toml must silence F401 "
                f"at the project path for this control to mean anything: "
                f"{project_findings!r}"
            )
            assert _has_f401(tmp_findings), (
                "precondition failed: the /tmp copy has no ruff.toml reachable from "
                f"its own directory, so it must still report F401: {tmp_findings!r}"
            )

            project_set = _findings_modulo_file(project_findings)
            tmp_set = _findings_modulo_file(tmp_findings)
            assert project_set != tmp_set, (
                "REFUTATION: identical bytes at a project path and a /tmp path "
                "returned the SAME findings even though the project's own ruff.toml "
                "changes the verdict. The plan's central claim, that each language "
                "analyzer resolves its configuration from the analysed file's own "
                "directory, so the pre-write and post-write runs are different "
                "COMPUTATIONS and not just different bytes, is FALSE on this "
                "machine. STOP HERE: do not build the Tier 1 skip; return this "
                "result to the user before touching hooks/scripts/pre-validate-"
                f"file.sh or hooks/scripts/validate-file.sh. project={project_findings!r} "
                f"tmp={tmp_findings!r}"
            )
        finally:
            shutil.rmtree(tmp_copy_dir, ignore_errors=True)


def _reconstruction_snippet() -> str:
    """The ACTUAL python source `pre-validate-file.sh` embeds to build its pre-write
    temp file, extracted verbatim from the real script (never hand-copied), so this
    test tracks the real fix rather than a paraphrase of it. Fails loudly if the
    surrounding bash wrapper it anchors on has moved, instead of silently matching
    nothing and testing an empty string."""
    text = PRE_VALIDATE_FILE.read_text(encoding="utf-8")
    match = re.search(
        r'python3 -c "\n(.*?)\n" 2>/dev/null\)\n\nif \[ -z "\$TMPFILE"', text, re.S
    )
    if not match:
        pytest.fail(
            "could not locate the TMPFILE reconstruction heredoc in "
            f"{PRE_VALIDATE_FILE}; its surrounding bash structure changed and this "
            "test's extraction regex needs updating alongside it"
        )
    return match.group(1)


_ORIGINAL_CONTENT = "aXaXa"


def _reconstruct(
    tmp_path: Path,
    original: str,
    old_string: str,
    new_string: str,
    *,
    include_replace_all: bool,
    replace_all: bool = False,
) -> str | None:
    """Feed the extracted snippet a real `HOOK_ENVELOPE`-shaped payload on stdin (as
    `load_hook_env` would have built it: top-level `file_path`, and the full
    `tool_input` dict passed through) and return the reconstructed content, or None
    if the snippet produced no temp file at all."""
    target = tmp_path / "target.txt"
    target.write_text(original)
    tool_input = {"old_string": old_string, "new_string": new_string}
    if include_replace_all:
        tool_input["replace_all"] = replace_all
    envelope = {"file_path": str(target), "tool_input": tool_input}
    result = subprocess.run(
        ["python3", "-c", _reconstruction_snippet()],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        timeout=15,
    )
    produced = result.stdout.strip()
    if not produced:
        return None
    produced_path = Path(produced)
    content = produced_path.read_text()
    produced_path.unlink(missing_ok=True)
    return content


class TestReplaceAllReconstructionShapes:
    """Pins the fix at the level the plan calls for explicitly: a unit test on the
    exact reconstruction expression, run as a real subprocess against the REAL
    source text (Decision: "A unit test pins the three shapes at the reconstruction
    level")."""

    def test_replace_all_true_replaces_every_occurrence(self, tmp_path) -> None:
        content = _reconstruct(
            tmp_path, _ORIGINAL_CONTENT, "a", "Z",
            include_replace_all=True, replace_all=True,
        )
        assert content == "ZXZXZ", (
            f"replace_all: true must reconstruct with every occurrence replaced, "
            f"got {content!r}"
        )

    def test_replace_all_false_replaces_only_the_first_occurrence(self, tmp_path) -> None:
        content = _reconstruct(
            tmp_path, _ORIGINAL_CONTENT, "a", "Z",
            include_replace_all=True, replace_all=False,
        )
        assert content == "ZXaXa", (
            f"replace_all: false must keep today's single-occurrence reconstruction, "
            f"got {content!r}"
        )

    def test_replace_all_absent_replaces_only_the_first_occurrence(self, tmp_path) -> None:
        """ABSENT IS NOT EMPTY: no `replace_all` key at all must behave exactly like
        `replace_all: false`, never like `replace_all: true`."""
        content = _reconstruct(
            tmp_path, _ORIGINAL_CONTENT, "a", "Z",
            include_replace_all=False,
        )
        assert content == "ZXaXa", (
            f"an absent replace_all key must keep the single-occurrence "
            f"reconstruction, got {content!r}"
        )
