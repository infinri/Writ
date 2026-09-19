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

from tests._inventory import hook_registrations

SKILL_DIR = Path(__file__).resolve().parent.parent
HOOK = SKILL_DIR / "hooks" / "scripts" / "writ-context-tracker.sh"
HOOKS_JSON = SKILL_DIR / "hooks" / "hooks.json"
GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"

NAME = "writ-context-tracker"


def _registration_count(hooks_data: dict) -> int:
    """Count COMMAND LEAVES, the same unit the shared derivation uses.

    This counted matcher BLOCKS until 2026-09-18. It agreed with
    hook_registrations() at 44 only because every block happened to hold exactly one
    command: a property of the data, not of the code. Adding a second command to an
    existing block made blocks 44 and commands 45, and the two readers disagreed for
    the first time. hook_registrations()'s docstring says the two "cannot disagree
    about what a registration is", so the block count was the wrong unit all along.
    """
    section = hooks_data.get("hooks", hooks_data)
    n = 0
    for entries in section.values():
        if isinstance(entries, list):
            for entry in entries:
                for hook in (entry.get("hooks") or []):
                    if hook.get("command"):
                        n += 1
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
        # DERIVED. This was the third of four places asserting 44, and adding a hook meant
        # editing all of them plus a HANDBOOK sentence. The canonical tripwire lives in
        # test_phase51_doc_counts.py, whose declared job is source-derived counts; this
        # site only needs to agree with the manifest it reads.
        data = json.loads(HOOKS_JSON.read_text())
        n = _registration_count(data)
        assert n == len(hook_registrations()), (
            f"this file's own registration count ({n}) disagrees with the shared derivation "
            f"({len(hook_registrations())}); one of the two readers is wrong"
        )
