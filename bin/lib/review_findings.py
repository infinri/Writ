"""Reviewer verdicts: parse one, decide whether it blocks, record and read it.

ONE module behind three callers, so the parse and the blocking rule cannot drift:
  * hooks/scripts/writ-subagent-stop.sh -- records the verdict when a writ-reviewer
    subagent stops, taking it from the payload the harness provides rather than
    from the orchestrator. The author of the code is never the courier for the
    critic's findings; that indirection is the whole point.
  * hooks/scripts/writ-bash-write-gate.sh -- asks before `git commit` while a
    blocking verdict stands.
  * writ/server/routes/session_state.py -- the HTTP surface for both.

WHY A PARSER AND NOT json.loads. agents/writ-reviewer.md ends with "Output JSON
only. No prose narrative." That is not what the reviewer emits. A real run
captured 2026-08-06 produced several paragraphs of prose and THEN a fenced ```json
block. Trusting the contract would record nothing on exactly the path that matters,
so this reads the LAST fenced JSON object, falling back to the whole message and
then to the last balanced object in the text.

FAIL TOWARD ASKING. A message that cannot be parsed records `parsed: False` and
counts as BLOCKING. Not understanding the critic must never read as approval. The
one thing that is not blocking is the absence of any verdict at all: this enforces
a reviewer's finding, it does not mandate that a reviewer be run.

stdlib only, like bin/lib/memory_capture.py: this runs from hooks where no
virtualenv is guaranteed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

# Ensure `import writ.session.*` resolves whether this is imported by a test, run
# as a script from a hook, or loaded by the server. Mirrors the bootstrap in
# bin/lib/writ-session.py; the skill root is two levels above bin/lib/.
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SKILL_ROOT not in sys.path:
    sys.path.insert(0, _SKILL_ROOT)

CACHE_KEY = "review_findings_state"
REVIEWER_AGENT_TYPE = "writ-reviewer"

# Fenced blocks, with or without a language tag. DOTALL so a multi-line object is
# one match; non-greedy so two blocks do not merge into one.
_FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_+-]*)\n(.*?)```", re.DOTALL)

_EMPTY_LISTS = ("critical", "important", "minor")


def _normalize(obj: dict) -> dict:
    """Shape a parsed object into the verdict record the callers rely on."""
    verdict = {
        "parsed": True,
        "spec_compliance": str(obj.get("spec_compliance") or ""),
        "status": str(obj.get("status") or ""),
    }
    for key in _EMPTY_LISTS:
        value = obj.get(key)
        verdict[key] = value if isinstance(value, list) else []
    return verdict


def _unparseable(reason: str) -> dict:
    verdict = {"parsed": False, "reason": reason, "spec_compliance": "", "status": ""}
    for key in _EMPTY_LISTS:
        verdict[key] = []
    return verdict


def _candidates(message: str):
    """Yield JSON-object candidates, best first.

    When the message has ANY fenced block, only the LAST one is considered. Two
    reasons, both about not recording the wrong thing as a verdict:
      * a reviewer that quotes the schema before its verdict must not have the
        example recorded, so earlier blocks never win;
      * if that last block is malformed, falling back to an earlier block would
        promote an example over a botched real verdict. Unparseable is the safer
        answer, because unparseable blocks and a stale example might not.
    With no fenced block at all, try the whole message (the contract's stated
    shape), then the last balanced object in the text.
    """
    fenced = _FENCE_RE.findall(message)
    if fenced:
        yield fenced[-1]
        return
    yield message
    start, end = message.find("{"), message.rfind("}")
    if start != -1 and end > start:
        yield message[start:end + 1]


def parse_verdict(message: str) -> dict:
    """Extract a reviewer verdict from its final message.

    Always returns a dict. `parsed` is False when no JSON object could be read;
    callers must treat that as blocking, never as approval.
    """
    if not message or not message.strip():
        return _unparseable("empty message")
    for candidate in _candidates(message):
        try:
            obj = json.loads(candidate.strip())
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            return _normalize(obj)
    return _unparseable("no JSON object found")


def is_blocking(verdict: dict | None) -> bool:
    """True when a recorded verdict must stop the commit.

    None (no reviewer has run) is NOT blocking. An unparseable verdict IS.
    """
    if verdict is None:
        return False
    if not verdict.get("parsed", False):
        return True
    return bool(verdict.get("critical"))


def describe(verdict: dict | None) -> str:
    """One line for the confirmation prompt: enough to decide without re-reading
    the whole review."""
    if verdict is None:
        return ""
    if not verdict.get("parsed", False):
        return (
            "the reviewer's last output could not be parsed as a verdict, so its "
            "findings are unknown"
        )
    critical = verdict.get("critical") or []
    first = critical[0] if critical and isinstance(critical[0], dict) else {}
    where = str(first.get("file") or "?")
    line = first.get("line")
    if line not in (None, ""):
        where = f"{where}:{line}"
    finding = str(first.get("finding") or "").strip()
    head = f"{len(critical)} unresolved CRITICAL review finding"
    if len(critical) != 1:
        head += "s"
    return f"{head}, first at {where}: {finding}" if finding else f"{head}, first at {where}"


def record(session_id: str, message: str, agent_id: str = "") -> dict:
    """Store the verdict for a session. The latest one wins.

    Writes the session cache DIRECTLY rather than through the daemon: this is a
    safety gate, and a daemon outage must not silently drop a blocking verdict and
    leave the commit ungated.

    PROVENANCE, stated honestly. This function trusts its caller. The protection is
    that `writ-bash-write-gate.sh` refuses any Bash command naming `review_findings`
    or `review-findings`, so the ordinary path by which an agent would write itself
    a clean verdict (running this CLI, or curling the endpoint) is denied, exactly
    as the manual-testing grant minter is protected. That is a confirmation
    boundary, not containment: an agent that writes an intermediate script and runs
    that instead is not stopped by a command-text check, and no in-repo secret would
    help, since the agent can read the repo. What closes the loop is the audit trail
    below plus the human at the commit prompt, which matches the north star that
    oversight is relocated to a person rather than replaced by trust.
    """
    from writ.session.cache import mutate_cache

    verdict = parse_verdict(message)
    state = {
        "verdict": verdict,
        "agent_id": agent_id,
        "recorded_at": datetime.now().isoformat(),
    }
    with mutate_cache(session_id) as cache:
        previous = cache.get(CACHE_KEY)
        cache[CACHE_KEY] = state

    # A block being lifted is the security-interesting transition, so it is recorded
    # even though the write itself succeeded. A human auditing the trail can see
    # exactly when a CRITICAL verdict stopped standing, and which agent replaced it.
    was_blocking = is_blocking((previous or {}).get("verdict"))
    if was_blocking and not is_blocking(verdict):
        try:
            from writ.session.friction import _log_friction_event

            _log_friction_event(
                session_id, None, "review_block_lifted",
                previous_agent_id=(previous or {}).get("agent_id", ""),
                clearing_agent_id=agent_id,
                clearing_status=verdict.get("status", ""),
            )
        except Exception:  # noqa: BLE001 - telemetry must not break the record
            pass
    return state


def read_state(session_id: str) -> dict | None:
    """The recorded state for a session, or None when no reviewer has run."""
    from writ.session.cache import _read_cache

    state = _read_cache(session_id).get(CACHE_KEY)
    return state if isinstance(state, dict) else None


def _cli() -> int:
    """`review_findings.py record <session_id> [agent_id]` reads the message on
    stdin (hooks pass it that way to keep it off the process table); `check
    <session_id>` prints the blocking reason and exits 1 when blocking."""
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "record":
        session_id, agent_id = args[1], (args[2] if len(args) > 2 else "")
        state = record(session_id, sys.stdin.read(), agent_id)
        print(json.dumps({"recorded": True, "blocking": is_blocking(state["verdict"])}))
        return 0
    if len(args) >= 2 and args[0] == "check":
        state = read_state(args[1])
        verdict = (state or {}).get("verdict")
        if is_blocking(verdict):
            print(describe(verdict))
            return 1
        return 0
    print("usage: review_findings.py record <session_id> [agent_id] | check <session_id>",
          file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(_cli())
