"""RED guard for Wave-4 remainder (4.2 + 4.3 + 4.4) dead-code / dead-config removal.

Covers three independent groups of verified-dead removals:

4.2 (daemon-loaded `writ/session`): the dead file-path-walking
`_detect_project_root(file_path)` in `locators.py` must be deleted, along with its
re-export in `bin/lib/writ-session.py` and its entry in the POL-6d facade
`hasattr` parametrize list. The differently-shaped LIVE `_detect_project_root
(project_root)` in `approval_workflow.py` must survive untouched (over-deletion
guard). The `_upd_set_gates_approved` test-only backdoor in `budget_tracking.py`
must be removed along with its `_UPDATE_HANDLERS` entry.

4.3 (hooks / tests, not daemon-loaded): the dead `parse_hook_stdin()` shell
function in `common.sh` must be deleted (superseded by `load_hook_env`); the
shadowed first `_load_plan_rules` def in `test_mode_infrastructure.py` must be
deleted, leaving exactly one; `TestChecklistLoading` + `CHECKLISTS_PATH` in
`test_checklist_injection.py` must be deleted (keeping
`TestBackwardContextInjection`); `bin/lib/checklists.json` must be deleted.

4.4 (config truth, not daemon-loaded): `writ.toml.example` must be stripped to
only the 4 sections with live `writ/config.py` readers, dropping the dead
`neo4j.database` key; `scripts/ingest_subagent_roles.py`'s `DISPATCHED_BY` must
replace the two stale reviewer keys with a single `writ-reviewer` key.

This guard is MOSTLY-HERMETIC: no Neo4j, no daemon, no server. It uses
source-scan / AST / tomllib / import-of-import-safe-modules only. Every check
below is written as its own pytest function so the RED/GREEN split per item is
legible in the pytest output.

RED today (2026-07-15, pre-implementation): checks 1, 2, 4, 5, 6, 7, 8, 9, 10
fail. Check 3 (the live approval_workflow function survives) passes now and
must keep passing after the deletions land -- it is an over-deletion guard,
not a removal assertion.

SECURITY: this file parses `writ.toml.example` only. It never reads or opens
`writ.toml` (gitignored, holds real secrets).
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. locators.py -- dead _detect_project_root(file_path) removed, live finders stay
# ---------------------------------------------------------------------------


def test_locators_detect_project_root_removed() -> None:
    path = REPO_ROOT / "writ" / "session" / "locators.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "def _detect_project_root" not in src, (
        "writ/session/locators.py must no longer define the dead "
        "_detect_project_root(file_path) walker; it has zero production callers "
        "and is superseded (for the caller that mattered) by the live, "
        "differently-shaped approval_workflow._detect_project_root(project_root)"
    )


def test_locators_find_debug_md_survives() -> None:
    path = REPO_ROOT / "writ" / "session" / "locators.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "def _find_debug_md" in src, (
        "_find_debug_md has live callers and must survive this deletion cycle"
    )


def test_locators_find_plan_md_survives() -> None:
    path = REPO_ROOT / "writ" / "session" / "locators.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "def _find_plan_md" in src, (
        "_find_plan_md has live callers and must survive this deletion cycle"
    )


# ---------------------------------------------------------------------------
# 2. bin/lib/writ-session.py -- POL-6d re-export drops _detect_project_root
# ---------------------------------------------------------------------------


def test_writ_session_facade_drops_detect_project_root_reexport() -> None:
    path = REPO_ROOT / "bin" / "lib" / "writ-session.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "_detect_project_root" not in src, (
        "bin/lib/writ-session.py must no longer import or reference "
        "_detect_project_root from the writ.session.locators re-export block; "
        "its only occurrence was the removed import line"
    )


# ---------------------------------------------------------------------------
# 3. approval_workflow.py -- live _detect_project_root(project_root) untouched
#    (over-deletion guard: GREEN now and after)
# ---------------------------------------------------------------------------


def test_approval_workflow_live_detect_project_root_survives() -> None:
    path = REPO_ROOT / "writ" / "session" / "approval_workflow.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "def _detect_project_root(project_root" in src, (
        "the LIVE approval_workflow._detect_project_root(project_root) function "
        "has a different signature than the deleted locators.py walker and must "
        "not be touched by this cycle"
    )


# ---------------------------------------------------------------------------
# 4. tests/test_pol6d_mode_engine_extraction.py -- facade hasattr list drops the name
# ---------------------------------------------------------------------------


def test_pol6d_facade_list_drops_detect_project_root() -> None:
    path = REPO_ROOT / "tests" / "test_pol6d_mode_engine_extraction.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert '"_detect_project_root"' not in src, (
        "test_pol6d_mode_engine_extraction.py's TestFacadeReExports parametrize "
        "list must no longer include the deleted _detect_project_root name"
    )


# ---------------------------------------------------------------------------
# 5. writ/session/budget_tracking.py -- --set-gates-approved backdoor removed
# ---------------------------------------------------------------------------


def test_budget_tracking_upd_set_gates_approved_removed() -> None:
    import writ.session.budget_tracking as bt

    assert not hasattr(bt, "_upd_set_gates_approved"), (
        "_upd_set_gates_approved is a test-only backdoor that writes "
        "gates_approved with no approval token; it must be deleted"
    )


def test_budget_tracking_update_handlers_drops_set_gates_approved() -> None:
    import writ.session.budget_tracking as bt

    assert "--set-gates-approved" not in bt._UPDATE_HANDLERS, (
        "_UPDATE_HANDLERS must no longer register the --set-gates-approved flag"
    )


# ---------------------------------------------------------------------------
# 6. bin/lib/common.sh -- dead parse_hook_stdin() removed, load_hook_env stays
# ---------------------------------------------------------------------------


def test_common_sh_parse_hook_stdin_function_removed() -> None:
    path = REPO_ROOT / "bin" / "lib" / "common.sh"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "parse_hook_stdin()" not in src and "parse_hook_stdin ()" not in src, (
        "bin/lib/common.sh must no longer define parse_hook_stdin(); it is dead, "
        "superseded by the single-spawn load_hook_env"
    )


def test_common_sh_load_hook_env_survives() -> None:
    path = REPO_ROOT / "bin" / "lib" / "common.sh"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "load_hook_env" in src, (
        "load_hook_env must survive; it is the live single-spawn hook-env reader"
    )


def test_parse_hook_stdin_py_helper_survives() -> None:
    path = REPO_ROOT / "bin" / "lib" / "parse-hook-stdin.py"
    assert path.exists(), (
        "bin/lib/parse-hook-stdin.py (the --shell helper load_hook_env invokes) "
        "must survive this deletion cycle"
    )


# ---------------------------------------------------------------------------
# 7. tests/test_mode_infrastructure.py -- shadowed first _load_plan_rules removed
# ---------------------------------------------------------------------------


def test_mode_infrastructure_load_plan_rules_defined_exactly_once() -> None:
    path = REPO_ROOT / "tests" / "test_mode_infrastructure.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    count = src.count("def _load_plan_rules")
    assert count == 1, (
        "test_mode_infrastructure.py must define _load_plan_rules exactly once "
        f"(found {count}); the first def at ~line 478 is dead, shadowed by the "
        "live second def at ~line 506 in the same class"
    )


# ---------------------------------------------------------------------------
# 8. checklists.json deleted; TestChecklistLoading + CHECKLISTS_PATH removed,
#    TestBackwardContextInjection stays
# ---------------------------------------------------------------------------


def test_checklists_json_deleted() -> None:
    path = REPO_ROOT / "bin" / "lib" / "checklists.json"
    assert not path.exists(), (
        "bin/lib/checklists.json is dead (no production reader; planning "
        "exit-criteria are already hardcoded in approval_workflow._validate_"
        "phase_a) and must be deleted"
    )


def test_checklist_injection_test_drops_checklist_loading_class() -> None:
    path = REPO_ROOT / "tests" / "test_checklist_injection.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "CHECKLISTS_PATH" not in src, (
        "test_checklist_injection.py must no longer reference CHECKLISTS_PATH "
        "(its only reader, TestChecklistLoading, is being deleted)"
    )
    assert "class TestChecklistLoading" not in src, (
        "test_checklist_injection.py must no longer define TestChecklistLoading "
        "(the 2 schema tests against the deleted checklists.json)"
    )


def test_checklist_injection_test_keeps_backward_context_injection_class() -> None:
    path = REPO_ROOT / "tests" / "test_checklist_injection.py"
    assert path.exists(), f"expected {path} to exist"
    src = path.read_text()
    assert "class TestBackwardContextInjection" in src, (
        "TestBackwardContextInjection (6 tests, no dependency on checklists.json) "
        "must survive this deletion cycle"
    )


# ---------------------------------------------------------------------------
# 9. writ.toml.example -- config truth: only sections with live config.py readers
# ---------------------------------------------------------------------------

# Sections with zero writ/config.py getters; none may remain as a top-level
# parsed key after the strip.
_STRIPPED_SECTIONS = {
    "source",
    "service",
    "embedding",
    "vector",
    "tantivy",
    "ranking",
    "context_budget",
    "ingestion",
    "validation",
    "authority",
    "gate",
    "review",
    "frequency",
    "origin_context",
}

# Sections with a live writ/config.py getter. hnsw/logs may end up comment-only
# (no active keys) and therefore may or may not appear as parsed keys at all --
# the assertion below treats their presence as optional, not required, so it
# is not brittle to that.
# "egress" joined 2026-08-06: get_egress_allow_hosts in writ/config.py reads it,
# which is this guard's own criterion for a kept section.
# "retrieval" joined 2026-08-06 on the same criterion:
# get_authority_preference_threshold reads [retrieval].authority_preference_threshold.
# Note this is NOT the old decorative "ranking" section (still stripped below) --
# that one had no reader; this one does, and the daemon passes its value into
# build_pipeline at startup.
_KEPT_SECTIONS_ALLOWED = {"neo4j", "bitbucket", "hnsw", "logs", "egress", "retrieval"}


def _load_toml_example() -> dict:
    path = REPO_ROOT / "writ.toml.example"
    assert path.exists(), f"expected {path} to exist"
    # SECURITY: only writ.toml.example is read here, never writ.toml (secrets).
    with path.open("rb") as f:
        return tomllib.load(f)


def test_toml_example_top_level_keys_subset_of_kept_sections() -> None:
    cfg = _load_toml_example()
    assert set(cfg) <= _KEPT_SECTIONS_ALLOWED, (
        f"writ.toml.example must only parse to a subset of "
        f"{_KEPT_SECTIONS_ALLOWED}; found extra top-level keys "
        f"{set(cfg) - _KEPT_SECTIONS_ALLOWED}"
    )


def test_toml_example_no_stripped_section_remains() -> None:
    cfg = _load_toml_example()
    leftover = set(cfg) & _STRIPPED_SECTIONS
    assert not leftover, (
        f"writ.toml.example must not contain any decorative section with zero "
        f"writ/config.py readers; found leftover sections {leftover}"
    )


def test_toml_example_keeps_neo4j_and_bitbucket() -> None:
    cfg = _load_toml_example()
    assert "neo4j" in cfg, "writ.toml.example must keep [neo4j] (live config.py getters)"
    assert "bitbucket" in cfg, (
        "writ.toml.example must keep [bitbucket] (live config.py getters)"
    )


def test_toml_example_neo4j_drops_database_key() -> None:
    cfg = _load_toml_example()
    assert "database" not in cfg["neo4j"], (
        "writ.toml.example [neo4j] must not carry the dead 'database' key; no "
        "writ/config.py getter reads it"
    )


def test_toml_example_neo4j_keys_subset_of_live_getters() -> None:
    cfg = _load_toml_example()
    assert set(cfg["neo4j"]) <= {"uri", "user", "password"}, (
        "writ.toml.example [neo4j] must only carry keys with a live "
        "writ/config.py getter (uri/user/password)"
    )


# ---------------------------------------------------------------------------
# 10. scripts/ingest_subagent_roles.py -- DISPATCHED_BY stale keys replaced
# ---------------------------------------------------------------------------


def _load_dispatched_by() -> dict:
    """AST-parse (never import) ingest_subagent_roles.py's DISPATCHED_BY dict.

    Importing the module pulls writ.graph.db's Neo4j driver and reads config at
    module scope; this keeps the guard hermetic.
    """
    path = REPO_ROOT / "scripts" / "ingest_subagent_roles.py"
    assert path.exists(), f"expected {path} to exist"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "DISPATCHED_BY":
                return ast.literal_eval(node.value)
        # AnnAssign form: DISPATCHED_BY: dict[str, list[str]] = {...}
        if isinstance(node, ast.AnnAssign):
            pass
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "DISPATCHED_BY" and node.value is not None:
                return ast.literal_eval(node.value)
    raise AssertionError("DISPATCHED_BY assignment not found in ingest_subagent_roles.py")


def test_dispatched_by_writ_reviewer_key_replaces_stale_keys() -> None:
    dispatched_by = _load_dispatched_by()
    assert dispatched_by.get("writ-reviewer") == [
        "PBK-PROC-SDD-001",
        "PBK-PROC-REVREQ-001",
    ], (
        "DISPATCHED_BY must carry a single writ-reviewer key with the union of "
        "the two stale roles' playbook ids"
    )


def test_dispatched_by_drops_writ_spec_reviewer() -> None:
    dispatched_by = _load_dispatched_by()
    assert "writ-spec-reviewer" not in dispatched_by, (
        "writ-spec-reviewer no longer exists as a .claude/agents/*.md role and "
        "must be dropped from DISPATCHED_BY"
    )


def test_dispatched_by_drops_writ_code_quality_reviewer() -> None:
    dispatched_by = _load_dispatched_by()
    assert "writ-code-quality-reviewer" not in dispatched_by, (
        "writ-code-quality-reviewer no longer exists as a .claude/agents/*.md "
        "role and must be dropped from DISPATCHED_BY"
    )
