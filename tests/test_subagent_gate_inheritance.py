"""A sub-agent may not write what its orchestrator was just refused.

MEASURED INCIDENT, 2026-09-21. Parent session f053345b sat in work mode at
`current_phase: testing` with `gates_approved: ['phase-a']`, and the
test-skeletons gate refused it five times on legal/index.php, GoogleSheets.php
and contact/api.php. It then dispatched sub-agents, which wrote 24 files in that
project under 28 `write_attempt` rows tagged `subagent_bypass`, newest
12:28:27Z. Three of the refused files are in the written set.

The bypass at gates.py:601 justifies itself in its own comment by the
orchestrator "having already passed the human-approval gate". These tests make
that premise a condition.

THE ARM ONLY EVER TURNS AN ALLOW INTO A DENY, and it ABSTAINS wherever it cannot
establish what the parent was allowed. A parent with no mode has no gates to
clear, so the long-standing allow for that case must survive unchanged: see
TestParentWithNoGatesIsUnchanged, which is the regression half of this file.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
if str(SKILL_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT / "tests"))

# The CACHE AND PLAN CONSTRUCTORS come from test_project_boundary via _tb(), because
# two models of that shape is the drift this file argues against in its own subject
# matter. These two fixtures are not that: an isolated cache dir and a project root
# with a marker, both self-contained, declared here so the fixture names are not
# also module-level imports that every test signature then shadows.
@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "session-cache"
    d.mkdir()
    monkeypatch.setenv("WRIT_CACHE_DIR", str(d))
    return d


@pytest.fixture()
def fake_project(tmp_path):
    """`.git` is the marker that stops resolve_project_root walking further up."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "app" / "modules" / "legal").mkdir(parents=True)
    (root / ".git").mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    return root, outside


def _tb():
    """The canonical constructors, reused rather than re-modelled: two models of
    one cache shape is how the readers in this repo drifted apart before."""
    if str(SKILL_ROOT) not in sys.path:
        sys.path.insert(0, str(SKILL_ROOT))
    if str(SKILL_ROOT / "tests") not in sys.path:
        sys.path.insert(0, str(SKILL_ROOT / "tests"))
    return importlib.import_module("test_project_boundary")


def _child(parent_sid: str, **overrides) -> dict:
    base = {
        "is_subagent": True,
        "cache_source": "subagent_start",
        "agent_type": "writ-implementer",
        "role_source": "envelope",
        "role_write_scope": None,
        "parent_session_id": parent_sid,
    }
    base.update(overrides)
    return base


def _gated_parent(root, **overrides) -> dict:
    """The incident's parent: work mode, phase-a only, test-skeletons pending."""
    base = {
        "mode": "work",
        "current_phase": "testing",
        "gates_approved": ["phase-a"],
        "project_root": str(root),
    }
    base.update(overrides)
    return base


class TestTheIncident:
    """The exact state from 2026-09-21, asserted on the CHILD's decision together
    with the PARENT's recorded state. Never on one session's log rows: the bypass
    rows are filed under the child's session id, and reading only the parent's
    made this look like a completely different defect."""

    def test_child_is_refused_what_the_parent_was_refused(self, cache_dir, fake_project):
        tb = _tb()
        root, _ = fake_project
        parent = "sgi-incident-parent"
        tb._write_cache_for(parent, _gated_parent(root))
        gates = tb._gates_module()
        target = str(root / "app" / "modules" / "legal" / "index.php")
        result = gates._can_write_check("sgi-incident-child", tb._envelope(target), "",
                                        _child(parent))
        assert result["can_write"] is False, (
            "the parent was refused this path by test-skeletons; the child must be too"
        )

    def test_the_refusal_names_the_gate_and_an_action(self, cache_dir, fake_project):
        tb = _tb()
        root, _ = fake_project
        parent = "sgi-reason-parent"
        tb._write_cache_for(parent, _gated_parent(root))
        gates = tb._gates_module()
        target = str(root / "src" / "thing.py")
        reason = gates._can_write_check("sgi-reason-child", tb._envelope(target), "",
                                        _child(parent))["reason"] or ""
        assert reason.strip(), "a refusal naming no action is a deadlock"
        assert "test-skeletons" in reason or "gate" in reason.lower()


class TestTheNormalCaseIsUnchanged:
    def test_child_is_allowed_what_the_parent_may_write(self, cache_dir, fake_project):
        tb = _tb()
        root, _ = fake_project
        parent = "sgi-ok-parent"
        tb._write_cache_for(parent, _gated_parent(
            root, current_phase="implementation",
            gates_approved=["phase-a", "test-skeletons"]))
        gates = tb._gates_module()
        target = str(root / "src" / "thing.py")
        result = gates._can_write_check("sgi-ok-child", tb._envelope(target), "",
                                        _child(parent))
        assert result["can_write"] is True


class TestParentWithNoGatesIsUnchanged:
    """The regression half. A parent with no mode has no gates to clear, so
    inventing a denial from that would be absence used as a policy, and it would
    break TestSubagentUndeclaredScopeConfinedToParentRoot, which is correct today."""

    def test_parent_with_only_a_project_root_still_allows(self, cache_dir, fake_project):
        tb = _tb()
        root, _ = fake_project
        parent = "sgi-nomode-parent"
        tb._write_cache_for(parent, {"project_root": str(root)})
        gates = tb._gates_module()
        target = str(root / "src" / "thing.py")
        assert gates._can_write_check("sgi-nomode-child", tb._envelope(target), "",
                                      _child(parent))["can_write"] is True

    @pytest.mark.parametrize("mode", ["conversation", "review", "investigate"])
    def test_a_parent_in_a_gateless_mode_still_allows(self, cache_dir, fake_project, mode):
        tb = _tb()
        root, _ = fake_project
        parent = f"sgi-{mode}-parent"
        tb._write_cache_for(parent, {"mode": mode, "project_root": str(root)})
        gates = tb._gates_module()
        target = str(root / "src" / "thing.py")
        assert gates._can_write_check(f"sgi-{mode}-child", tb._envelope(target), "",
                                      _child(parent))["can_write"] is True


class TestAbstentions:
    """Every state where nothing establishes what the parent was allowed."""

    def test_no_parent_session_id_abstains(self, cache_dir, fake_project):
        tb = _tb()
        root, _ = fake_project
        gates = tb._gates_module()
        target = str(root / "src" / "thing.py")
        assert gates._can_write_check("sgi-noparent-child", tb._envelope(target), "",
                                      _child(""))["can_write"] is True

    def test_a_lazily_seeded_cache_abstains_here(self, cache_dir, fake_project):
        """cache_source != subagent_start is already handled by the bypass's own
        guard; this arm must not be the thing that decides it."""
        tb = _tb()
        root, _ = fake_project
        parent = "sgi-lazy-parent"
        tb._write_cache_for(parent, _gated_parent(root))
        gates = tb._gates_module()
        target = str(root / "src" / "thing.py")
        result = gates._can_write_check("sgi-lazy-child", tb._envelope(target), "",
                                        _child(parent, cache_source="lazy_seed"))
        assert result["can_write"] is False

    def test_an_empty_parent_cache_abstains(self, cache_dir, fake_project):
        tb = _tb()
        root, _ = fake_project
        gates = tb._gates_module()
        target = str(root / "src" / "thing.py")
        assert gates._can_write_check("sgi-gone-child", tb._envelope(target), "",
                                      _child("sgi-parent-that-never-existed"))["can_write"] is True


class TestOrderingIsPreserved:
    """You cannot require a gate's permission to edit the gate."""

    def test_the_skill_dir_exemption_still_wins(self, cache_dir, fake_project, tmp_path):
        tb = _tb()
        root, _ = fake_project
        parent = "sgi-skill-parent"
        tb._write_cache_for(parent, _gated_parent(root))
        gates = tb._gates_module()
        skill_dir = tmp_path / "writ-install"
        (skill_dir / "writ").mkdir(parents=True)
        target = str(skill_dir / "writ" / "thing.py")
        result = gates._can_write_check("sgi-skill-child", tb._envelope(target),
                                        str(skill_dir), _child(parent))
        assert result["can_write"] is True


class TestMutationProof:
    """With the arm gone, the incident's exact state allows the write. Without
    this, a vacuous arm would pass every test above for the wrong reason."""

    def test_removing_the_arm_restores_the_bypass(self, cache_dir, fake_project, monkeypatch):
        tb = _tb()
        root, _ = fake_project
        parent = "sgi-mutation-parent"
        tb._write_cache_for(parent, _gated_parent(root))
        gates = tb._gates_module()
        target = str(root / "app" / "modules" / "legal" / "index.php")

        before = gates._can_write_check("sgi-mut-a", tb._envelope(target), "", _child(parent))
        assert before["can_write"] is False

        monkeypatch.setattr(gates, "_check_subagent_gate_inheritance",
                            lambda *a, **k: None)
        after = gates._can_write_check("sgi-mut-b", tb._envelope(target), "", _child(parent))
        assert after["can_write"] is True, (
            "with the arm neutralised the write must be allowed again, or the deny "
            "above was coming from something else and this file proves nothing"
        )
