"""Phase 6e/6f/6g: methodology corpus promotion to bible/methodology/.

The 60-file methodology corpus moved from
`tests/fixtures/synthetic_methodology/` to `bible/methodology/` in
this commit. Migration runs against the new location to populate
Neo4j with Skill / Playbook / Technique / AntiPattern / Role /
ForbiddenResponse / Phase / Rationalization / PressureScenario /
WorkedExample / SubagentRole nodes.

Tests verify:
  1. The new location exists with the expected per-type file counts.
  2. The old location is gone (catches incomplete moves).
  3. Each major node-type prefix is represented.
  4. The corpus parses cleanly via the existing methodology loader
     (no parser regression introduced by the path change).
  5. The Phase 6 master plan reflects 6e/6f/6g as shipped (doc
     update coupled to the code change).

Migration smoke (running the migrate script + asserting against the
live Neo4j) is verified out-of-band via curl /health, not in this
test suite, because integration tests against a running daemon
belong with the integration harness, not unit pytest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
BIBLE_METHODOLOGY = WRIT_ROOT / "bible" / "methodology"
OLD_FIXTURE_PATH = WRIT_ROOT / "tests" / "fixtures" / "synthetic_methodology"


# Per-prefix counts. Originally captured pre-rename for Phase 6e/6f/6g
# promotion verification. Updated 2026-05-09 for two additions:
# SKL-PROC-WORKTREE-001 (post-PSR-008 methodology-gap closure) and
# PBK-AUTHOR-001 (skill-authoring playbook). Updated 2026-05-21 for
# v1.4.0 additions: PBK-PROC-WORK-WORKFLOW-001, PBK-PROC-ORCHESTRATOR-001,
# SKL-PROC-MODE-001, SKL-PROC-WRIT-FAILURE-001. Updated 2026-06-12 for
# Phase 0 additions: 22 CAT-*.md category nodes + SKL-PROC-DEBUG-001
# (total +23). When you add a new methodology file in this corpus, bump
# the corresponding count here so the snapshot stays honest.
EXPECTED_FILE_COUNTS = {
    "PBK": 15,  # Playbooks (+3 debug-mode Increment 3: PBK-PROC-DIAGNOSE-*; +1 INV-3: PBK-PROC-RESEARCH-001; +1 INV-6a: PBK-PROC-AUDIT-FANOUT-001)
    "SKL": 16,  # Skills (+1 INC-5: SKL-PROC-INVESTIGATE-001; +1 INC-7: SKL-PROC-TDD-DESIGN-FEEDBACK-001; +1 INC-11: SKL-PROC-METHODOLOGY-CHECK-001; +1 Phase3: SKL-PROC-DISPATCH-001; +1 Phase0: SKL-PROC-DEBUG-001; +1 2026-08-05: SKL-PROC-WRIT-DIAGNOSIS-001, the first ai-provisional node) -- INC-9 enriched existing SKL-PROC-REVRECV-001, no new node
    "ANT": 14,  # AntiPatterns (+1 INC-2: ANT-PROC-FINISH-001; +1 INC-7: ANT-PROC-TDD-006; +1 INC-10: ANT-PROC-WORKTREE-001; +1 2026-08-08: ANT-PROC-TDD-007, the absence claim whose search never reached the code)
    "ROL": 5,   # SubagentRoles (explorer, planner, test-writer, implementer, reviewer) -- spec-reviewer + code-quality-reviewer merged into ROL-REVIEWER-001
    "FRB": 2,   # ForbiddenResponses
    "PHA": 20,  # Phases (+11 INC-4: PHA-WORK/ORCH/FANOUT)
    "RAT": 4,   # Rationalizations (+1 INC-11: RAT-PROC-SKILLCHECK-001)
    "PSC": 3,   # PressureScenarios
    "EXM": 3,   # WorkedExamples (+1 INC-8: EXM-PLAN-001)
    "ENF": 11,  # Rule companions (+1 INC-3: ENF-META-CONCISE-001; +1 INC-11: ENF-PROC-PRIORITY-001; +1 Phase4-A3: ENF-COMMS-OUTPUT-001)
    "META": 2,  # Meta-authoring nodes
    "TEC": 11,  # Techniques (+2 INC-3: KEYWORDS, PERSUASION; +1 INC-7: RED-VERIFY; +1 INC-8: FILE-STRUCTURE; +2 INC-12: VERIFY-EVIDENCE-MAP, PARALLEL-PROMPT)
    "CAT": 22,  # Category nodes (Phase 0 Wave B: 22 CAT-*.md membership-target nodes)
}


class TestCorpusLocation:
    """The corpus lives at bible/methodology/ after promotion."""

    def test_new_location_exists(self) -> None:
        assert BIBLE_METHODOLOGY.is_dir(), (
            f"Expected promoted corpus at {BIBLE_METHODOLOGY}; "
            "did the git mv complete?"
        )

    def test_old_location_is_gone(self) -> None:
        """Sanity: the rename should have removed the old path."""
        assert not OLD_FIXTURE_PATH.exists(), (
            f"Old fixture path {OLD_FIXTURE_PATH} still present -- "
            "the rename was incomplete (copy instead of move?)."
        )

    def test_total_file_count_matches_pre_rename(self) -> None:
        """Sum of per-prefix counts equals the total file count, and
        every .md file has a recognized prefix."""
        files = sorted(BIBLE_METHODOLOGY.glob("*.md"))
        expected_total = sum(EXPECTED_FILE_COUNTS.values())
        assert len(files) == expected_total, (
            f"Expected {expected_total} methodology files; found {len(files)}"
        )


class TestPerPrefixCounts:
    """Each major node-type prefix has the expected file count."""

    @pytest.mark.parametrize(
        "prefix, expected",
        sorted(EXPECTED_FILE_COUNTS.items()),
    )
    def test_prefix_count(self, prefix: str, expected: int) -> None:
        files = list(BIBLE_METHODOLOGY.glob(f"{prefix}-*.md"))
        assert len(files) == expected, (
            f"Expected {expected} files with prefix {prefix}-; "
            f"found {len(files)}: {[f.name for f in files]}"
        )


class TestCorpusParsesAfterRename:
    """The existing methodology loader still loads every file from
    the new location -- the rename did not break the parser path."""

    def test_methodology_loader_loads_corpus(self) -> None:
        from tests.fixtures.methodology_loader import load_corpus
        corpus = load_corpus(BIBLE_METHODOLOGY)
        # load_corpus returns a list; assert non-empty + sane minimum.
        assert len(corpus) >= 50, (
            f"Methodology loader returned only {len(corpus)} nodes "
            "from the renamed corpus; expected >=50"
        )

    def test_each_major_type_present_after_load(self) -> None:
        from tests.fixtures.methodology_loader import load_corpus
        corpus = load_corpus(BIBLE_METHODOLOGY)
        # Each item is a dict with a node_type key (or a Pydantic
        # model with one). Use a coarse "any of the expected
        # types appears" assertion.
        types_seen: set[str] = set()
        for item in corpus:
            if hasattr(item, "node_type"):
                types_seen.add(getattr(item, "node_type", ""))
            elif isinstance(item, dict):
                t = item.get("node_type")
                if t:
                    types_seen.add(t)
            else:
                # MethodologyNode-shaped: fall back to class name
                types_seen.add(type(item).__name__)
        for required in (
            "Skill", "Playbook", "AntiPattern", "ForbiddenResponse",
            "SubagentRole",
        ):
            assert required in types_seen, (
                f"Major node type {required!r} missing from corpus after rename. "
                f"Types seen: {sorted(types_seen)}"
            )


# TestMasterPlanReflectsClosure removed 2026-05-10: docs/phase-6-plan.md
# was deleted as a stale planning artifact (methodology absorption shipped;
# the corpus and code in this very test file are the verified outcome).
