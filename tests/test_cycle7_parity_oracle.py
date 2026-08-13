"""Cycle 7, defect 1: `writ validate` skips four parity detectors by default,
then prints "All checks passed." (writ/cli.py:568-572 vs. prune/reconcile's
Path(DEFAULT_BIBLE_DIR) default; the four falsy-on-no-oracle guards at
writ/graph/integrity/parity_checks.py:138-141,203-206,238-241,281-284).

Pins six capabilities of the fix described in plan.md's "Defect 1" section:
  1. validate's --bible-dir defaults to Path(DEFAULT_BIBLE_DIR).
  2. resolve_parity_oracle's three skip reasons + the usable-oracle return.
  3. run_all_checks does NOT call the four detectors on the skip path (the
     load-bearing capability -- see TestSkipPathNeverCallsParityDetectors).
  4. parity_checks_skipped names the reason + the four checks and is
     non-gating.
  5. with a usable oracle the four detectors DO run, and a finding from any
     one of them still flips exit_code to 1 (and a clean run keeps it 0).
  6. render_findings emits exactly one skip line, placed before the parity
     sections.

No live Neo4j anywhere: every IntegrityChecker here is constructed with
driver=None (the same pattern tests/test_integrity.py::TestRunAllChecksPhase0
already uses), and the oracle-resolution tests use tmp_path directories.
resolve_parity_oracle does not exist yet, so it is imported inside each test
that needs it rather than at module scope, so the rest of this file still
collects.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from writ.cli import DEFAULT_BIBLE_DIR, validate
from writ.graph.integrity import IntegrityChecker
from writ.graph.validate_report import render_findings

# Mirrors integrity/__init__.py's planned _PARITY_CHECK_KEYS: the four finding
# keys the skip path sets directly and the four detectors it must not call.
_PARITY_CHECK_KEYS = (
    "parity_violations",
    "edge_parity",
    "prop_parity",
    "methodology_field_drift",
)


def _base_patches() -> dict[str, AsyncMock]:
    """AsyncMock stand-ins for the run_all_checks detectors that have NO
    `self._driver is None` guard (structural_checks.py, frequency_checks.py).
    Every other detector run_all_checks calls DOES guard driver=None and
    returns its falsy default without opening a session, so
    IntegrityChecker(None, None) plus these ten patches covers the whole
    call graph with no Neo4j -- verified against the live run_all_checks by
    tests/test_integrity.py::TestRunAllChecksPhase0, which uses this exact
    set.
    """
    return {
        "detect_conflicts": AsyncMock(return_value=[]),
        "detect_orphans": AsyncMock(return_value=[]),
        "detect_stale": AsyncMock(return_value=[]),
        "detect_redundant": AsyncMock(return_value=[]),
        "check_unreviewed_count": AsyncMock(return_value=None),
        "detect_frequency_stale": AsyncMock(return_value=[]),
        "detect_graduation_flags": AsyncMock(return_value=[]),
        "detect_dangling_dispatched_roles": AsyncMock(return_value=[]),
        "detect_orphans_all_labels": AsyncMock(return_value=([], {})),
        "detect_category_reachability": AsyncMock(
            return_value={"skipped": True, "reason": "no categories"}
        ),
    }


async def _run_all_checks_with(
    checker: IntegrityChecker, patches: dict[str, AsyncMock], **kwargs
) -> dict:
    """Apply `patches` onto `checker` as instance-attribute overrides for the
    duration of exactly one run_all_checks call."""
    cms = [patch.object(checker, name, mock) for name, mock in patches.items()]
    for cm in cms:
        cm.start()
    try:
        return await checker.run_all_checks(**kwargs)
    finally:
        for cm in cms:
            cm.stop()


@pytest.fixture()
def checker() -> IntegrityChecker:
    return IntegrityChecker(None, None)


class TestValidateBibleDirDefault:
    """Cap 1: --bible-dir on `validate` defaults to Path(DEFAULT_BIBLE_DIR),
    the same default `prune` (cli.py:478-482) and `reconcile` (cli.py:513-517)
    already declare, instead of None."""

    def test_bible_dir_option_default_is_default_bible_dir_path(self) -> None:
        raw_default = inspect.signature(validate).parameters["bible_dir"].default
        # typer.Option(...) installs an OptionInfo object as the function's
        # parameter default; the value passed on the CLI when the flag is
        # omitted is OptionInfo.default.
        resolved_default = getattr(raw_default, "default", raw_default)
        assert resolved_default == Path(DEFAULT_BIBLE_DIR), (
            "validate's --bible-dir must default to Path(DEFAULT_BIBLE_DIR), "
            f"like prune/reconcile; got {resolved_default!r}"
        )


class TestResolveParityOracle:
    """Cap 2: resolve_parity_oracle's three skip reasons and its one usable
    return, plus both directions of the bible_dir/default_bible_dir
    precedence its two-argument signature implies."""

    def test_no_bible_dir_and_no_default_returns_skip_reason(self) -> None:
        from writ.graph.integrity._common import resolve_parity_oracle

        resolved, reason = resolve_parity_oracle(None, None)

        assert resolved is None
        assert reason == "no markdown corpus configured (pass --bible-dir)"

    def test_nonexistent_directory_returns_skip_reason(self, tmp_path: Path) -> None:
        from writ.graph.integrity._common import resolve_parity_oracle

        missing = tmp_path / "does-not-exist"

        resolved, reason = resolve_parity_oracle(missing, None)

        assert resolved is None
        assert reason == f"markdown corpus {missing} does not exist"

    def test_directory_with_no_markdown_files_returns_skip_reason(
        self, tmp_path: Path
    ) -> None:
        from writ.graph.integrity._common import resolve_parity_oracle

        (tmp_path / "notes.txt").write_text("not markdown")

        resolved, reason = resolve_parity_oracle(tmp_path, None)

        assert resolved is None
        assert reason == f"markdown corpus {tmp_path} contains no *.md files"

    def test_directory_with_a_markdown_file_returns_path_and_no_reason(
        self, tmp_path: Path
    ) -> None:
        from writ.graph.integrity._common import resolve_parity_oracle

        (tmp_path / "doc.md").write_text("# doc")

        resolved, reason = resolve_parity_oracle(tmp_path, None)

        assert resolved == tmp_path
        assert reason is None

    def test_explicit_bible_dir_takes_precedence_over_default(
        self, tmp_path: Path
    ) -> None:
        from writ.graph.integrity._common import resolve_parity_oracle

        explicit = tmp_path / "explicit"
        explicit.mkdir()
        (explicit / "doc.md").write_text("# doc")
        default = tmp_path / "default-unused"  # never created; must not be read

        resolved, reason = resolve_parity_oracle(explicit, default)

        assert resolved == explicit
        assert reason is None

    def test_falls_back_to_default_when_bible_dir_is_none(
        self, tmp_path: Path
    ) -> None:
        from writ.graph.integrity._common import resolve_parity_oracle

        (tmp_path / "doc.md").write_text("# doc")

        resolved, reason = resolve_parity_oracle(None, tmp_path)

        assert resolved == tmp_path
        assert reason is None


class TestSkipPathNeverCallsParityDetectors:
    """Cap 3, the load-bearing one: on the skip path, run_all_checks sets the
    four parity keys directly and must NOT invoke the detectors at all --
    not merely receive a falsy result from them. Falsy is exactly what a
    silently-skipped check returns today, so an assertion on falsiness alone
    would PASS against the broken code and prove nothing. The four detector
    methods are call-recording AsyncMock doubles seeded with TRUTHY return
    values on purpose: if the skip path awaited them anyway, both the
    call-count assertions AND the falsy-default assertions below would catch
    it.
    """

    @staticmethod
    def _truthy_parity_doubles() -> dict[str, AsyncMock]:
        return {
            "detect_parity_violations": AsyncMock(
                return_value=[{"type": "Rule", "id": "SHOULD-NOT-RUN"}]
            ),
            "detect_edge_parity": AsyncMock(
                return_value={"stale": [("ABSTRACTS", "a", "b")], "missing": []}
            ),
            "detect_prop_parity": AsyncMock(return_value={"SOME-NODE": ["p"]}),
            "detect_methodology_field_drift": AsyncMock(
                return_value={"SOME-NODE": {"f": {"graph": 1, "source": 2}}}
            ),
        }

    @pytest.mark.asyncio
    async def test_no_oracle_skip_path_never_awaits_the_four_detectors(
        self, checker: IntegrityChecker
    ) -> None:
        parity_doubles = self._truthy_parity_doubles()
        patches = _base_patches()
        patches.update(parity_doubles)

        await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=None
        )

        for name, mock in parity_doubles.items():
            assert mock.await_count == 0, (
                f"{name} must not be awaited when the oracle resolves to a "
                f"skip; it was awaited {mock.await_count} time(s)"
            )

    @pytest.mark.asyncio
    async def test_no_oracle_skip_path_sets_the_four_falsy_defaults_directly(
        self, checker: IntegrityChecker
    ) -> None:
        patches = _base_patches()
        patches.update(self._truthy_parity_doubles())

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=None
        )

        assert findings["parity_violations"] == []
        assert findings["edge_parity"] is None
        assert findings["prop_parity"] is None
        assert findings["methodology_field_drift"] is None


class TestParityChecksSkippedNonGating:
    """Cap 4: parity_checks_skipped names the reason and the four skipped
    checks, and is non-gating -- a skipped run with no other findings still
    exits 0."""

    @pytest.mark.asyncio
    async def test_skip_dict_names_reason_and_the_four_checks(
        self, checker: IntegrityChecker
    ) -> None:
        patches = _base_patches()

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=None
        )

        skipped = findings.get("parity_checks_skipped")
        assert skipped, "parity_checks_skipped must be populated on the skip path"
        assert skipped["reason"] == "no markdown corpus configured (pass --bible-dir)"
        assert skipped["checks"] == list(_PARITY_CHECK_KEYS)

    @pytest.mark.asyncio
    async def test_skip_with_no_other_findings_still_exits_zero(
        self, checker: IntegrityChecker
    ) -> None:
        patches = _base_patches()

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=None
        )

        assert findings.get("parity_checks_skipped"), (
            "sanity: this run must be on the skip path"
        )
        assert findings["exit_code"] == 0, (
            "a skip with no other findings is an honest report, not a "
            f"failure; got exit_code={findings['exit_code']}"
        )


class TestUsableOracleWiring:
    """Cap 5: with a usable oracle, run_all_checks calls all four parity
    detectors (and marks the run as not-skipped); a non-empty result from
    ANY of them still flips exit_code to 1, and an all-clean run keeps it 0
    (the converse direction -- the non-gating change in cap 4 must not make
    the usable-oracle path non-gating too)."""

    @staticmethod
    def _usable_bible_dir(tmp_path: Path) -> Path:
        (tmp_path / "doc.md").write_text("# doc")
        return tmp_path

    @pytest.mark.asyncio
    async def test_usable_oracle_awaits_all_four_and_marks_not_skipped(
        self, checker: IntegrityChecker, tmp_path: Path
    ) -> None:
        bible_dir = self._usable_bible_dir(tmp_path)
        parity_doubles = {
            "detect_parity_violations": AsyncMock(return_value=[]),
            "detect_edge_parity": AsyncMock(return_value=None),
            "detect_prop_parity": AsyncMock(return_value=None),
            "detect_methodology_field_drift": AsyncMock(return_value=None),
        }
        patches = _base_patches()
        patches.update(parity_doubles)

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=bible_dir
        )

        for name, mock in parity_doubles.items():
            assert mock.await_count == 1, (
                f"{name} must run exactly once against a usable oracle; "
                f"awaited {mock.await_count} time(s)"
            )
        assert "parity_checks_skipped" in findings, (
            "run_all_checks must explicitly set parity_checks_skipped (to "
            "None) on the usable-oracle path, not leave the key unset"
        )
        assert findings["parity_checks_skipped"] is None

    @pytest.mark.asyncio
    async def test_usable_oracle_all_clean_keeps_exit_code_zero(
        self, checker: IntegrityChecker, tmp_path: Path
    ) -> None:
        bible_dir = self._usable_bible_dir(tmp_path)
        patches = _base_patches()
        patches.update({
            "detect_parity_violations": AsyncMock(return_value=[]),
            "detect_edge_parity": AsyncMock(return_value=None),
            "detect_prop_parity": AsyncMock(return_value=None),
            "detect_methodology_field_drift": AsyncMock(return_value=None),
        })

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=bible_dir
        )

        assert "parity_checks_skipped" in findings
        assert findings["parity_checks_skipped"] is None
        assert findings["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_usable_oracle_parity_violation_flips_exit_code_to_one(
        self, checker: IntegrityChecker, tmp_path: Path
    ) -> None:
        bible_dir = self._usable_bible_dir(tmp_path)
        patches = _base_patches()
        patches.update({
            "detect_parity_violations": AsyncMock(
                return_value=[{"type": "Rule", "id": "ORPHAN-001"}]
            ),
            "detect_edge_parity": AsyncMock(return_value=None),
            "detect_prop_parity": AsyncMock(return_value=None),
            "detect_methodology_field_drift": AsyncMock(return_value=None),
        })

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=bible_dir
        )

        assert "parity_checks_skipped" in findings
        assert findings["parity_checks_skipped"] is None
        assert findings["exit_code"] == 1, (
            "a non-empty detect_parity_violations result must flip exit_code "
            f"to 1; got {findings['exit_code']}"
        )

    @pytest.mark.asyncio
    async def test_usable_oracle_edge_parity_finding_flips_exit_code_to_one(
        self, checker: IntegrityChecker, tmp_path: Path
    ) -> None:
        bible_dir = self._usable_bible_dir(tmp_path)
        patches = _base_patches()
        patches.update({
            "detect_parity_violations": AsyncMock(return_value=[]),
            "detect_edge_parity": AsyncMock(
                return_value={"stale": [("ABSTRACTS", "a", "b")], "missing": []}
            ),
            "detect_prop_parity": AsyncMock(return_value=None),
            "detect_methodology_field_drift": AsyncMock(return_value=None),
        })

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True, bible_dir=bible_dir
        )

        assert "parity_checks_skipped" in findings
        assert findings["parity_checks_skipped"] is None
        assert findings["exit_code"] == 1, (
            "a non-empty detect_edge_parity result must flip exit_code to 1; "
            f"got {findings['exit_code']}"
        )


class TestRenderParityChecksSkipped:
    """Cap 6: render_findings emits exactly one "Parity checks skipped" line,
    naming the reason and the four checks, immediately before the parity
    sections."""

    def test_renders_the_exact_skip_line(self) -> None:
        reason = "no markdown corpus configured (pass --bible-dir)"
        checks = list(_PARITY_CHECK_KEYS)
        findings = {
            "parity_checks_skipped": {"reason": reason, "checks": checks},
        }

        stdout_lines, _stderr_lines = render_findings(findings)

        expected = (
            f"\nParity checks skipped: {reason} "
            f"({', '.join(checks)} did not run)"
        )
        assert expected in stdout_lines, (
            f"expected line {expected!r} not found in {stdout_lines!r}"
        )
        assert stdout_lines.count(expected) == 1, (
            f"expected exactly one skip line; got {stdout_lines.count(expected)}"
        )

    def test_skip_line_appears_immediately_before_the_parity_violations_section(
        self,
    ) -> None:
        # A pure ordering pin on _RENDER_STEPS: force both blocks to render in
        # the same call so their relative position in stdout_lines is
        # observable. (In production the two never co-occur -- the skip path
        # sets parity_violations to its falsy default -- but render-step
        # ORDER is a property of _RENDER_STEPS, not of any one findings
        # dict.)
        findings = {
            "parity_checks_skipped": {
                "reason": "no markdown corpus configured (pass --bible-dir)",
                "checks": list(_PARITY_CHECK_KEYS),
            },
            "parity_violations": [{"type": "Rule", "id": "RULE-A-001"}],
        }

        stdout_lines, _stderr_lines = render_findings(findings)

        skip_indices = [
            i for i, line in enumerate(stdout_lines)
            if line.startswith("\nParity checks skipped:")
        ]
        violation_indices = [
            i for i, line in enumerate(stdout_lines)
            if line.startswith("\nParity violations")
        ]
        assert len(skip_indices) == 1, (
            f"expected exactly one skip line; got {skip_indices!r}"
        )
        assert len(violation_indices) == 1, (
            f"expected exactly one parity-violations header; got "
            f"{violation_indices!r}"
        )
        assert skip_indices[0] < violation_indices[0], (
            "the skip line must render BEFORE the parity violations section; "
            f"skip at {skip_indices[0]}, violations at {violation_indices[0]}"
        )


class TestArtifactAbstractsParityIsGating:
    """Review follow-up (cycle 7): the gating property of
    `artifact_abstracts_parity` was correct only BY CONSTRUCTION -- it is
    absent from `_NON_GATING`, so the generic truthy sweep picks it up.

    Nothing asserted it. That is the wrong shape for this particular check:
    it is the one that BOUGHT BACK the coverage the oracle-blind exemption
    gave up, so if a later edit added it to `_NON_GATING` the corpus would
    lose its only guard on the ABSTRACTS edge set and every existing test
    would still pass. Assert the property directly instead of inferring it
    from a set membership.
    """

    @staticmethod
    def _usable_bible_dir(tmp_path: Path) -> Path:
        (tmp_path / "doc.md").write_text("# doc")
        return tmp_path

    @staticmethod
    def _patches_with_clean_parity() -> dict[str, AsyncMock]:
        """_base_patches() plus the four parity detectors.

        A usable oracle makes run_all_checks actually CALL them, and
        detect_parity_violations reaches get_all_nodes
        (parity_checks.py:35), which dereferences self._driver with no
        None guard. Patching them clean isolates the exit-code claim to
        artifact_abstracts_parity alone.
        """
        patches = _base_patches()
        patches.update({
            "detect_parity_violations": AsyncMock(return_value=[]),
            "detect_edge_parity": AsyncMock(return_value=None),
            "detect_prop_parity": AsyncMock(return_value=None),
            "detect_methodology_field_drift": AsyncMock(return_value=None),
        })
        return patches

    _FINDING = {
        "stale": [("ABS-FAKE-001", "RULE-FAKE-001")],
        "missing": [("ABS-FAKE-002", "RULE-FAKE-002")],
    }

    @pytest.mark.asyncio
    async def test_a_finding_flips_exit_code_to_one(
        self, checker: IntegrityChecker, tmp_path: Path
    ) -> None:
        patches = self._patches_with_clean_parity()
        patches["detect_artifact_abstracts_parity"] = AsyncMock(
            return_value=self._FINDING
        )

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True,
            bible_dir=self._usable_bible_dir(tmp_path),
        )

        assert findings["artifact_abstracts_parity"] == self._FINDING
        assert findings["exit_code"] == 1, (
            "a non-None artifact_abstracts_parity result must set exit_code=1; "
            f"got {findings['exit_code']}. If this regressed, check whether the "
            "key was added to _NON_GATING"
        )

    @pytest.mark.asyncio
    async def test_no_finding_keeps_exit_code_zero(
        self, checker: IntegrityChecker, tmp_path: Path
    ) -> None:
        """The control. Without it the test above would also pass against a
        build where EVERYTHING is gating."""
        patches = self._patches_with_clean_parity()
        patches["detect_artifact_abstracts_parity"] = AsyncMock(return_value=None)

        findings = await _run_all_checks_with(
            checker, patches, skip_redundancy=True,
            bible_dir=self._usable_bible_dir(tmp_path),
        )

        assert findings["artifact_abstracts_parity"] is None
        assert findings["exit_code"] == 0


class TestRenderArtifactAbstractsParity:
    """Review follow-up (cycle 7): the renderer had no test, unlike its
    sibling `_render_parity_checks_skipped`.

    The REMEDY STRING is the part worth pinning. The check this cycle
    replaced printed `writ reconcile`, and following that advice would have
    DETACH DELETEd 186 correct edges. This one must name a rebuild, never a
    prune, so assert the remedy text and not merely that some line appeared.
    """

    _FINDING = {
        "stale": [("ABS-FAKE-001", "RULE-FAKE-001")],
        "missing": [("ABS-FAKE-002", "RULE-FAKE-002")],
    }

    def test_renders_both_directions_with_a_non_destructive_remedy(self) -> None:
        out, err = render_findings({"artifact_abstracts_parity": self._FINDING})
        text = "\n".join(out + err)

        assert "ABS-FAKE-001 -ABSTRACTS-> RULE-FAKE-001" in text, (
            f"stale pair must render with the edge type; got:\n{text}"
        )
        assert "ABS-FAKE-002 -ABSTRACTS-> RULE-FAKE-002" in text, (
            f"missing pair must render with the edge type; got:\n{text}"
        )
        assert "stale=1" in text and "missing=1" in text, (
            f"header must count both directions; got:\n{text}"
        )
        assert "writ reconcile" not in text, (
            "the remedy must NOT be `writ reconcile`: that is the destructive "
            "advice the replaced check printed, and following it would delete "
            f"correct ABSTRACTS edges. Got:\n{text}"
        )

    def test_renders_nothing_when_there_is_no_finding(self) -> None:
        out, err = render_findings({"artifact_abstracts_parity": None})
        text = "\n".join(out + err)
        assert "ABSTRACTS" not in text, (
            f"a clean run must render no abstraction-edge block; got:\n{text}"
        )
