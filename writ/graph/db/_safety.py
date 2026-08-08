"""Refuse a whole-graph wipe against an instance nobody marked disposable.

WHY THIS EXISTS
---------------
Running the full pytest suite destroyed the live graph's runtime records: the graph
held 2 Memory nodes against 98 on-disk memory files, and Decision / FileChange /
Commit were all 0. The cause was two test fixtures calling
`clear_all(preserve_labels=frozenset())` -- the explicit everything-wipe that
overrides `clear_all`'s record-preserving default -- against whatever instance
writ.toml happened to point at, which is the interactive one.

Rules survive that, because `bible/` and `writ-corpus.cypher` can rebuild them. A
Decision record has NO file source and cannot be rebuilt at all. So the wipe is not
"slow to recover from", it is unrecoverable, and it had already happened more than
once (see also the 2026-08-05 graph-wipe incident).

THE MECHANISM, AND WHY BOTH HALVES ARE REQUIRED
-----------------------------------------------
A full wipe is allowed only when BOTH hold:

  1. `WRIT_TEST_GRAPH=1` is in the environment -- a human said "this graph is
     disposable", and
  2. the connected instance is NOT the production instance, compared by
     (host, port) rather than by URI string.

Neither half is sufficient alone, and the failure mode of each is concrete:

  * Env var alone. `WRIT_TEST_GRAPH` is one sticky global. Export it once in a shell
    profile, forget, run the suite six weeks later against the real instance, and the
    marker cheerfully authorizes the exact wipe it was added to prevent. An env var
    records intent; it cannot verify the target.

  * Differing URI alone. It would let a config edit, a typo, or any process that
    merely happens to point somewhere unusual wipe a graph with no human ever
    saying so. It also silently authorizes wiping a DIFFERENT production instance.

Requiring both means the destructive path needs a separate instance to have been
stood up AND a person to have declared it throwaway. Neither a stale env var nor a
config change opens the door by itself.

WHY (host, port) AND NOT STRING EQUALITY
-----------------------------------------
`bolt://127.0.0.1:7687` and `bolt://localhost:7687` are different strings and the
same server. A string comparison would call the first one "not production" and
authorize a wipe of production. Because Neo4j COMMUNITY edition serves exactly one
database per instance -- there is no scratch database to switch to inside the same
server -- instance identity IS (host, port), and every loopback spelling collapses
to one host. That makes a different PORT (or a different machine) the only thing
that counts as a different graph, which is exactly the isolation boundary Community
edition leaves available.

Anything unparseable resolves to "cannot identify this instance", which denies.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

# The opt-in marker. Exactly "1", matching WRIT_NO_AUTOSTART and
# WRIT_ALLOW_EMBEDDING_FALLBACK, so there is one spelling to teach and no
# near-miss value ("yes", "TRUE") that quietly reads as consent.
TEST_GRAPH_ENV_VAR = "WRIT_TEST_GRAPH"
TEST_GRAPH_OPT_IN = "1"

# Every spelling of "this machine" that a bolt URI can carry. urlsplit strips the
# brackets from the IPv6 form, so the bare `::1` is what actually arrives here.
_LOOPBACK_ALIASES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})

# Neo4j's bolt default, used when a URI omits the port. Without it
# `bolt://localhost` and `bolt://localhost:7687` would read as two instances.
_DEFAULT_BOLT_PORT = 7687


class FullWipeRefused(RuntimeError):
    """A whole-graph delete was attempted against a graph nobody marked disposable."""


def instance_key(uri: str | None) -> tuple[str, int] | None:
    """Identify the Neo4j INSTANCE a URI addresses, as (canonical host, port).

    Returns None when the URI is missing or carries no host, i.e. when the instance
    cannot be identified. Callers must treat None as "assume production".

    Loopback aliases collapse to "localhost" so the same server cannot be made to
    look like two. The scheme is intentionally ignored: bolt, bolt+s, neo4j and
    neo4j+s reach the same server, and a scheme change must not read as isolation.
    """
    if not uri or not uri.strip():
        return None
    try:
        parts = urlsplit(uri.strip())
    except ValueError:
        # A malformed URI (e.g. a bad IPv6 literal) identifies nothing.
        return None
    host = (parts.hostname or "").strip().lower()
    if not host:
        return None
    if host in _LOOPBACK_ALIASES:
        host = "localhost"
    try:
        port = parts.port or _DEFAULT_BOLT_PORT
    except ValueError:
        # Out-of-range or non-numeric port: not a resolvable instance.
        return None
    return (host, port)


def marker_present(env: dict[str, str] | None = None) -> bool:
    """True when the operator set the disposable-graph opt-in marker."""
    source = os.environ if env is None else env
    return source.get(TEST_GRAPH_ENV_VAR, "").strip() == TEST_GRAPH_OPT_IN


def is_production_instance(uri: str | None, production_uri: str | None = None) -> bool:
    """True when `uri` addresses the instance this install treats as production.

    An unidentifiable `uri` (None, empty, malformed, hostless) returns True. That
    is the whole safety posture in one line: not knowing where you are pointed is
    treated the same as being pointed at production, so an unparseable connection
    string can never be the thing that authorizes a delete.
    """
    if production_uri is None:
        from writ.config import get_production_neo4j_uri

        production_uri = get_production_neo4j_uri()
    target = instance_key(uri)
    if target is None:
        return True
    production = instance_key(production_uri)
    if production is None:
        # Production itself is unidentifiable; nothing can be proven distinct from it.
        return True
    return target == production


def full_wipe_allowed(uri: str | None, production_uri: str | None = None) -> bool:
    """True only when BOTH the marker is set AND the instance is not production."""
    return marker_present() and not is_production_instance(uri, production_uri)


def how_to_run_safely() -> str:
    """The operator-facing instructions, shared by the refusal and the pytest skip.

    One string so the exception and the skip message cannot drift into telling a
    developer two different things about the same requirement.
    """
    return (
        "A whole-graph wipe is only permitted against a disposable Neo4j instance. "
        "Neo4j Community serves one database per server, so isolation means a "
        "SEPARATE instance on another bolt port. To run these tests:\n"
        "  docker run -d --name writ-test-neo4j -p 7688:7687 -p 7475:7474 "
        "-e NEO4J_AUTH=neo4j/writtestpass neo4j:5\n"
        "  export WRIT_NEO4J_URI=bolt://localhost:7688\n"
        "  export WRIT_NEO4J_PASSWORD=writtestpass\n"
        f"  export {TEST_GRAPH_ENV_VAR}={TEST_GRAPH_OPT_IN}\n"
        "Both the separate port and the marker are required: the marker alone "
        "cannot tell a scratch graph from the real one."
    )


def assert_full_wipe_allowed(uri: str | None, production_uri: str | None = None) -> None:
    """Raise FullWipeRefused unless this graph is explicitly marked disposable.

    Call BEFORE issuing the delete, never after: the contract is that a refused
    wipe deletes nothing, so the refusal has to happen before a session is opened.
    """
    if full_wipe_allowed(uri, production_uri):
        return
    if production_uri is None:
        from writ.config import get_production_neo4j_uri

        production_uri = get_production_neo4j_uri()
    if not marker_present():
        reason = f"{TEST_GRAPH_ENV_VAR} is not set to {TEST_GRAPH_OPT_IN!r}"
    else:
        reason = (
            f"{TEST_GRAPH_ENV_VAR} is set, but the connection targets the production "
            f"instance {instance_key(uri)} (production is {instance_key(production_uri)}); "
            "the marker does not make the real graph disposable"
        )
    raise FullWipeRefused(
        f"Refusing a whole-graph delete against {uri!r}: {reason}. "
        "Runtime records (Memory, Decision, FileChange, Commit) have no bible/ or "
        "dump source, and a Decision record cannot be rebuilt from anything.\n"
        f"{how_to_run_safely()}"
    )
