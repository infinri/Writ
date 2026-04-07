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
import sys


def parse() -> None:
    raw = sys.stdin.read()

    # Try stdin envelope first (Claude Code internal format)
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        envelope = {}

    # Extract tool_input -- could be dict or JSON string
    tool_input = envelope.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            tool_input = {}

    # Fallback: CLAUDE_TOOL_INPUT env var (current documented behavior)
    if not tool_input:
        env_input = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if env_input:
            try:
                tool_input = json.loads(env_input)
            except (json.JSONDecodeError, ValueError):
                tool_input = {}

    # Normalize output -- flatten common fields for hook convenience
    result = {
        "session_id": envelope.get("session_id", ""),
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
        # Flattened fields -- the ones hooks actually need
        "file_path": tool_input.get(
            "file_path", tool_input.get("path", "")
        ),
        "content": tool_input.get("content", ""),
        "old_string": tool_input.get("old_string", ""),
        "new_string": tool_input.get("new_string", ""),
        "command": tool_input.get("command", ""),
    }

    json.dump(result, sys.stdout)


if __name__ == "__main__":
    parse()
