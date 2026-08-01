"""FIX-5 (Option B): full sub-agent role reconciliation.

The graph must be the honest single source of truth for all worker agents
shipped under .claude/agents/ (five after the reviewer merge). Before this fix:
  - writ-explorer / writ-planner / writ-test-writer had no SubagentRole node;
  - ROL-IMPLEMENTER-001 / ROL-SPEC-REVIEWER-001 drifted from their agent files
    (wrong model, missing tools, stale prompt body);
  - the code-reviewer node was named writ-code-reviewer / ROL-CODE-REVIEWER-001
    while the agent file is writ-code-quality-reviewer.md, so role-prompt and the
    SDD playbook's dispatch both pointed at a name no agent file had.

After this fix, every .claude/agents/*.md has exactly one SubagentRole node that
renders byte-identical to it (`export_subagent_roles.py --check` clean), every
canonical name resolves via `writ role-prompt`, and the old name/id is gone.

Pure-filesystem assertions (parse, parity, render byte-match, edge resolution,
rename completeness) always run. Live assertions (role-prompt, export --check)
skip only when Neo4j / the server is unreachable.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from writ.graph.ingest import (
    parse_edges_from_file,
    parse_nodes_from_file,
    validate_parsed_node,
)
from writ.graph.schema import SubagentRole

from tests._bible_guard import requires_bible

pytestmark = requires_bible


WRIT_ROOT = Path(__file__).resolve().parent.parent
BIBLE_METHODOLOGY = WRIT_ROOT / "bible" / "methodology"
AGENTS_DIR = WRIT_ROOT / "agents"
EXPORT_SCRIPT = WRIT_ROOT / "scripts" / "export_subagent_roles.py"
WRIT_PY = WRIT_ROOT / ".venv" / "bin" / "python"

# Canonical name -> (role node file, role_id, model). The five real worker agents.
# The spec-reviewer + code-quality-reviewer were merged into one two-pass writ-reviewer.
ROLE_SPECS = {
    "writ-explorer": ("ROL-EXPLORER-001.md", "ROL-EXPLORER-001", "sonnet"),
    "writ-planner": ("ROL-PLANNER-001.md", "ROL-PLANNER-001", "opus"),
    "writ-test-writer": ("ROL-TEST-WRITER-001.md", "ROL-TEST-WRITER-001", "sonnet"),
    "writ-implementer": ("ROL-IMPLEMENTER-001.md", "ROL-IMPLEMENTER-001", "opus"),
    "writ-reviewer": ("ROL-REVIEWER-001.md", "ROL-REVIEWER-001", "sonnet"),
}

# The retired identifiers that must not survive the rename anywhere.
OLD_NAME = "writ-code-reviewer"
OLD_ROLE_ID = "ROL-CODE-REVIEWER-001"
# Directories swept for dangling references to the old identifiers.
RENAME_SWEEP_DIRS = [
    WRIT_ROOT / "bible",
    WRIT_ROOT / "tests",
    WRIT_ROOT / "hooks" / "scripts",
    WRIT_ROOT / "docs",
]


def _load_render():
    """Load render_agent_md from the export script (mirrors test_phase3b)."""
    spec = importlib.util.spec_from_file_location("export_subagent_roles", EXPORT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["export_subagent_roles"] = mod
    spec.loader.exec_module(mod)
    return mod.render_agent_md


def _parse_role(name: str) -> dict:
    """Parse the single SubagentRole node for a canonical agent name."""
    node_file = BIBLE_METHODOLOGY / ROLE_SPECS[name][0]
    assert node_file.exists(), f"Missing role node file: {node_file}"
    nodes = parse_nodes_from_file(node_file)
    assert len(nodes) == 1, f"{node_file.name} parsed to {len(nodes)} nodes, expected 1"
    return nodes[0]


def _role_prompt_unavailable(stderr: str, stdout: str) -> bool:
    """True if role-prompt failed because Neo4j is unreachable (skip), not absent (fail)."""
    blob = (stderr + stdout).lower()
    return any(s in blob for s in ("refused", "could not connect", "serviceunavailable", "unable to retrieve routing"))


# --- 1. Each of the six nodes parses and validates as a SubagentRole ----------


class TestNodesValid:
    @pytest.mark.parametrize("name", sorted(ROLE_SPECS))
    def test_node_parses_as_subagent_role(self, name: str) -> None:
        node = _parse_role(name)
        _, role_id, model = ROLE_SPECS[name]
        assert node["node_type"] == "SubagentRole"
        assert node["role_id"] == role_id
        assert node["name"] == name
        assert node.get("prompt_template", "").strip(), f"{name}: empty prompt_template"
        assert node.get("model_preference") == model
        assert node.get("tools", "").strip(), f"{name}: missing tools (render needs it)"
        assert node.get("description", "").strip(), f"{name}: missing description"

    @pytest.mark.parametrize("name", sorted(ROLE_SPECS))
    def test_node_validates_through_pydantic(self, name: str) -> None:
        node = _parse_role(name)
        result = validate_parsed_node(node)
        assert isinstance(result, SubagentRole)
        assert result.name == name


# --- 2. Full parity between agent files and role nodes ------------------------


class TestParity:
    def test_exactly_five_role_files(self) -> None:
        files = sorted(BIBLE_METHODOLOGY.glob("ROL-*.md"))
        assert len(files) == 5, f"Expected 5 ROL-*.md files, found {len(files)}: {[f.name for f in files]}"

    def test_agent_files_and_node_names_are_the_same_set(self) -> None:
        agent_stems = {p.stem for p in AGENTS_DIR.glob("*.md")}
        node_names = {_parse_role(n)["name"] for n in ROLE_SPECS}
        expected = set(ROLE_SPECS)
        assert agent_stems == expected, f"Agent files {agent_stems} != expected {expected}"
        assert node_names == expected, f"Node names {node_names} != expected {expected}"

    @pytest.mark.parametrize("name", sorted(ROLE_SPECS))
    def test_each_node_has_its_agent_file(self, name: str) -> None:
        assert (AGENTS_DIR / f"{name}.md").exists(), f"No agent file for node name {name}"


# --- 3. Render byte-match: node renders identical to its agent file -----------


class TestRenderByteMatch:
    """The centerpiece: render_agent_md(node) must equal the agent file exactly.

    This is precisely what `export_subagent_roles.py --check` enforces, pinned at
    the file level so it runs without Neo4j.
    """

    @pytest.mark.parametrize("name", sorted(ROLE_SPECS))
    def test_node_renders_to_agent_file(self, name: str) -> None:
        render = _load_render()
        node = _parse_role(name)
        agent_file = AGENTS_DIR / f"{name}.md"
        assert render(node) == agent_file.read_text(encoding="utf-8"), (
            f"{name}: rendered node does not byte-match {agent_file.name}; "
            "export --check would report drift."
        )


# --- 4. Every edge target resolves to a methodology file ----------------------


class TestEdgesResolve:
    @pytest.mark.parametrize("name", sorted(ROLE_SPECS))
    def test_edge_targets_exist(self, name: str) -> None:
        # Any edges the role file declares must resolve to a real methodology node.
        node_file = BIBLE_METHODOLOGY / ROLE_SPECS[name][0]
        for edge in parse_edges_from_file(node_file):
            target = edge["target"]
            assert (BIBLE_METHODOLOGY / f"{target}.md").exists(), (
                f"{name}: edge target {target} ({edge['type']}) has no methodology file"
            )

    @pytest.mark.parametrize("name", sorted(ROLE_SPECS))
    def test_role_is_dispatched_by_a_playbook(self, name: str) -> None:
        # Post-INC-2: DISPATCHES is declared on the dispatching playbook (Playbook -> Role,
        # the canonical direction), so each role must be the TARGET of a DISPATCHES edge.
        role_id = ROLE_SPECS[name][1]
        dispatched = any(
            e.get("type") == "DISPATCHES" and e.get("target") == role_id
            for f in BIBLE_METHODOLOGY.glob("*.md")
            for e in parse_edges_from_file(f)
        )
        assert dispatched, f"{name} ({role_id}) is not dispatched by any playbook"


# --- 5. The rename is complete: old name/id appears nowhere -------------------


class TestRenameComplete:
    @pytest.mark.parametrize("needle", [OLD_NAME, OLD_ROLE_ID])
    def test_old_identifier_absent(self, needle: str) -> None:
        this_file = Path(__file__).resolve()
        hits = []
        for root in RENAME_SWEEP_DIRS:
            for path in root.rglob("*"):
                if not path.is_file() or path.resolve() == this_file:
                    continue
                if path.suffix not in (".md", ".py", ".sh", ".json", ".txt"):
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                if needle in text:
                    hits.append(str(path.relative_to(WRIT_ROOT)))
        assert not hits, f"Stale '{needle}' still present in: {hits}"


# --- 6. role-prompt resolves all six canonical names (live) -------------------


class TestRolePromptLive:
    @pytest.mark.parametrize("name", sorted(ROLE_SPECS))
    def test_role_prompt_resolves(self, name: str, corpus_ready) -> None:
        # INC-1: corpus_ready guarantees the full corpus is present, so a "not found" here
        # is a real regression (FAIL), never a masked empty-graph skip.
        proc = subprocess.run(
            [str(WRIT_PY), "-m", "writ.cli", "role-prompt", name],
            capture_output=True, text=True, cwd=str(WRIT_ROOT),
        )
        if _role_prompt_unavailable(proc.stderr, proc.stdout):
            pytest.skip("Neo4j not reachable for role-prompt")
        assert proc.returncode == 0, f"{name}: role-prompt exited {proc.returncode}\n{proc.stderr}"
        assert "not found" not in proc.stdout.lower(), f"{name}: role-prompt returned not-found"
        assert ROLE_SPECS[name][1] in proc.stdout, f"{name}: role_id missing from output"

    def test_old_name_no_longer_resolves(self, corpus_ready) -> None:
        proc = subprocess.run(
            [str(WRIT_PY), "-m", "writ.cli", "role-prompt", OLD_NAME],
            capture_output=True, text=True, cwd=str(WRIT_ROOT),
        )
        if _role_prompt_unavailable(proc.stderr, proc.stdout):
            pytest.skip("Neo4j not reachable for role-prompt")
        # The CLI reports an unresolved name on stderr with a non-zero exit.
        combined = (proc.stdout + proc.stderr).lower()
        assert proc.returncode != 0 and "not found" in combined, (
            f"'{OLD_NAME}' should be retired but role-prompt still resolves it"
        )


# --- 7. export --check is clean after ingest (live) ---------------------------


class TestExportCheckClean:
    def test_export_check_passes(self, corpus_ready) -> None:
        # INC-1: corpus_ready ensures SubagentRole nodes are present, so the only legitimate
        # skip is an unreachable Neo4j; "No SubagentRole nodes" can no longer mask drift.
        proc = subprocess.run(
            [str(WRIT_PY), str(EXPORT_SCRIPT), "--check"],
            capture_output=True, text=True, cwd=str(WRIT_ROOT),
        )
        if proc.returncode != 0 and "refused" in proc.stderr.lower():
            pytest.skip("Neo4j not reachable")
        assert proc.returncode == 0, f"export --check drift:\n{proc.stdout}\n{proc.stderr}"
