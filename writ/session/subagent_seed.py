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
from writ.session.subagent_role import SOURCE_UNRESOLVED, UNKNOWN_ROLE, resolve_role

CACHE_SOURCE_START = "subagent_start"
CACHE_SOURCE_LAZY = "lazy_seed"

SEED_EVENT = "subagent_seeded"
SEED_FAILED_EVENT = "subagent_seed_failed"

# How much of a fault's description reaches the row. A gap report names the agent, the path
# it failed on and enough of the cause to act on; it is not a place to reproduce the fault.
# An unbounded traceback in an operator's log is the same defect as an unbounded value in an
# exec envelope, and the census reads these rows one line at a time.
REASON_MAX_CHARS = 160

# Recorded in the child cache next to the scope itself: "graph" when the role node
# answered, "" when nothing was stamped. Without it, a role that declares no scope and a
# dispatch whose fetch failed both read as None and the unenforced share is unmeasurable.
SCOPE_SOURCE_GRAPH = "graph"
SCOPE_SOURCE_NONE = ""

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


def already_seeded(cache: dict | None) -> bool:
    """True when this cache DECLARES that a seeder already governed the agent.

    THE ONE PYTHON SPELLING OF THE RULE, called at both the pre-lock check and the in-lock
    re-check so the two cannot drift. A cache exists for reasons other than seeding: a gate
    denial counts itself inside `mutate_cache`, whose context manager ends in an
    unconditional `_write_cache`, so the first refusal an ungoverned sub-agent earns is what
    creates its file. That file carries `cache_source` empty and `is_subagent` false, which
    is what this reads, rather than the file's mere existence.

    `is_subagent` with no `cache_source` is the LEGACY seeded shape, the only one a cache
    written before `cache_source` existed can carry, so it counts as seeded here.
    """
    if not isinstance(cache, dict):
        return False
    return bool(cache.get("cache_source") or cache.get("is_subagent"))


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
    # exists, and mutate_cache would otherwise rewrite the file on every call. EXISTENCE IS
    # NOT SEEDING, which is why this reads the document instead of stat-ing the path: a gate
    # denial creates the file, so a bare `os.path.exists` made the refusal that proved an
    # agent needed governance the thing that locked it out of governance permanently.
    if os.path.exists(_cache_path(agent)) and already_seeded(_read_cache(agent)):
        return False

    try:
        parent_cache = _read_cache(parent)
    except Exception as exc:  # noqa: BLE001 - an unreadable parent is not a hook failure
        log_seed_failure(agent, cache_source, exc)
        return False
    if not isinstance(parent_cache, dict):
        return False
    inherited_mode = parent_cache.get("mode") or default_mode
    if not inherited_mode:
        return False

    if role is None or role_source is None:
        role, role_source = resolve_role(agent, envelope_agent_type,
                                         projects_dir=projects_dir)

    scope, scope_source = _declared_scope(cache_source, role, role_source)

    seeded_over_existing = False
    try:
        with mutate_cache(agent) as cache:
            # Re-checked INSIDE the lock: two hooks for one tool call can race here, and
            # the loser must not replace an inherited gate set mid-run.
            if already_seeded(cache):
                return False
            seeded_over_existing = os.path.exists(_cache_path(agent))
            # THE CACHE BEING SEEDED OVER IS NOT A FRESH AGENT'S. It exists because
            # something already wrote it, and the thing that writes it first is a gate
            # denial. `denial_counts` is the one key in _CLEAN_OPERATIONAL_STATE with an
            # enforcement consequence, because the write gate escalates deny to ask on a
            # repeat count, so it is carried across the clean state rather than reset with
            # it. No branch: a missing prior cache yields {}, which is what the clean state
            # already carried.
            prior_denials = dict(cache.get("denial_counts") or {})
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
            # Written on every path, and it is (None, "") on every path but the start one
            # because the FETCH above is what is guarded. None is not []: the gate reads
            # None as "no declared scope" and leaves today's authority alone, and [] as
            # "this role writes nothing" and refuses every path.
            cache["role_write_scope"] = scope
            cache["role_scope_source"] = scope_source
            cache.update({k: (v.copy() if hasattr(v, "copy") else v)
                          for k, v in _CLEAN_OPERATIONAL_STATE.items()})
            cache["denial_counts"] = prior_denials
    except Exception as exc:  # noqa: BLE001 - a cache write fault must not fail the hook
        log_seed_failure(agent, cache_source, exc)
        return False

    # RECORDED, because the governance census counts lazily seeded agents from this row.
    # A silent seed would be invisible in exactly the population being measured.
    # `seeded_over_existing` is on the row so how often the seed lands on a cache a denial
    # already created is MEASURED in the field rather than inferred.
    try:
        _log_friction_event(agent, inherited_mode, SEED_EVENT,
                            agent_id=agent, parent_session=parent,
                            cache_source=cache_source, agent_type=role,
                            role_source=role_source,
                            seeded_over_existing=seeded_over_existing)
    except Exception:  # noqa: BLE001 - telemetry never fails the caller
        pass
    return True


def log_seed_failure(agent_id: object, cache_source: str, reason: object) -> None:
    """Record that a sub-agent could not inherit governance: a gap to report, not a failure.

    WRITTEN BY THE PROCESS THAT IS ALREADY RUNNING. The lazy seed costs one python start on
    the first hook inside a sub-agent, so a fault that python can see is reported without a
    second spawn. Bash speaks only when this never ran at all (`_writ_seed_subagent_cache`
    in bin/lib/common.sh), which is the one case where there would otherwise be no row.

    A DECLINE NEVER REACHES HERE. No parent mode, unusable ids and a cache that already
    exists are answers, not faults: 688 of 714 sub-agent sessions with hook activity never
    resolved a mode, so reporting those as failures would bury the real gap under a
    population thirty times its size.

    THE SAME ROW ON BOTH PATHS, told apart by `cache_source`. A `mutate_cache` fault on the
    spawn path returned False, the start hook printed `skipped`, and nothing was recorded
    there either; one call site closes both, and the start hook adds no second row because
    its status word is non-empty.

    Never raises: telemetry never fails the caller.
    """
    agent = str(agent_id or "").strip()
    if not agent:
        return
    if isinstance(reason, BaseException):
        text = f"{type(reason).__name__}: {reason}"
    else:
        text = str(reason or "")
    # Whitespace-collapsed before it is cut, so a multi-line cause cannot forge a second
    # log line and a cut cannot leave a dangling newline behind.
    text = " ".join(text.split())[:REASON_MAX_CHARS]
    try:
        _log_friction_event(agent, "", SEED_FAILED_EVENT,
                            cache_source=cache_source, reason=text)
    except Exception:  # noqa: BLE001 - telemetry never fails the caller
        pass


def _declared_scope(cache_source: str, role: str,
                    role_source: str) -> tuple[list[str] | None, str]:
    """(the role's declared write scope, where it came from) for this dispatch.

    ONE FETCH PER DISPATCH, ON THE START PATH ONLY. A `subagent_start` cache exists because
    `SubagentStart` fired, which means a real dispatch happened; a lazily seeded cache
    exists because a hook noticed an agent nobody governed, and cycle K established that
    such a cache confers no write authority at all. Fetching a scope for it would be
    reading a boundary for an agent that is already outside every gate, so the lazy path
    fetches nothing and stamps nothing and stays worth exactly what cycle K made it worth.

    AN UNOBSERVED ROLE IS NOT FETCHED EITHER. `unknown` / `unresolved` name no role, so
    there is no node to ask about, and asking would spend a round trip to be told so.

    Never raises: a dispatch must not fail because a scope could not be read. The failure
    degrades to None, which is today's decision, because ABSENCE IS NOT A POLICY.
    """
    if cache_source != CACHE_SOURCE_START:
        return None, SCOPE_SOURCE_NONE
    if not role or role == UNKNOWN_ROLE or role_source == SOURCE_UNRESOLVED:
        return None, SCOPE_SOURCE_NONE
    # Imported and called through the MODULE, resolved fresh at call time, matching this
    # codebase's in-function import style -- and required by it: the scope fetcher is the
    # seam the tests patch, and a name bound at this module's import would ignore the patch.
    from writ.session import role_scope

    try:
        scope = role_scope.fetch_declared_scope(role)
    except Exception:  # noqa: BLE001 - an unreadable scope is not a dispatch failure
        return None, SCOPE_SOURCE_NONE
    if scope is None:
        return None, SCOPE_SOURCE_NONE
    return scope, SCOPE_SOURCE_GRAPH


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
