#!/usr/bin/env python3
"""Parse Claude Code hook stdin envelope into normalized fields.

Claude Code dispatches hooks with a JSON envelope on stdin containing
structured tool metadata. This parser normalizes the envelope and falls
back to the CLAUDE_TOOL_INPUT environment variable when the envelope is
missing or incomplete.

Stdin: the full JSON envelope from Claude Code's hook dispatch.
Stdout: normalized JSON with top-level fields for easy consumption.

Envelope format (from Claude Code internals):
{
    "hook_event_name": "PreToolUse",
    "tool_name": "Write",
    "tool_input": {"file_path": "...", "content": "..."},
    "tool_input_json": "{...}",
    "tool_output": null,
    "tool_result_is_error": false
}

Stdlib only -- no external dependencies.
"""

import json
import os
import shlex
import sys


def _as_object(value: object) -> dict:
    """Anything that is not a JSON object becomes an empty one.

    A `"tool_input": null` envelope used to reach `tool_input.get(...)` below and
    raise AttributeError, which the calling hook cannot distinguish from an empty
    parse: every HOOK_* variable stays unset and the session id silently falls back
    to the PPID heuristic. Same for a `tool_input` string that parses to a list, and
    for a root document that is not an object. Measured 2026-08-07 while porting this
    parser to jq: `{"tool_input": null}` crashed here and parsed cleanly there. The
    jq arm normalizes at each step, so this one has to as well, or the fallback
    disagrees with the fast path on the one shape that only breaks one of them.
    """
    return value if isinstance(value, dict) else {}


def parse() -> None:
    raw = sys.stdin.read()

    # Try stdin envelope first (Claude Code internal format)
    try:
        envelope = _as_object(json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        envelope = {}

    # Extract tool_input -- could be dict or JSON string
    tool_input = envelope.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            tool_input = {}
    tool_input = _as_object(tool_input)

    # Fallback: CLAUDE_TOOL_INPUT env var (current documented behavior)
    if not tool_input:
        env_input = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if env_input:
            try:
                tool_input = _as_object(json.loads(env_input))
            except (json.JSONDecodeError, ValueError):
                tool_input = {}

    # Normalize output -- flatten common fields for hook convenience
    result = {
        "session_id": envelope.get("session_id", ""),
        "agent_id": envelope.get("agent_id", ""),
        "agent_type": envelope.get("agent_type", ""),
        "event": envelope.get("hook_event_name", os.environ.get("HOOK_EVENT", "")),
        "tool_name": envelope.get("tool_name", os.environ.get("HOOK_TOOL_NAME", "")),
        "tool_input": tool_input,
        "tool_output": envelope.get(
            "tool_output", os.environ.get("HOOK_TOOL_OUTPUT")
        ),
        "is_error": envelope.get(
            "tool_result_is_error",
            os.environ.get("HOOK_TOOL_IS_ERROR") == "1",
        ),
        # Flattened fields -- the ones hooks actually need. NotebookEdit uses
        # notebook_path/new_source instead of file_path/content; map them here so
        # the whole write/security/RAG stack (which reads file_path + content) gates
        # notebook cell edits like any other write (#4).
        "file_path": (
            tool_input.get("file_path")
            or tool_input.get("path")
            or tool_input.get("notebook_path")
            or ""
        ),
        "content": tool_input.get("content") or tool_input.get("new_source", ""),
        "old_string": tool_input.get("old_string", ""),
        "new_string": tool_input.get("new_string", ""),
        "command": tool_input.get("command", ""),
    }

    if "--shell" in sys.argv:
        _emit_shell(result)
    else:
        json.dump(result, sys.stdout)


def _emit_shell(result: dict) -> None:
    """Emit shlex-quoted shell assignments for the scalar fields + HOOK_ENVELOPE.

    One `eval` of this output sets all HOOK_* vars in a single python3 spawn, so
    hooks read fields as bash variables instead of re-spawning python per field.
    shlex.quote guarantees envelope values cannot be shell-executed by the eval.
    """
    def _q(v: object) -> str:
        return shlex.quote("" if v is None else str(v))

    # Mirror detect_session_id's preference: agent_id (sub-agent isolation) else session_id.
    session_id = result["agent_id"] or result["session_id"]
    is_error = "1" if result["is_error"] else "0"
    lines = [
        f"HOOK_SESSION_ID={_q(session_id)}",
        f"HOOK_SESSION_ID_RAW={_q(result['session_id'])}",
        f"HOOK_AGENT_ID={_q(result['agent_id'])}",
        f"HOOK_AGENT_TYPE={_q(result['agent_type'])}",
        f"HOOK_EVENT={_q(result['event'])}",
        f"HOOK_TOOL_NAME={_q(result['tool_name'])}",
        f"HOOK_FILE_PATH={_q(result['file_path'])}",
        f"HOOK_COMMAND={_q(result['command'])}",
        f"HOOK_IS_ERROR={_q(is_error)}",
        f"HOOK_ENVELOPE={_q(json.dumps(result))}",
    ]
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    parse()
