"""Session-id rotation carry-forward (PIECE 2).

The Claude Code harness can assign a NEW session id partway through a run. The fresh
cache then has mode=None and the write gate denies every write with [ENF-GATE-MODE].
carry_forward_mode carries the MODE (with its mode_source provenance, which is part of
the mode, not extra state) from the pre-rotation session, via
mode_engine._mode_init, so a rotated session re-approves every gate (gates_approved and
a mid-cycle current_phase are NEVER inherited -- that governance north star falls out of
_mode_init delegating to _mode_set, which lands current_phase at the mode's initial phase
and gates_approved at []).

Homed separately so cache.py stays free of a mode_engine import. Acyclic: rotation
depends on cache + mode_engine; neither depends on rotation.
"""

from __future__ import annotations

import sys

from writ.session import cache, mode_engine


def carry_forward_mode(
    session_id: str, cwd: str, prev_session_id: str, source: str
) -> None:
    """Carry the pre-rotation session's mode, and its mode_source, into the rotated session.

    No-op unless: the new session has no mode yet, source is a continued (non-startup)
    session, a prev_session_id is given, the candidate cache has a mode, and the
    candidate's stamped project_root equals cwd (same-project guard). On a successful
    carry, mode_engine._mode_init resets gates and lands the mode's initial phase, then
    a single-line notice is emitted to stderr. Only the successful-carry path prints the
    "carried" notice; every no-op path is silent (a cross-project refusal warns).
    """
    # 1. new session already has a mode -> nothing to do.
    if cache._read_cache(session_id).get("mode"):
        return

    # 2. brand-new (startup) session -> no carry.
    if source == "startup":
        return

    # 3. no candidate id -> nothing to carry from.
    if not prev_session_id:
        return

    # 4. candidate cache has no mode -> nothing to carry.
    candidate = cache._read_cache(prev_session_id)
    prev_mode = candidate.get("mode")
    if not prev_mode:
        return

    # 5. same-project guard: only carry when the candidate declared its mode in cwd.
    if candidate.get("project_root") != cwd:
        print(
            "[Writ] session rotated; NOT carrying mode forward "
            "(pre-rotation session was a different project); run `mode set` to set it.",
            file=sys.stderr,
        )
        return

    # 6. carry the mode AND its provenance (gates reset via _mode_init), then notice.
    #
    # mode_source travels with the mode because a mode that arrives unlabelled reads as
    # explicitly chosen (that is the deliberate fail-closed default), so an AUTO-routed
    # session that rotated would come out the other side looking hand-set and could never
    # be re-routed again -- a rotation, which the user never asked for and cannot see,
    # would silently promote a classifier's guess to the user's word. Passing the
    # candidate's own value also carries None forward as None: a pre-rotation cache written
    # before the field has no provenance to recover, and inventing one either way would be
    # a guess dressed as a record.
    mode_engine._mode_init(session_id, prev_mode, mode_source=candidate.get("mode_source"))
    print(
        f"[Writ] session rotated; carried mode {prev_mode} forward; "
        "gates reset - re-approve each gate.",
        file=sys.stderr,
    )
