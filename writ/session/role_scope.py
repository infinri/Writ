"""A sub-agent role's declared write scope: how it is fetched, and how a path is judged.

TWO FUNCTIONS, TWO DIFFERENT PROCESSES, DELIBERATELY IN ONE MODULE WITH NO STATE BETWEEN
THEM. `fetch_declared_scope` runs ONCE PER DISPATCH, in the `SubagentStart` hook's python
process, and its answer is stamped into the child's session cache. `path_in_scope` runs on
EVERY WRITE, inside whichever process evaluates the gate, and reads that stamped answer.
The write path therefore performs no graph query, no HTTP call and no file read of its own,
which is not a micro-optimisation: the write gate runs in two processes, the daemon route
(which holds a db handle) and a CLI subprocess (which does not), so a write-time lookup
would allow in one and deny in the other and the decision would depend on which one ran.

IMPORTS NOTHING FROM writ.session. `writ/session/subagent_seed.py` calls the fetcher and
`writ/session/gates.py` calls the matcher, and gates.py already imports subagent_seed, so
any import back into that package from here would close a cycle.

THE MATCHER IS ITS OWN FUNCTION RATHER THAN gates.py's `_matches_any`, for the reason
above and for one more that is load-bearing. Those globs match the RAW path and `*` spans
`/`, which is SAFE for an exclusion list (over-matching only keeps a file writable) and
UNSAFE for an allowlist, where `*/tests/*` would otherwise be satisfied by
`/proj/tests/../../etc/shadow`. So the pattern SYNTAX is deliberately the same family
authors already know, and the path is resolved with `os.path.realpath` before matching, the
same choice the settings exemption makes for the same reason (gates.py explains why
`normpath` is wrong there: it collapses `..` lexically while the OS follows the symlink).
"""
from __future__ import annotations

import os
import re

# The daemon's read surface over TCP. Reads are served on every path over TCP
# (writ/server/transport.py: the VERB bounds what TCP may change, not the path), so this
# GET needs no unix socket, and the same convention as writ/session/doctor.py's
# _HEALTH_URL keeps one spelling of the daemon's address in the session package.
_ROLE_URL = "http://localhost:8765/subagent-role/"

# Long enough for a local GET that runs one `MATCH (r:SubagentRole) ... LIMIT 1` over a
# five-node label, short enough that a hung daemon does not hold up a dispatch. Timing out
# means no stamp, which means the sub-agent keeps today's write authority.
_DEFAULT_TIMEOUT = 2.0


def fetch_declared_scope(role: str, timeout: float = _DEFAULT_TIMEOUT) -> list[str] | None:
    """The role's declared write scope, or None when there is no declared scope to read.

    None is returned for EVERY failure as well as for a role that declares nothing, and
    the two are deliberately the same answer here: the caller stamps None, the gate reads
    None as "no declared scope" and keeps the sub-agent bypass. ABSENCE IS NOT A POLICY, so
    a daemon that is down, slow, or missing the role must never harden into a refusal the
    user never asked for. The gap is made countable instead, by the empty `role_scope_source`
    on the cache and by `writ doctor`'s subagent-role-scope-coverage check.

    An EMPTY LIST is a real answer and is passed through as `[]`: the role declares that it
    writes nothing. Only `isinstance(scope, list)` distinguishes it from the absent case,
    which is why nothing on this path coalesces one into the other.

    urllib is imported inside the function because this module is imported by the write
    path (for `path_in_scope`), which must not pay for an http client it never uses.
    """
    name = str(role or "").strip()
    if not name:
        return None

    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    url = _ROLE_URL + urllib.parse.quote(name, safe="")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - every failure degrades to "no declared scope"
        return None
    if not isinstance(payload, dict):
        return None
    scope = payload.get("write_scope")
    if not isinstance(scope, list):
        # Covers the route's own error shapes ({"error": ...}, so no write_scope key at
        # all) and a role whose node leaves the property off.
        return None
    return [str(pattern) for pattern in scope]


def _glob_match(path: str, pattern: str) -> bool:
    """Bash-style glob where `*` matches any character INCLUDING `/`.

    Same dialect as gates.py's exclusion matcher, so an author writing a `write_scope`
    does not learn a second glob syntax; duplicated rather than imported because that
    module imports this package's seeder and a call back would close an import cycle.
    """
    regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    return bool(re.fullmatch(regex, path))


def path_in_scope(file_path: str, patterns) -> bool:
    """True when the RESOLVED `file_path` matches at least one declared pattern.

    An EMPTY pattern list is in scope for nothing, which is exactly what a role declaring
    `write_scope: []` means. A pattern matches either the whole resolved path or its
    basename, mirroring gates.py's `_matches_any`, so `plan.md` is anchored on the
    basename (the plan directory is chosen per dispatch, and a role may not read dispatch
    state) while `*/tests/*` is anchored on the path.

    THE RESOLVED PATH IS WHAT IS JUDGED. `realpath` first, always: it follows symlinks and
    collapses `..` against the true target, so `<in-scope-dir>/../../elsewhere.py` and a
    symlink planted inside the declared directory but pointing out of it are both refused,
    and a symlink whose TARGET is in scope is allowed. Judging the raw string would let
    either escape, because `*` spans `/` and the pattern would still match.
    """
    if not file_path or not patterns:
        return False
    resolved = os.path.realpath(file_path)
    basename = os.path.basename(resolved)
    for pattern in patterns:
        if _glob_match(resolved, pattern) or _glob_match(basename, pattern):
            return True
    return False
