"""Resolve which role a sub-agent is running as, from the one field the harness delivers.

WHY THIS MODULE EXISTS. `agent_type` is in the SubagentStart and SubagentStop envelope
schema, and on this build it arrives EMPTY: 10 of 10 real SubagentStop envelopes captured
2026-08-27 carried `agent_type: ""` while `agent_id` was populated in 10 of 10. Both hooks
then rewrote the empty string to the literal `general-purpose`, so 53 governance records
that day named a role nobody observed and were indistinguishable from a real dispatch of a
general-purpose agent. A write scope keyed on that field would have granted every worker
one role's authority while the code read as though it granted five.

WHERE THE ROLE ACTUALLY LIVES. Claude Code writes a sidecar beside each sub-agent
transcript, `agent-<agent_id>.meta.json`, carrying `agentType` (163 of 163 files on this
machine) plus `spawnDepth`. The naming convention was confirmed 10 of 10 against the
envelope's own `agent_transcript_path`, so `agent_id` is enough to find it.

THE SIDECAR IS EPHEMERAL, WHICH IS WHY A CACHE IS A SOURCE. Claude Code deletes the
sidecar and the transcript after the agent finishes: 0 of 57 logged `agent_id`s still had
one, while 68 sidecars from the same session survived for agents whose stop rows were not
in the log. So the role is resolved at the earliest hook that sees it and PERSISTED, and a
later hook replays it rather than re-deriving it from a file that may be gone.

THE SOURCE TRAVELS WITH THE ROLE, and that is the point of the whole module. Enforcement
(cycle M) may only key on a role that was OBSERVED; a defaulted one has to be visible as
such. A cached role reports the source that originally observed it, not "cache", so
provenance is not degraded by each hop.
"""
from __future__ import annotations

import glob
import json
import os
import re

UNKNOWN_ROLE = "unknown"

SOURCE_ENVELOPE = "envelope"
SOURCE_SIDECAR = "sidecar"
SOURCE_CACHE = "cache"
SOURCE_UNRESOLVED = "unresolved"

_AGENT_PREFIX = "agent-"
_SIDECAR_SUFFIX = ".meta.json"

# `agent_id` is interpolated into a filesystem path and comes off an untrusted envelope.
# An ALLOWLIST, not a blocklist (ABS-SECURITY-024): the real ids are hex-ish
# (`a93fab086dab25fe2`), so anything carrying a separator, a dot or a wildcard is refused
# outright rather than sanitized. That also makes `../../x` resolve to unknown instead of
# reading a file outside the projects tree.
_VALID_AGENT_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


def _projects_root(projects_dir: str | os.PathLike | None = None) -> str:
    """Where Claude Code keeps per-project session directories.

    WRIT_PROJECTS_DIR exists so the hooks' tests can point this at a fixture tree; it is
    not a production knob.
    """
    if projects_dir:
        return str(projects_dir)
    env = os.environ.get("WRIT_PROJECTS_DIR")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _normalized_agent_id(agent_id: object) -> str:
    """The bare id, or "" when it is absent or not a shape we will put in a path.

    Production ids arrive bare; fixtures and some harness paths carry the `agent-` prefix,
    and both must reach the same sidecar.
    """
    candidate = str(agent_id or "").strip()
    if candidate.startswith(_AGENT_PREFIX):
        candidate = candidate[len(_AGENT_PREFIX):]
    if not _VALID_AGENT_ID.match(candidate):
        return ""
    return candidate


def _sidecar_patterns(root: str, filename: str) -> tuple[str, ...]:
    """The two layouts Claude Code uses, at fixed depth.

    A recursive `**` walk would descend every session's transcript store (thousands of
    files) on every hook call. Both layouts are known and shallow, so they are globbed
    directly: flat for an ordinary dispatch, one level deeper under
    `subagents/workflows/wf_<id>/` for workflow fan-out, which was 61 of this session's
    68 sidecars.
    """
    return (
        os.path.join(root, "*", "*", "subagents", filename),
        os.path.join(root, "*", "*", "subagents", "*", "*", filename),
    )


def sidecar_path(agent_id: object, projects_dir: str | os.PathLike | None = None) -> str | None:
    """Absolute path of this agent's sidecar, or None."""
    bare = _normalized_agent_id(agent_id)
    if not bare:
        return None
    root = _projects_root(projects_dir)
    if not os.path.isdir(root):
        return None
    filename = f"{_AGENT_PREFIX}{bare}{_SIDECAR_SUFFIX}"
    for pattern in _sidecar_patterns(root, filename):
        for hit in sorted(glob.glob(pattern)):
            return hit
    return None


def role_from_sidecar(agent_id: object,
                      projects_dir: str | os.PathLike | None = None) -> str | None:
    """The role this agent was dispatched as, read from its sidecar, or None.

    Every failure degrades to None rather than raising: this is called from hooks, and a
    telemetry read must never become a hook failure.
    """
    path = sidecar_path(agent_id, projects_dir)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    role = data.get("agentType")
    if not isinstance(role, str) or not role.strip():
        return None
    return role.strip()


def resolve_role(agent_id: object, envelope_agent_type: object = "",
                 projects_dir: str | os.PathLike | None = None,
                 cache: dict | None = None) -> tuple[str, str]:
    """Return (role, source). Never raises, never invents a role name.

    Order: the envelope when it carries a value (free, and it is what the harness meant),
    then the sidecar (present while the agent lives), then a role this session already
    stored (the answer to the sidecar's deletion), then an explicit unknown.

    A cached role reports the source that ORIGINALLY observed it when the cache recorded
    one, so replaying a stored value does not launder an observation into a weaker claim
    or a default into a stronger one.
    """
    from_envelope = str(envelope_agent_type or "").strip()
    if from_envelope:
        return from_envelope, SOURCE_ENVELOPE

    from_sidecar = role_from_sidecar(agent_id, projects_dir)
    if from_sidecar:
        return from_sidecar, SOURCE_SIDECAR

    if cache:
        stored = str(cache.get("agent_type") or "").strip()
        if stored and stored != UNKNOWN_ROLE:
            origin = str(cache.get("role_source") or "").strip()
            return stored, origin or SOURCE_CACHE

    return UNKNOWN_ROLE, SOURCE_UNRESOLVED


def sidecar_census(projects_dir: str | os.PathLike | None = None) -> dict:
    """What the resolver can currently see: {sidecars, by_type, unreadable}.

    `unreadable` is reported rather than swallowed, because a census that hides its own
    failures is the thing this cycle exists to remove.
    """
    root = _projects_root(projects_dir)
    by_type: dict[str, int] = {}
    seen: set[str] = set()
    unreadable = 0

    if os.path.isdir(root):
        for pattern in _sidecar_patterns(root, f"{_AGENT_PREFIX}*{_SIDECAR_SUFFIX}"):
            for path in glob.glob(pattern):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    with open(path, encoding="utf-8") as handle:
                        data = json.load(handle)
                except (OSError, ValueError):
                    unreadable += 1
                    continue
                role = data.get("agentType") if isinstance(data, dict) else None
                if isinstance(role, str) and role.strip():
                    by_type[role.strip()] = by_type.get(role.strip(), 0) + 1
                else:
                    unreadable += 1

    return {"sidecars": len(seen), "by_type": by_type, "unreadable": unreadable}
