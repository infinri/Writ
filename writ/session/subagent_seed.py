"""Inherit a parent session's governance state into a sub-agent cache.

WHY THIS IS NOT ONLY THE START HOOK'S JOB. `SubagentStart` is the event that creates a
sub-agent's cache, and it does not arrive for every sub-agent. Measured 2026-08-27 across
all 239 log files including the 226 gzipped archives: 1,381 distinct agents have a
`subagent_start` row, 2,219 hit the stop-side fallback instead, and the two sets do not
intersect. An agent is either governed at spawn or never governed, and 64% are never
governed. 1,425 of the ungoverned nonetheless have `daemon_request` rows filed under their
own agent id, which means Writ hooks DID run inside them: the cache can be seeded on the
first one instead of waiting for an event that may not come.

WHAT A SEEDED CACHE IS WORTH AT THE GATE: NOTHING. Creating a cache where none existed
would otherwise LOOSEN enforcement, because the child inherits the parent's mode and
approved gates and the work gate then allows on its own. Measured before this module was
written, on one envelope:

    TODAY   ungoverned sub-agent (no cache): False  [ENF-GATE-MODE] No mode declared...
    SEEDED  with parent's mode+gates       : True
    SEEDED  without is_subagent            : True

The third line is why `cache_source` exists. `gates.py` resolves a `lazy_seed` cache's mode
as ABSENT, so the write decision is identical to today's on every path, and the sub-agent
write bypass additionally requires `subagent_start`. The mode is still inherited because
retrieval reads it and retrieval is not authority: the Bash gate has no mode-conditional
arm, and of the four readers of `is_subagent` only the write bypass touches permission.

So a lazily seeded agent gains rules, a role and a parent link, and gains nothing it could
write with. Whether it SHOULD be able to write is a question for a later cycle, answered
from an observed role rather than from the accident of a missing file.
"""
from __future__ import annotations

import os
import re

from writ.session.cache import _cache_path, _read_cache, mutate_cache
from writ.session.config import DEFAULT_SESSION_BUDGET
from writ.session.friction import _log_friction_event
from writ.session.subagent_role import UNKNOWN_ROLE, resolve_role

CACHE_SOURCE_START = "subagent_start"
CACHE_SOURCE_LAZY = "lazy_seed"

SEED_EVENT = "subagent_seeded"

# Both ids are interpolated into a cache filename, and the agent id comes off an untrusted
# envelope. An allowlist, not a blocklist (ABS-SECURITY-024): session ids are uuids and
# agent ids are hex-ish, so anything carrying a separator or a dot is refused rather than
# sanitized.
_VALID_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")

# Operational state a fresh sub-agent starts with. Kept here rather than in the hook so the
# start path and the lazy path cannot drift: whatever a governed sub-agent begins with, an
# ungoverned one begins with too.
_CLEAN_OPERATIONAL_STATE: dict = {
    "remaining_budget": DEFAULT_SESSION_BUDGET,
    "loaded_rule_ids": [],
    "loaded_rule_ids_by_phase": {},
    "loaded_rules": [],
    "denial_counts": {},
    "queries": 0,
    "context_percent": 0,
    "files_written": [],
    "analysis_results": {},
    "pending_violations": [],
    "feedback_sent": [],
    "pretool_queried_files": [],
    "token_snapshots": [],
}


def _usable(agent_id: object, parent_session_id: object) -> tuple[str, str] | None:
    """The two ids, or None when they cannot name a child and a distinct parent."""
    agent = str(agent_id or "").strip()
    parent = str(parent_session_id or "").strip()
    if not agent or not parent or agent == parent:
        return None
    if not _VALID_ID.match(agent) or not _VALID_ID.match(parent):
        return None
    return agent, parent


def seed_subagent_cache(agent_id: object, parent_session_id: object, *,
                        cache_source: str = CACHE_SOURCE_LAZY,
                        envelope_agent_type: object = "",
                        projects_dir: str | None = None,
                        default_mode: str | None = None,
                        role: str | None = None,
                        role_source: str | None = None) -> bool:
    """Create a sub-agent cache inheriting the parent's governance state.

    Returns True only when this call created it. False means it already existed, the ids
    were unusable, or the parent had no mode to inherit. Never raises: a hook must not fail
    because governance could not be inherited.

    NO MODE MEANS NO SEED, on the lazy path. A parent with no declared mode has nothing to
    pass down, and inventing one for an agent nobody authorized would be worse than leaving
    it alone, because the mode is exactly what the gate reads.

    `default_mode` exists for the START path only, and it preserves behaviour rather than
    adding any. `writ-subagent-start.sh` has always coalesced a null parent mode to `work`
    (a mode-unset parent would otherwise hand the child a None and let it run mode-less),
    and that is a real authorization event: the dispatch happened. Passing None, as the
    lazy path does, keeps the stricter rule.

    `role` and `role_source` let a caller that already resolved the role pass it in, so
    provenance is not relabelled: the start hook resolves from the sidecar and must not have
    that recorded as `envelope` just because it handed the value over.
    """
    ids = _usable(agent_id, parent_session_id)
    if ids is None:
        return False
    agent, parent = ids

    # Cheap pre-check outside the lock: the common case by far is a cache that already
    # exists, and mutate_cache would otherwise rewrite the file on every call.
    if os.path.exists(_cache_path(agent)):
        return False

    try:
        parent_cache = _read_cache(parent)
    except Exception:  # noqa: BLE001 - an unreadable parent is not a hook failure
        return False
    if not isinstance(parent_cache, dict):
        return False
    inherited_mode = parent_cache.get("mode") or default_mode
    if not inherited_mode:
        return False

    if role is None or role_source is None:
        role, role_source = resolve_role(agent, envelope_agent_type,
                                         projects_dir=projects_dir)

    try:
        with mutate_cache(agent) as cache:
            # Re-checked INSIDE the lock: two hooks for one tool call can race here, and
            # the loser must not replace an inherited gate set mid-run.
            if cache.get("cache_source") or cache.get("is_subagent"):
                return False
            cache["mode"] = inherited_mode
            cache["current_phase"] = parent_cache.get("current_phase") or "planning"
            cache["gates_approved"] = list(parent_cache.get("gates_approved") or [])
            # project_root IS DELIBERATELY NOT INHERITED, and the reason is not obvious.
            # `rotation._sessions_claiming_project` counts every cache whose project_root
            # equals cwd, with no sub-agent exclusion, and the sole-claimant guard refuses
            # to carry a mode forward when more than one session claims the project. A
            # sub-agent that stamped its parent's project would make every rotation look
            # contested and silently cost the user their mode. An earlier draft of this
            # function inherited it and introduced exactly that; the test that pins this is
            # test_it_does_not_stamp_the_project. Teaching the claimant scan to skip
            # sub-agents is the better fix and belongs with that function, not here.
            cache["parent_session_id"] = parent
            cache["is_subagent"] = True
            cache["cache_source"] = cache_source
            cache["agent_type"] = role
            cache["role_source"] = role_source
            cache.update({k: (v.copy() if hasattr(v, "copy") else v)
                          for k, v in _CLEAN_OPERATIONAL_STATE.items()})
    except Exception:  # noqa: BLE001 - a cache write fault must not fail the hook
        return False

    # RECORDED, because the governance census counts lazily seeded agents from this row.
    # A silent seed would be invisible in exactly the population being measured.
    try:
        _log_friction_event(agent, inherited_mode, SEED_EVENT,
                            agent_id=agent, parent_session=parent,
                            cache_source=cache_source, agent_type=role,
                            role_source=role_source)
    except Exception:  # noqa: BLE001 - telemetry never fails the caller
        pass
    return True


def is_lazily_seeded(cache: dict | None) -> bool:
    """True when this cache was created by a hook rather than by SubagentStart.

    The gate calls this to decide that the cache confers no write authority. A cache with
    no `cache_source` at all is NOT lazy: caches written before this module existed are
    governed today, and treating them as lazy would retroactively deny their writes.
    """
    if not isinstance(cache, dict):
        return False
    return str(cache.get("cache_source") or "") == CACHE_SOURCE_LAZY


def unknown_role_marker() -> str:
    """Re-exported so callers need not import two modules to name the unresolved case."""
    return UNKNOWN_ROLE
