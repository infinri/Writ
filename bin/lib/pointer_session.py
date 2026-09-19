"""Does a given session belong to a given project? (stdlib only, never raises)

WHY THIS EXISTS. The git post-commit hook has to name a Claude Code session, and git hands
it no envelope: writ/session/cache.py documents the env vars that might carry one as
unreachable from a bash hook, so the machine-global pointer file is the only bridge
available. The pointer names whichever session took a turn most recently, and because
prompts vastly outnumber commits across concurrent sessions, that was routinely a session
working a DIFFERENT project, whose queried-rules map then landed in this commit's
provenance. writ/session/rotation.py already refuses to act on the same pointer when the
named session declared a different project; this is that comparison, for the commit path.

THIS MODULE NEVER READS THE POINTER, and that is deliberate rather than incidental.
post-commit reads it, and that read is the single exemption
tests/test_session_identity_no_fallback.py grants by name, held to exactly one occurrence.
Moving the read in here would take it out of the file the guard audits and hide it behind an
open() the guard's bash-shaped scan cannot match, silently widening the audited surface.
So the caller reads the pointer and passes the id; this module answers only the question
that was actually missing.

WHY PYTHON AND NOT A common.sh FUNCTION. post-commit is `#!/bin/sh` and documents "System
python3 and curl only", so sourcing a bash library into it would add a bash dependency to a
hook deliberately kept minimal. It already shells to python3 to assemble its JSON.

STDLIB ONLY, AND NEVER RAISES. A git hook must not fail a commit, so every error path
resolves to "no session", which is the same best-effort empty an absent pointer produces.
"""
from __future__ import annotations

import json
import os
import sys


def _cache_dir() -> str:
    explicit = os.environ.get("WRIT_CACHE_DIR")
    if explicit:
        return explicit
    writ_dir = os.environ.get("WRIT_DIR") or ""
    if writ_dir:
        return os.path.join(writ_dir, "var", "session")
    # Fall back to this file's own install location: bin/lib/ -> repo root.
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, "var", "session")


def session_matches_project(session_id: str, project_root: str) -> str:
    """`session_id` when its cache records THIS project as its root, else "".

    "" covers every case in which no session may be credited: an empty id, a missing or
    unreadable session cache, a session that recorded no project, and a session that
    recorded a different one. A session with no recorded project is a MISMATCH rather than
    a pass, because an unknown project cannot be shown to be this one.

    Both sides are realpath'd so a symlinked checkout compares equal to itself.
    """
    try:
        if not project_root or not session_id:
            return ""
        cache_file = os.path.join(_cache_dir(), f"writ-session-{session_id}.json")
        with open(cache_file) as handle:
            recorded = (json.load(handle) or {}).get("project_root") or ""
        if not recorded:
            return ""
        if os.path.realpath(recorded) == os.path.realpath(project_root):
            return session_id
        return ""
    except Exception:
        # A git hook must never fail a commit, so every error is "no session".
        return ""


def main(argv: list[str]) -> int:
    """Usage: pointer_session.py <session_id> <project_root>. Prints the id, or nothing."""
    if len(argv) < 2:
        return 0
    resolved = session_matches_project(argv[0], argv[1])
    if resolved:
        sys.stdout.write(resolved + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
