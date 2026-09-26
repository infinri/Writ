"""Roll a sub-agent's examined files and shown rule ids up into its parent session.

Called once per sub-agent completion by writ-subagent-stop.sh through
`writ-session.py rollup-subagent <agent_id> <parent_session_id>`. Without it a fan-out
lead's synthesis-gate saw none of its workers' reads, and a plan written by a dispatched
planner could not cite the rules that planner was shown.

Imports writ.session.cache and writ.session.subagent_seed (for CACHE_SOURCE_START);
neither imports this module, so the package's layer graph stays acyclic.
"""

import os

from writ.session.cache import _cache_path, _read_cache, mutate_cache
from writ.session.subagent_seed import CACHE_SOURCE_START


def _child_examined_files(child: dict) -> set:
    # Same definition as investigations._examined_files: opened (pretool_queried_files)
    # or cited as a file.
    files = set(child.get("pretool_queried_files", []))
    files.update(
        r["ref"] for r in child.get("citation_log", [])
        if r.get("artifact_type") == "file" and r.get("ref")
    )
    return files


def _child_rule_ids(child: dict) -> set:
    ids = set(child.get("loaded_rule_ids", []))
    for phase_ids in child.get("loaded_rule_ids_by_phase", {}).values():
        ids.update(phase_ids)
    ids.update(child.get("always_on_rule_ids", []))
    return ids


def rollup_subagent_into_parent(agent_id: str, parent_session_id: str) -> dict:
    """Set-union the child's examined files into the parent's pretool_queried_files and
    its rule ids into the parent's subagent_rule_ids, in one locked write.

    Examined files go to pretool_queried_files rather than citation_log: that log is
    capped, and a wide fan-out would evict the parent's own rows. Rule ids go to
    subagent_rule_ids rather than loaded_rule_ids, which is the parent's ranked-query
    exclude list for rules it never retrieved.

    Skips without writing anything when the ids are missing or equal, the child cache
    file is absent (checked on disk, because _read_cache answers a miss with defaults),
    the child is not linked to this parent, the child was not seeded by the start hook
    (parent_session_id is settable through `update`, cache_source is not, so only a
    start-seeded child's link was observed by Writ), the parent cache file is absent (a rollup
    must not create a session Writ never governed), or the child has nothing to merge.
    Idempotent: a second run adds nothing.
    """
    if not agent_id or not parent_session_id or agent_id == parent_session_id:
        return {"status": "skipped", "reason": "missing_ids"}
    if not os.path.exists(_cache_path(agent_id)):
        return {"status": "skipped", "reason": "child_absent"}
    child = _read_cache(agent_id)
    if child.get("parent_session_id", "") != parent_session_id:
        return {"status": "skipped", "reason": "not_child_of_parent"}
    if str(child.get("cache_source") or "") != CACHE_SOURCE_START:
        return {"status": "skipped", "reason": "child_not_start_seeded"}
    if not os.path.exists(_cache_path(parent_session_id)):
        return {"status": "skipped", "reason": "parent_absent"}

    child_files = _child_examined_files(child)
    child_rule_ids = _child_rule_ids(child)
    if not child_files and not child_rule_ids:
        return {"status": "skipped", "reason": "nothing_to_merge"}

    with mutate_cache(parent_session_id) as parent:
        files_before = set(parent.get("pretool_queried_files", []))
        rules_before = set(parent.get("subagent_rule_ids", []))
        parent["pretool_queried_files"] = sorted(files_before | child_files)
        parent["subagent_rule_ids"] = sorted(rules_before | child_rule_ids)

    return {
        "status": "merged",
        "files_added": len(child_files - files_before),
        "rule_ids_added": len(child_rule_ids - rules_before),
    }
