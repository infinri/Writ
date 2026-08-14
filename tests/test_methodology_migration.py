"""v1.4.0 acceptance tests: static-context absorption into hybrid RAG.

Covers the new acceptance surface introduced in v1.4.0:
- SKILL.md deletion
- plugin.json skills key removal
- four new Methodology nodes and their frontmatter
- templates/CLAUDE.md slimming
- writ-rag-inject.sh breadcrumb repoint
- rules/ stubs pointing at Methodology nodes

Doc-content assertions (docs/*.md prose) were removed 2026-07-31: documentation
is not a test surface, so doc edits must never fail the suite.

Cycle E additions (E1/E2/E3): the exhaustive per-playbook dispatch
classification map (a new playbook cannot default into an unexplained empty
dispatched_roles list), and the ROL-*.md corpus-data pins for the explorer's
lens correction, the Write-without-Edit tools fix, and the dispatched_by
reverse cache. RED today for PBK-PROC-DEBUG-001 / PBK-PROC-TDD-001 (currently
dispatched_roles: []), for ROL-PLANNER-001 / ROL-TEST-WRITER-001 (currently
tools without Edit), for ROL-EXPLORER-001's lens text (currently substitutes
research for runtime), and for the three dispatched_by reverse-cache entries
(PBK-PROC-DEBUG-001 on ROL-EXPLORER-001, PBK-PROC-TDD-001 on
ROL-TEST-WRITER-001 and ROL-IMPLEMENTER-001).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests._bible_guard import requires_bible

pytestmark = requires_bible


REPO_ROOT = (Path(__file__).resolve().parent.parent)
METHODOLOGY_DIR = REPO_ROOT / "bible" / "methodology"

# Required SKL frontmatter keys (from SKL-PROC-BRAIN-001.md schema)
SKL_REQUIRED_KEYS = {
    "skill_id",
    "node_type",
    "domain",
    "severity",
    "trigger",
    "statement",
    "confidence",
    "authority",
    "last_validated",
}

# Required PBK frontmatter keys (from PBK-PROC-PLAN-001.md schema)
PBK_REQUIRED_KEYS = {
    "playbook_id",
    "node_type",
    "domain",
    "severity",
    "trigger",
    "statement",
    "confidence",
    "authority",
    "last_validated",
    "phase_ids",
    "preconditions",
    "dispatched_roles",
}


def _parse_frontmatter(path: Path) -> dict:
    """Extract and parse YAML frontmatter from a Markdown file.

    Matches the '--- ... ---' block at the start of the file and passes
    the contents to yaml.safe_load. Returns an empty dict if no block
    is found or parsing fails.
    """
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


# ---------------------------------------------------------------------------
# SKILL.md deletion
# ---------------------------------------------------------------------------


class TestSkillMdDeleted:
    def test_skill_md_is_deleted(self) -> None:
        """SKILL.md must not exist at the repository root after v1.4.0 migration."""
        assert (REPO_ROOT / "SKILL.md").exists() is False, (
            "SKILL.md must be deleted from the repo root in v1.4.0; "
            "its content has moved to HANDBOOK.md and Methodology nodes"
        )


# ---------------------------------------------------------------------------
# plugin.json skills key removal
# ---------------------------------------------------------------------------


class TestPluginJsonSkillsKey:
    def test_plugin_json_has_no_skills_key(self) -> None:
        """plugin.json must not contain a top-level 'skills' key.

        Writ no longer ships as an Agent Skill plugin; SKILL.md is deleted
        and the skills auto-discovery path is removed.
        """
        import json
        manifest_path = REPO_ROOT / ".claude-plugin" / "plugin.json"
        assert manifest_path.exists(), ".claude-plugin/plugin.json must exist"
        manifest = json.loads(manifest_path.read_text())
        assert "skills" not in manifest, (
            "plugin.json must not have a 'skills' key; "
            "Writ no longer ships as a Skill plugin (v1.4.0)"
        )


# ---------------------------------------------------------------------------
# Methodology node existence
# ---------------------------------------------------------------------------


class TestMethodologyNodeExists:
    def test_methodology_node_skl_proc_mode_001_exists(self) -> None:
        """bible/methodology/SKL-PROC-MODE-001.md must exist."""
        node = METHODOLOGY_DIR / "SKL-PROC-MODE-001.md"
        assert node.exists(), (
            f"{node} must exist; it teaches the mode-set workflow "
            "migrated from templates/CLAUDE.md in v1.4.0"
        )

    def test_methodology_node_pbk_proc_work_workflow_001_exists(self) -> None:
        """bible/methodology/PBK-PROC-WORK-WORKFLOW-001.md must exist."""
        node = METHODOLOGY_DIR / "PBK-PROC-WORK-WORKFLOW-001.md"
        assert node.exists(), (
            f"{node} must exist; it describes the Work-mode gate pipeline "
            "migrated from templates/CLAUDE.md in v1.4.0"
        )

    def test_methodology_node_pbk_proc_orchestrator_001_exists(self) -> None:
        """bible/methodology/PBK-PROC-ORCHESTRATOR-001.md must exist."""
        node = METHODOLOGY_DIR / "PBK-PROC-ORCHESTRATOR-001.md"
        assert node.exists(), (
            f"{node} must exist; it replaces rules/writ-orchestrator.md "
            "as the canonical orchestrator playbook in v1.4.0"
        )

    def test_methodology_node_skl_proc_writ_failure_001_exists(self) -> None:
        """bible/methodology/SKL-PROC-WRIT-FAILURE-001.md must exist."""
        node = METHODOLOGY_DIR / "SKL-PROC-WRIT-FAILURE-001.md"
        assert node.exists(), (
            f"{node} must exist; it replaces rules/writ-workflow.md "
            "as the canonical failure-mode skill in v1.4.0"
        )


# ---------------------------------------------------------------------------
# Methodology node frontmatter
# ---------------------------------------------------------------------------


class TestMethodologyNodeFrontmatter:
    def test_methodology_node_skl_proc_mode_001_has_required_frontmatter(
        self,
    ) -> None:
        """SKL-PROC-MODE-001.md frontmatter must contain all required SKL keys."""
        node = METHODOLOGY_DIR / "SKL-PROC-MODE-001.md"
        assert node.exists(), f"{node} must exist before frontmatter can be checked"
        fm = _parse_frontmatter(node)
        missing = SKL_REQUIRED_KEYS - set(fm.keys())
        assert not missing, (
            f"SKL-PROC-MODE-001.md frontmatter is missing keys: {sorted(missing)}. "
            f"Required SKL keys: {sorted(SKL_REQUIRED_KEYS)}"
        )

    def test_methodology_node_pbk_proc_work_workflow_001_has_required_frontmatter(
        self,
    ) -> None:
        """PBK-PROC-WORK-WORKFLOW-001.md frontmatter must contain all required PBK keys."""
        node = METHODOLOGY_DIR / "PBK-PROC-WORK-WORKFLOW-001.md"
        assert node.exists(), f"{node} must exist before frontmatter can be checked"
        fm = _parse_frontmatter(node)
        missing = PBK_REQUIRED_KEYS - set(fm.keys())
        assert not missing, (
            f"PBK-PROC-WORK-WORKFLOW-001.md frontmatter is missing keys: {sorted(missing)}. "
            f"Required PBK keys: {sorted(PBK_REQUIRED_KEYS)}"
        )

    def test_methodology_node_pbk_proc_orchestrator_001_has_required_frontmatter(
        self,
    ) -> None:
        """PBK-PROC-ORCHESTRATOR-001.md frontmatter must contain all required PBK keys."""
        node = METHODOLOGY_DIR / "PBK-PROC-ORCHESTRATOR-001.md"
        assert node.exists(), f"{node} must exist before frontmatter can be checked"
        fm = _parse_frontmatter(node)
        missing = PBK_REQUIRED_KEYS - set(fm.keys())
        assert not missing, (
            f"PBK-PROC-ORCHESTRATOR-001.md frontmatter is missing keys: {sorted(missing)}. "
            f"Required PBK keys: {sorted(PBK_REQUIRED_KEYS)}"
        )

    def test_methodology_node_skl_proc_writ_failure_001_has_required_frontmatter(
        self,
    ) -> None:
        """SKL-PROC-WRIT-FAILURE-001.md frontmatter must contain all required SKL keys."""
        node = METHODOLOGY_DIR / "SKL-PROC-WRIT-FAILURE-001.md"
        assert node.exists(), f"{node} must exist before frontmatter can be checked"
        fm = _parse_frontmatter(node)
        missing = SKL_REQUIRED_KEYS - set(fm.keys())
        assert not missing, (
            f"SKL-PROC-WRIT-FAILURE-001.md frontmatter is missing keys: {sorted(missing)}. "
            f"Required SKL keys: {sorted(SKL_REQUIRED_KEYS)}"
        )


# ---------------------------------------------------------------------------
# Methodology node semantic content
# ---------------------------------------------------------------------------


class TestMethodologyNodeContent:
    def test_methodology_node_pbk_proc_work_workflow_001_declares_preconditions(
        self,
    ) -> None:
        """PBK-PROC-WORK-WORKFLOW-001.md preconditions must include SKL-PROC-MODE-001.

        The Work-mode playbook depends on the mode-set skill being understood
        first; the precondition encodes that dependency for graph traversal.
        """
        node = METHODOLOGY_DIR / "PBK-PROC-WORK-WORKFLOW-001.md"
        assert node.exists(), f"{node} must exist"
        fm = _parse_frontmatter(node)
        preconditions = fm.get("preconditions", [])
        assert "SKL-PROC-MODE-001" in preconditions, (
            f"PBK-PROC-WORK-WORKFLOW-001.md preconditions must include "
            f"'SKL-PROC-MODE-001'; got {preconditions!r}"
        )

    def test_methodology_node_pbk_proc_orchestrator_001_lists_workers(
        self,
    ) -> None:
        """PBK-PROC-ORCHESTRATOR-001.md dispatched_roles must list all four workers
        by canonical ROL-*-001 id.

        The orchestrator playbook must enumerate every worker role so the graph
        can express the dispatch sequence as edges. Phase 2b: entries must be
        canonical role ids (ROL-EXPLORER-001, ...), not short names like
        'writ-explorer' -- the latter never matched a node id, so the DISPATCHES
        edges were never created (dangling refs).
        """
        node = METHODOLOGY_DIR / "PBK-PROC-ORCHESTRATOR-001.md"
        assert node.exists(), f"{node} must exist"
        fm = _parse_frontmatter(node)
        dispatched_roles = fm.get("dispatched_roles", [])
        expected_roles = {
            "ROL-EXPLORER-001",
            "ROL-PLANNER-001",
            "ROL-TEST-WRITER-001",
            "ROL-IMPLEMENTER-001",
        }
        missing = expected_roles - set(dispatched_roles)
        assert not missing, (
            f"PBK-PROC-ORCHESTRATOR-001.md dispatched_roles must include "
            f"{sorted(expected_roles)}; missing: {sorted(missing)}; "
            f"got {dispatched_roles!r}"
        )


# ---------------------------------------------------------------------------
# Cycle E / E3: exhaustive per-playbook dispatch classification.
#
# Every PBK-*.md is classified exactly once. A playbook either hands work to a
# separate agent session (and then must name the workers in its own text, which
# detect_dispatch_prose_parity enforces) or it does not. The map is exhaustive
# over the glob, so a NEW playbook fails this test until someone classifies it:
# an empty dispatched_roles is an answer here, never a default nobody chose.
# ---------------------------------------------------------------------------

DISPATCHING_PLAYBOOKS = {
    "PBK-PROC-ORCHESTRATOR-001": {
        "ROL-EXPLORER-001", "ROL-PLANNER-001",
        "ROL-TEST-WRITER-001", "ROL-IMPLEMENTER-001",
    },
    "PBK-PROC-SDD-001": {"ROL-IMPLEMENTER-001", "ROL-REVIEWER-001"},
    "PBK-PROC-REVREQ-001": {"ROL-REVIEWER-001"},
    "PBK-PROC-AUDIT-FANOUT-001": {"ROL-EXPLORER-001"},
    "PBK-PROC-DEBUG-001": {"ROL-EXPLORER-001"},
    "PBK-PROC-TDD-001": {"ROL-TEST-WRITER-001", "ROL-IMPLEMENTER-001"},
}

SINGLE_SESSION_PLAYBOOKS = {
    "PBK-PROC-WORK-WORKFLOW-001": "the three gates are what one session does; PBK-PROC-ORCHESTRATOR-001 is the dispatch form and it declares the four workers",
    "PBK-PROC-PLAN-001": "the procedure ROL-PLANNER-001 executes; executed-by is not dispatches",
    "PBK-PROC-BRAIN-001": "a design conversation with the user, and a worker cannot hold the user's approval",
    "PBK-PROC-RESEARCH-001": "the investigation spine; PBK-PROC-AUDIT-FANOUT-001 is the at-scale form that dispatches",
    "PBK-PROC-FINISH-001": "presents four options to the user and waits; there is nothing to hand off",
    "PBK-AUTHOR-001": "authoring runs in the session that holds the pressure-scenario transcripts",
    "PBK-PROC-DIAGNOSE-CRASH-STACKTRACE-001": "a lens PBK-PROC-DEBUG-001 INVOKES; the dispatch decision belongs to the playbook that invokes it",
    "PBK-PROC-DIAGNOSE-FAILING-TEST-001": "a lens PBK-PROC-DEBUG-001 INVOKES; same reason",
    "PBK-PROC-DIAGNOSE-HEISENBUG-001": "a lens PBK-PROC-DEBUG-001 INVOKES; same reason",
}


class TestPlaybookDispatchClassification:
    def test_every_playbook_is_classified_exactly_once(self) -> None:
        on_disk = {p.stem for p in METHODOLOGY_DIR.glob("PBK-*.md")}
        classified = set(DISPATCHING_PLAYBOOKS) | set(SINGLE_SESSION_PLAYBOOKS)
        overlap = set(DISPATCHING_PLAYBOOKS) & set(SINGLE_SESSION_PLAYBOOKS)
        assert not overlap, f"classified as both dispatching and single-session: {sorted(overlap)}"
        assert on_disk == classified, (
            f"unclassified playbooks: {sorted(on_disk - classified)}; "
            f"classified but absent from disk: {sorted(classified - on_disk)}"
        )

    @pytest.mark.parametrize("playbook_id", sorted(DISPATCHING_PLAYBOOKS))
    def test_dispatching_playbook_declares_its_roles(self, playbook_id: str) -> None:
        fm = _parse_frontmatter(METHODOLOGY_DIR / f"{playbook_id}.md")
        assert set(fm.get("dispatched_roles") or []) == DISPATCHING_PLAYBOOKS[playbook_id]

    @pytest.mark.parametrize("playbook_id", sorted(SINGLE_SESSION_PLAYBOOKS))
    def test_single_session_playbook_declares_no_roles(self, playbook_id: str) -> None:
        fm = _parse_frontmatter(METHODOLOGY_DIR / f"{playbook_id}.md")
        assert not (fm.get("dispatched_roles") or []), (
            f"{playbook_id} declares roles but is classified single-session: "
            f"{SINGLE_SESSION_PLAYBOOKS[playbook_id]}"
        )


# ---------------------------------------------------------------------------
# Cycle E / E1+E2+E3: ROL-*.md corpus-data pins.
#
# Nothing pinned `tools` before this cycle -- that is how Write-without-Edit
# survived undetected (test_fix5_role_coverage.py:106 only asserts non-empty;
# test_phase3b_export_subagent_roles.py exercises a synthetic "Read Glob"
# row). Pin the real values directly so the boundary cannot regress silently
# again.
# ---------------------------------------------------------------------------


class TestRoleCountInvariant:
    def test_exactly_five_subagent_role_files_on_disk(self) -> None:
        """No sixth SubagentRole node: E1 corrects ROL-EXPLORER-001 in place
        (it already holds the read-only runtime lens via its tool grant)
        rather than introducing a new 'debugger' role."""
        roles = sorted(p.stem for p in METHODOLOGY_DIR.glob("ROL-*.md"))
        assert roles == [
            "ROL-EXPLORER-001", "ROL-IMPLEMENTER-001", "ROL-PLANNER-001",
            "ROL-REVIEWER-001", "ROL-TEST-WRITER-001",
        ]


class TestE1ExplorerLensCorrection:
    """ROL-EXPLORER-001 must describe the investigation engine as three
    source types (code, web, runtime) and name the runtime/debug lens,
    instead of substituting research for runtime while still claiming
    'three lenses' (SKL-PROC-INVESTIGATE-001 defines code/web/runtime and
    routes runtime to PBK-PROC-DEBUG-001)."""

    def test_prompt_template_names_the_runtime_debug_lens(self) -> None:
        node = METHODOLOGY_DIR / "ROL-EXPLORER-001.md"
        text = node.read_text().lower()
        assert "runtime" in text and "debug" in text, (
            "ROL-EXPLORER-001.md must name the runtime/debug lens "
            "(SKL-PROC-INVESTIGATE-001's third source type) somewhere in "
            "its text, not substitute 'research' for it"
        )

    def test_tags_include_runtime_and_debug(self) -> None:
        node = METHODOLOGY_DIR / "ROL-EXPLORER-001.md"
        fm = _parse_frontmatter(node)
        tags = set(fm.get("tags") or [])
        missing = {"runtime", "debug"} - tags
        assert not missing, (
            f"ROL-EXPLORER-001.md tags must include 'runtime' and 'debug'; "
            f"missing: {sorted(missing)}; got {sorted(tags)}"
        )

    def test_trigger_mentions_pbk_proc_debug_001_needing_runtime_evidence(self) -> None:
        node = METHODOLOGY_DIR / "ROL-EXPLORER-001.md"
        fm = _parse_frontmatter(node)
        trigger = fm.get("trigger", "")
        assert "PBK-PROC-DEBUG-001" in trigger, (
            "ROL-EXPLORER-001's trigger must mention PBK-PROC-DEBUG-001 "
            f"needing runtime evidence gathered before any source is read; "
            f"got: {trigger!r}"
        )

    def test_dispatched_by_includes_pbk_proc_debug_001(self) -> None:
        node = METHODOLOGY_DIR / "ROL-EXPLORER-001.md"
        fm = _parse_frontmatter(node)
        dispatched_by = fm.get("dispatched_by") or []
        assert "PBK-PROC-DEBUG-001" in dispatched_by, (
            f"ROL-EXPLORER-001.dispatched_by must include PBK-PROC-DEBUG-001 "
            f"(E1/E3, matching the new DISPATCHES edge); got {dispatched_by!r}"
        )


class TestE2ToolsBoundary:
    """State the principle once: Write-without-Edit is backwards, because
    Write is strictly more destructive (whole-file replace) than Edit
    (exact-match substring replace). The fix is additive to planner and
    test-writer only; explorer and reviewer's read-only boundary (neither
    Write nor Edit) is coherent and E2 explicitly does not extend it."""

    @pytest.mark.parametrize("role_id", ["ROL-PLANNER-001", "ROL-TEST-WRITER-001"])
    def test_gains_edit_alongside_write(self, role_id: str) -> None:
        node = METHODOLOGY_DIR / f"{role_id}.md"
        fm = _parse_frontmatter(node)
        tools = (fm.get("tools") or "").split()
        assert "Write" in tools, f"{role_id}.md tools={tools!r} must include Write"
        assert "Edit" in tools, (
            f"{role_id}.md tools={tools!r} must include Edit alongside Write "
            "(E2: an agent that can Write but not Edit has the same blast "
            "radius and only the destructive means of using it)"
        )

    @pytest.mark.parametrize("role_id", ["ROL-EXPLORER-001", "ROL-REVIEWER-001"])
    def test_stays_write_and_edit_free(self, role_id: str) -> None:
        node = METHODOLOGY_DIR / f"{role_id}.md"
        fm = _parse_frontmatter(node)
        tools = (fm.get("tools") or "").split()
        assert "Write" not in tools and "Edit" not in tools, (
            f"{role_id}.md tools={tools!r} must hold neither Write nor Edit "
            "-- this read-only boundary is coherent and E2 explicitly does "
            "not extend it to explorer or reviewer"
        )


class TestE3DispatchedByReverseCache:
    """PBK-PROC-TDD-001 gains ROL-TEST-WRITER-001 (RED phase) and
    ROL-IMPLEMENTER-001 (GREEN/REFACTOR); each role's dispatched_by reverse
    cache must be kept in lockstep with the new DISPATCHES edge."""

    @pytest.mark.parametrize(
        "role_id", ["ROL-TEST-WRITER-001", "ROL-IMPLEMENTER-001"]
    )
    def test_tdd_roles_dispatched_by_includes_tdd_playbook(self, role_id: str) -> None:
        node = METHODOLOGY_DIR / f"{role_id}.md"
        fm = _parse_frontmatter(node)
        dispatched_by = fm.get("dispatched_by") or []
        assert "PBK-PROC-TDD-001" in dispatched_by, (
            f"{role_id}.dispatched_by must include PBK-PROC-TDD-001; "
            f"got {dispatched_by!r}"
        )


# ---------------------------------------------------------------------------
# templates/CLAUDE.md slimming
# ---------------------------------------------------------------------------


class TestClaudeMdTemplate:
    @pytest.fixture()
    def template(self) -> str:
        path = REPO_ROOT / "templates" / "CLAUDE.md"
        assert path.exists(), "templates/CLAUDE.md must exist"
        return path.read_text()

    def test_claude_md_template_drops_memory_tiers_section(
        self, template: str
    ) -> None:
        """templates/CLAUDE.md must not contain the '## Memory tiers' section.

        The memory-tiers table is now surfaced via RAG injection, not static
        context. Its presence in the template wastes ~200 tokens per session.
        """
        assert "## Memory tiers" not in template, (
            "templates/CLAUDE.md must not contain '## Memory tiers'; "
            "this section was removed in v1.4.0 (now surfaced via RAG)"
        )

    def test_claude_md_template_drops_mandatory_workflow_section(
        self, template: str
    ) -> None:
        """templates/CLAUDE.md must not contain '## Mandatory workflow before any task'.

        The mandatory-workflow tutorial is now a Methodology node
        (PBK-PROC-WORK-WORKFLOW-001) surfaced via RAG injection.
        """
        assert "## Mandatory workflow before any task" not in template, (
            "templates/CLAUDE.md must not contain '## Mandatory workflow before any task'; "
            "this section was removed in v1.4.0 (now in PBK-PROC-WORK-WORKFLOW-001)"
        )

    def test_claude_md_template_retains_global_preferences_section(
        self, template: str
    ) -> None:
        """templates/CLAUDE.md must retain the '## Global preferences' section.

        User preferences (no emojis, no em-dashes, short responses, etc.) are
        persistent and must not be moved to RAG -- they apply unconditionally.
        """
        assert "## Global preferences" in template, (
            "templates/CLAUDE.md must retain the '## Global preferences' section; "
            "user preferences are not workflow rules and must not be RAG-only"
        )

    def test_claude_md_template_has_bootstrap_fallback(
        self, template: str
    ) -> None:
        """templates/CLAUDE.md must contain a server-down bootstrap-fallback paragraph.

        When the Writ server is unavailable, Claude needs minimal orientation.
        The fallback paragraph must reference the server-unavailable scenario.
        """
        fallback_phrases = [
            "server is unavailable",
            "server unavailable",
            "unreachable",
            "server is down",
        ]
        found = any(phrase in template.lower() for phrase in fallback_phrases)
        assert found, (
            "templates/CLAUDE.md must contain a bootstrap-fallback paragraph for "
            "when the Writ server is unreachable; expected one of: "
            + str(fallback_phrases)
        )

    def test_claude_md_template_is_meaningfully_smaller(
        self, template: str
    ) -> None:
        """templates/CLAUDE.md must have fewer than 30 lines.

        The prior template was 82 lines. The v1.4.0 target is ~25 lines
        (user preferences + one bootstrap paragraph).
        """
        line_count = len(template.splitlines())
        assert line_count < 30, (
            f"templates/CLAUDE.md has {line_count} lines; must be under 30. "
            "The tutorial sections have been moved to Methodology nodes."
        )


# ---------------------------------------------------------------------------
# writ-rag-inject.sh breadcrumb repoint
# ---------------------------------------------------------------------------


class TestRagInjectHook:
    @pytest.fixture()
    def hook_text(self) -> str:
        path = REPO_ROOT / "hooks" / "scripts" / "writ-rag-inject.sh"
        assert path.exists(), ".claude/hooks/writ-rag-inject.sh must exist"
        return path.read_text()

    def test_rag_inject_hook_no_longer_references_skill_md(
        self, hook_text: str
    ) -> None:
        """writ-rag-inject.sh must not reference SKILL.md anywhere.

        The three breadcrumb lines at former lines 220, 585, 641 that
        said 'see SKILL.md' must be updated to reference HANDBOOK.md.
        """
        assert "SKILL.md" not in hook_text, (
            "writ-rag-inject.sh must not reference 'SKILL.md'; "
            "all breadcrumbs were repointed to HANDBOOK.md in v1.4.0"
        )


# ---------------------------------------------------------------------------
# rules/ stubs pointing at Methodology nodes
# ---------------------------------------------------------------------------


class TestRulesStubs:
    def test_rules_writ_workflow_md_is_stub_pointing_at_methodology_node(
        self,
    ) -> None:
        """rules/writ-workflow.md must contain 'SKL-PROC-WRIT-FAILURE-001'.

        The file is kept as a thin pointer so the platform's automatic
        ~/.claude/rules/*.md global-load slot still surfaces something
        rather than 404'ing.
        """
        stub = REPO_ROOT / "rules" / "writ-workflow.md"
        assert stub.exists(), "rules/writ-workflow.md must exist as a stub"
        content = stub.read_text()
        assert "SKL-PROC-WRIT-FAILURE-001" in content, (
            "rules/writ-workflow.md must point at 'SKL-PROC-WRIT-FAILURE-001'; "
            "the content was migrated to that Methodology node in v1.4.0"
        )

    def test_rules_writ_orchestrator_md_is_stub_pointing_at_methodology_node(
        self,
    ) -> None:
        """rules/writ-orchestrator.md must contain PBK-PROC-ORCHESTRATOR-001,
        --orchestrator, and at least one of suppress/injection/token.

        Preserves the existing test_orchestrator_hardening.py assertions while
        confirming the stub points at the new Methodology node.
        """
        stub = REPO_ROOT / "rules" / "writ-orchestrator.md"
        assert stub.exists(), "rules/writ-orchestrator.md must exist as a stub"
        content = stub.read_text()
        assert "PBK-PROC-ORCHESTRATOR-001" in content, (
            "rules/writ-orchestrator.md must reference 'PBK-PROC-ORCHESTRATOR-001'"
        )
        assert "--orchestrator" in content, (
            "rules/writ-orchestrator.md must retain '--orchestrator' "
            "(required by test_orchestrator_hardening.py)"
        )
        token_words = {"suppress", "injection", "token"}
        found_token = any(word in content for word in token_words)
        assert found_token, (
            "rules/writ-orchestrator.md must contain at least one of "
            f"{sorted(token_words)} (required by test_orchestrator_hardening.py)"
        )
