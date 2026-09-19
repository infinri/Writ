"""Trust ledger (roadmap 18e): what can act on this machine, and what changed.

The classifier is a pure function over (inventory, baseline), so these tests need
no real home directory (TEST-ISOLATE-001). The mutation proof is load-bearing: if
a one-byte edit does not move an entry to CHANGED, the hashing is decorative
(TEST-ASSERT-001).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from writ.session.trust_ledger import (
    BASELINE_REL_PATH,
    MCP_BLIND_SPOT,
    classify,
    collect_inventory,
    render_ledger,
)

_BASE = {
    "skill": {"writ": "h1", "peer-review": "h2"},
    "agent": {"writ-reviewer": "h3"},
    "mcp": {},
}


class TestClassification:
    def test_identical_inventory_is_all_unchanged(self):
        out = classify(_BASE, _BASE)
        assert {s for s in out.values()} == {"unchanged"}

    def test_an_added_entry_is_new(self):
        inv = json.loads(json.dumps(_BASE))
        inv["skill"]["brand-new"] = "h9"
        assert classify(inv, _BASE)[("skill", "brand-new")] == "new"

    def test_an_edited_entry_is_changed(self):
        inv = json.loads(json.dumps(_BASE))
        inv["skill"]["writ"] = "DIFFERENT"
        assert classify(inv, _BASE)[("skill", "writ")] == "changed"

    def test_a_missing_entry_is_removed(self):
        inv = json.loads(json.dumps(_BASE))
        del inv["skill"]["peer-review"]
        assert classify(inv, _BASE)[("skill", "peer-review")] == "removed"

    def test_changed_is_not_collapsed_into_new(self):
        """An edit under a name you already trusted is the interesting event and
        must stay distinguishable from something that just appeared."""
        inv = json.loads(json.dumps(_BASE))
        inv["skill"]["writ"] = "DIFFERENT"
        inv["skill"]["fresh"] = "h9"
        out = classify(inv, _BASE)
        assert out[("skill", "writ")] == "changed"
        assert out[("skill", "fresh")] == "new"


class TestFirstRunIsNotTotalDrift:
    def test_absent_baseline_is_its_own_state(self):
        out = classify(_BASE, None)
        assert {s for s in out.values()} == {"unrecorded"}

    def test_first_run_output_does_not_read_as_an_alarm(self):
        doc = render_ledger(_BASE, None)
        assert "unrecorded" in doc
        assert "CHANGED" not in doc


class TestMutationProofOverRealFiles:
    def test_one_edited_byte_moves_the_entry_to_changed(self, tmp_path):
        skills = tmp_path / "skills" / "demo"
        skills.mkdir(parents=True)
        f = skills / "SKILL.md"
        f.write_text("original content\n")
        first = collect_inventory(tmp_path / "skills", [], None)

        f.write_text("original contenu\n")
        second = collect_inventory(tmp_path / "skills", [], None)

        assert first["skill"]["demo"] != second["skill"]["demo"]
        assert classify(second, first)[("skill", "demo")] == "changed"

    def test_untouched_files_hash_identically_across_runs(self, tmp_path):
        skills = tmp_path / "skills" / "demo"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("stable\n")
        a = collect_inventory(tmp_path / "skills", [], None)
        b = collect_inventory(tmp_path / "skills", [], None)
        assert a == b


class TestMcpBlindSpotIsDisclosed:
    def test_empty_mcp_section_states_the_limit(self):
        """Connector-managed MCP servers are invisible from disk: this machine has
        12 connected and `mcpServers` is empty for all 37 projects in
        ~/.claude.json. Reporting zero without saying so is a false assurance."""
        doc = render_ledger(_BASE, _BASE)
        assert MCP_BLIND_SPOT in doc

    def test_the_disclosure_is_not_silently_droppable(self):
        assert MCP_BLIND_SPOT.strip() != ""
        assert "connector" in MCP_BLIND_SPOT.lower()


class TestBaselineIsNotCommittable:
    def test_the_baseline_path_is_gitignored(self):
        repo = Path(__file__).resolve().parent.parent
        r = subprocess.run(
            ["git", "check-ignore", "-q", BASELINE_REL_PATH],
            cwd=repo, capture_output=True,
        )
        assert r.returncode == 0, (
            f"{BASELINE_REL_PATH} is not gitignored, so a machine-specific trust "
            f"baseline can be staged and committed."
        )


class TestTwoSourcesCannotShadowEachOther:
    """Agents resolve from the plugin dir AND ~/.claude/agents/. Keying on the bare
    name would let the last directory scanned win, showing one entry while two
    different definitions answer to one name. Found by reading real output."""

    def test_same_name_in_two_dirs_yields_two_entries(self, tmp_path):
        one = tmp_path / "plugin" / "agents"
        two = tmp_path / "home" / "agents"
        one.mkdir(parents=True)
        two.mkdir(parents=True)
        (one / "writ-reviewer.md").write_text("the real one\n")
        (two / "writ-reviewer.md").write_text("an impostor\n")

        agents = collect_inventory(None, [one, two], None)["agent"]
        assert len(agents) == 2, agents
        assert len(set(agents.values())) == 2, "divergent content must show as two hashes"
        assert any(k.endswith(":writ-reviewer") for k in agents)
