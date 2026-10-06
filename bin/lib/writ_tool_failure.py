"""The tool-failure budget (program item 7c): one streak of IDENTICAL failures per agent and
tool, kept in the session cache field `tool_failure_streak`.

ONE MODULE BEHIND THREE CALLERS, so the key, the hash and the count cannot drift:
  * hooks/scripts/writ-tool-failure-record.sh (PostToolUseFailure) runs `record`.
  * hooks/scripts/writ-tool-failure-budget.sh runs `check` on PreToolUse and `reset` on
    PostToolUse.
  * tests/test_tool_failure_budget.py and tests/firedrill/_census.py import the functions.

THE PARSER DECIDES IDENTITY AND INPUT. Both hooks pass the envelope parser's HOOK_SESSION_ID and
HOOK_AGENT_ID on argv and its HOOK_ENVELOPE on stdin, so the cache file is the one every other
hook uses for this agent, and the hashed `tool_input` is the parser's normalized object (JSON
strings parsed, the CLAUDE_TOOL_INPUT fallback applied). Nothing here re-derives either.

IDENTICAL FAILURES ONLY. A failure whose input hash matches the stored one extends the streak;
any other input restarts it at 1. A successful Write, Edit or NotebookEdit clears every streak the
agent holds, because it changed a file the failing call may read. docs/adr/ADR-tool-failure-budget.md
records the alternatives.

FAIL OPEN AND NEVER CREATE STATE. A missing or unreadable cache records nothing, resets nothing
and refuses nothing. `record` and `reset` read the cache before taking the lock, so they never
create a cache for an id that had none and never let mutate_cache replace a corrupt cache with
defaults.

stdlib only: this runs from hooks where no virtualenv is guaranteed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time

# Mirrors the bootstrap in bin/lib/review_findings.py: the skill root is two levels above
# bin/lib/, and `import writ.session.*` must resolve when this runs as a script.
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SKILL_ROOT not in sys.path:
    sys.path.insert(0, _SKILL_ROOT)

from writ.session.cache import _cache_path, mutate_cache  # noqa: E402

CACHE_KEY = "tool_failure_streak"
STREAK_LIMIT = 3
MAIN_AGENT = "main"
WORLD_CHANGING_TOOLS = frozenset({"Write", "Edit", "NotebookEdit"})
FRICTION_EVENT = "tool_budget_denied"
_COMMANDS = ("record", "reset", "check")


def streak_key(agent_id: str, tool_name: str) -> str:
    """`<agent id or main>|<tool>`. The budget hook spells the same key in bash."""
    return f"{agent_id or MAIN_AGENT}|{tool_name}"


def input_hash(tool_name: str, tool_input) -> str:
    """Hash of the parser's normalized input. A non-object is the empty object, which is also
    what the parser hands over for a call with no input."""
    canonical = json.dumps(
        {"tool": tool_name, "input": tool_input if isinstance(tool_input, dict) else {}},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]


def _load(session_id: str) -> dict | None:
    """The cache as a dict, or None when it is missing or unreadable. Never raises."""
    if not session_id:
        return None
    try:
        with open(_cache_path(session_id), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _streaks(cache: dict) -> dict:
    value = cache.get(CACHE_KEY)
    return value if isinstance(value, dict) else {}


def record_failure(session_id: str, agent_id: str, tool_name: str, tool_input) -> dict | None:
    """Extend or restart this agent's streak for `tool_name`; return the new entry, or None
    when nothing was recorded (no tool name, or no readable cache)."""
    if not tool_name or _load(session_id) is None:
        return None
    digest = input_hash(tool_name, tool_input)
    key = streak_key(agent_id, tool_name)
    with mutate_cache(session_id) as cache:
        streaks = _streaks(cache)
        previous = streaks.get(key)
        count = 1
        if isinstance(previous, dict) and previous.get("input_hash") == digest:
            try:
                count = int(previous.get("count", 0)) + 1
            except (TypeError, ValueError):
                count = 1
        entry = {
            "count": count,
            "input_hash": digest,
            "tool": tool_name,
            "agent": agent_id or MAIN_AGENT,
            "last_failed_at": int(time.time()),
        }
        streaks[key] = entry
        cache[CACHE_KEY] = streaks
    return entry


def reset_after_success(session_id: str, agent_id: str, tool_name: str) -> int:
    """Clear streaks after a SUCCESSFUL call; return how many entries were removed.

    The same tool clears its own entry. A world-changing tool clears every entry this agent
    holds. Another agent's entries are never touched."""
    if not tool_name:
        return 0
    current = _load(session_id)
    if current is None or not _streaks(current):
        return 0
    prefix = f"{agent_id or MAIN_AGENT}|"
    with mutate_cache(session_id) as cache:
        streaks = _streaks(cache)
        if tool_name in WORLD_CHANGING_TOOLS:
            doomed = [key for key in streaks if key.startswith(prefix)]
        else:
            own = streak_key(agent_id, tool_name)
            doomed = [own] if own in streaks else []
        for key in doomed:
            del streaks[key]
        cache[CACHE_KEY] = streaks
    return len(doomed)


def refusal_text(tool_name: str, count: int) -> str:
    if tool_name == "Bash":
        override = ("If the user wants this exact command run anyway, ask the user to run it "
                    "themselves by prefixing it with ! in the prompt.")
    else:
        override = "If the user wants this exact call repeated, ask the user how to proceed."
    return (
        f"[ENF-TOOL-BUDGET] Refusing this {tool_name} call: the identical call has already "
        f"failed {count} times in a row, and repeating it unchanged will fail the same way. "
        "Change the input before retrying: read the last error, fix what it names, or take a "
        f"different approach. The count clears on the next successful {tool_name} call or any "
        f"successful file edit. {override}"
    )


def check(session_id: str, agent_id: str, tool_name: str, tool_input) -> dict | None:
    """The refusal verdict for this call, or None to allow. Never raises on bad state."""
    cache = _load(session_id)
    if cache is None or not tool_name:
        return None
    entry = _streaks(cache).get(streak_key(agent_id, tool_name))
    if not isinstance(entry, dict):
        return None
    try:
        count = int(entry.get("count", 0))
    except (TypeError, ValueError):
        return None
    digest = input_hash(tool_name, tool_input)
    if count < STREAK_LIMIT or entry.get("input_hash") != digest:
        return None
    return {"reason": refusal_text(tool_name, count), "count": count,
            "input_hash": digest, "mode": cache.get("mode")}


def _log_refusal(session_id: str, agent_id: str, tool_name: str, verdict: dict) -> None:
    try:
        from writ.session.friction import _log_friction_event

        _log_friction_event(session_id, verdict.get("mode"), FRICTION_EVENT,
                            tool=tool_name, agent=agent_id or MAIN_AGENT,
                            streak=verdict["count"], input_hash=verdict["input_hash"])
    except Exception:
        pass


def _envelope_from_stdin() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str]) -> int:
    """`writ_tool_failure.py <record|reset|check> <HOOK_SESSION_ID> <HOOK_AGENT_ID>`, with
    HOOK_ENVELOPE on stdin. Always exits 0; `check` prints the reason or nothing."""
    if len(argv) != 4 or argv[1] not in _COMMANDS:
        return 0
    command, session_id, agent_id = argv[1], argv[2], argv[3]
    envelope = _envelope_from_stdin()
    tool_name = str(envelope.get("tool_name") or "")
    tool_input = envelope.get("tool_input")
    try:
        if command == "record":
            record_failure(session_id, agent_id, tool_name, tool_input)
        elif command == "reset":
            reset_after_success(session_id, agent_id, tool_name)
        else:
            verdict = check(session_id, agent_id, tool_name, tool_input)
            if verdict:
                _log_refusal(session_id, agent_id, tool_name, verdict)
                sys.stdout.write(verdict["reason"])
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
