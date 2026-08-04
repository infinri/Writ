"""POL-5b-4: the dead writ-context-tracker.sh Stop hook is deleted + de-registered.

The hook was an explicit no-op (`exit 0`) retained only for registration
compatibility -- a wasted bash spawn on every assistant turn. This guards its
full removal: file gone, no surface references it, and the Stop event survives
(friction-logger still routes).

RED until the file is deleted and de-registered from all 3 surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-context-tracker.sh"
HOOKS_JSON = SKILL_DIR / "hooks" / "hooks.json"
GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"

NAME = "writ-context-tracker"


def _registration_count(hooks_data: dict) -> int:
    """Flatten every matcher-block entry across all events (mirrors the routing
    test's _collect_all_registrations)."""
    section = hooks_data.get("hooks", hooks_data)
    n = 0
    for entries in section.values():
        if isinstance(entries, list):
            n += len(entries)
    return n


def _commands(hooks_data: dict) -> list[str]:
    section = hooks_data.get("hooks", hooks_data)
    out: list[str] = []
    for entries in section.values():
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    if "command" in entry:
                        out.append(entry["command"])
                    for inner in entry.get("hooks", []) or []:
                        if isinstance(inner, dict) and "command" in inner:
                            out.append(inner["command"])
    return out


class TestFileDeleted:
    def test_hook_file_gone(self) -> None:
        assert not HOOK.exists(), (
            "writ-context-tracker.sh is a dead no-op and must be deleted"
        )


class TestDeregistered:
    def test_not_in_hooks_json(self) -> None:
        assert NAME not in HOOKS_JSON.read_text(), (
            "hooks/hooks.json must not reference writ-context-tracker"
        )

    def test_not_in_global_settings(self) -> None:
        if not GLOBAL_SETTINGS.exists():
            return
        assert NAME not in GLOBAL_SETTINGS.read_text(), (
            "~/.claude/settings.json must not reference writ-context-tracker"
        )


class TestStopEventIntact:
    def test_stop_still_routes_friction_logger(self) -> None:
        data = json.loads(HOOKS_JSON.read_text())
        section = data.get("hooks", {})
        assert "Stop" in section, "Stop event must still exist"
        stop_cmds = " ".join(
            inner.get("command", "")
            for entry in section["Stop"]
            for inner in (entry.get("hooks", []) or [])
        )
        assert "friction-logger.sh" in stop_cmds, (
            "removing context-tracker must not drop friction-logger from Stop"
        )

    def test_registration_count(self) -> None:
        # Current count after #1 (removed dead PreToolUse TodoWrite), #3 (removed
        # dead PostToolUseFailure track-failed-writes), #6 (added PreToolUse Bash
        # writ-bash-write-gate), the token-saving read-junk gate (added PreToolUse
        # Read writ-read-junk-gate), and the comms-output gate (added Stop
        # writ-comms-output-gate), the manual-testing grant (added its UserPromptSubmit
        # minter and the PreToolUse Write|Edit state-write gate), and the auto-memory
        # mirror (added PostToolUse Write|Edit writ-memory-capture). Bump when
        # adding/removing a registration; keep HANDBOOK 'registers **N hook scripts**'
        # in sync.
        data = json.loads(HOOKS_JSON.read_text())
        n = _registration_count(data)
        assert n == 44, f"hooks.json registration count drifted; found {n}, expected 44"
