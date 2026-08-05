"""POL-5a-plugin: patch-global-config.sh delivers the statusLine to plugin installs.

A Claude Code plugin cannot ship the main statusLine (manifest has no field; plugin
settings.json honors only agent/subagentStatusLine; hooks can't set it). It is the
same gap class as permissions/CLAUDE.md, which patch-global-config.sh already merges
into the plugin user's real ~/.claude/settings.json. This adds the statusLine to that
merge, with policy: add-if-absent / refresh-if-Writ (upgrade-safe) / leave-if-foreign.

All cases run the real script with WRIT_SETTINGS_TARGET + WRIT_CLAUDE_MD_TARGET pointed
at temp files, so the real ~/.claude is never touched.

RED until patch-global-config.sh merges the statusLine.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
PATCH_SCRIPT = SKILL_DIR / "scripts" / "patch-global-config.sh"
# The statusLine merge itself moved into the stdlib-only install module; the shell script
# is now the entry point that calls it. Source-shape assertions follow the code.
INSTALL_MODULE = SKILL_DIR / "bin" / "lib" / "writ_install.py"
MODULE_SRC = INSTALL_MODULE.read_text()

EXPECTED_CMD = f"bash {SKILL_DIR}/hooks/scripts/writ-statusline.sh"

# No tool-presence skip guard: the merge is stdlib-only Python now, so the jq/envsubst
# skipif that used to hide these behavioral cases has nothing left to guard.


def _write_settings(path: Path, status_line=None, with_perms: bool = True) -> None:
    d: dict = {}
    if with_perms:
        d["permissions"] = {"allow": ["Bash(existing-user-entry *)"], "deny": []}
    if status_line is not None:
        d["statusLine"] = status_line
    path.write_text(json.dumps(d, indent=2) + "\n")


def _run_patch(tmp_dir: Path, settings_path: Path) -> subprocess.CompletedProcess:
    claude_md = tmp_dir / "CLAUDE.md"
    env = {
        **os.environ,
        "WRIT_SETTINGS_TARGET": str(settings_path),
        "WRIT_CLAUDE_MD_TARGET": str(claude_md),
    }
    return subprocess.run(
        ["bash", str(PATCH_SCRIPT)],
        capture_output=True, text=True, cwd=str(SKILL_DIR), env=env, timeout=30,
    )


# --------------------------------------------------------------------------- #
# source-shape
# --------------------------------------------------------------------------- #
class TestSourceShape:
    def test_the_merge_module_references_the_statusline_hook(self) -> None:
        assert "writ-statusline.sh" in MODULE_SRC, (
            "the install module must reference the statusLine hook"
        )
        assert "statusLine" in MODULE_SRC, (
            "the install module must assign a statusLine in its settings merge"
        )

    def test_the_patch_script_still_delivers_it(self) -> None:
        """The script stays the documented entry point even though the merge moved: docs,
        bootstrap.sh and this suite all name patch-global-config.sh."""
        assert "writ_install.py" in PATCH_SCRIPT.read_text(), (
            "patch-global-config.sh must delegate to bin/lib/writ_install.py"
        )


# --------------------------------------------------------------------------- #
# behavioral (live script, temp targets)
# --------------------------------------------------------------------------- #
class TestStatusLineMerge:
    def test_absent_gets_added(self, tmp_path) -> None:
        s = tmp_path / "settings.json"
        _write_settings(s, status_line=None)
        r = _run_patch(tmp_path, s)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        d = json.loads(s.read_text())
        assert d.get("statusLine", {}).get("type") == "command"
        assert d["statusLine"]["command"] == EXPECTED_CMD, (
            f"absent statusLine must be added with the current path; got {d.get('statusLine')!r}"
        )

    def test_foreign_left_untouched(self, tmp_path) -> None:
        foreign = {"type": "command", "command": "bash /usr/local/bin/my-statusline.sh"}
        s = tmp_path / "settings.json"
        _write_settings(s, status_line=foreign)
        r = _run_patch(tmp_path, s)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        d = json.loads(s.read_text())
        assert d["statusLine"]["command"] == "bash /usr/local/bin/my-statusline.sh", (
            "a foreign statusLine must never be clobbered"
        )

    def test_stale_writ_refreshed(self, tmp_path) -> None:
        stale = {"type": "command", "command": "bash /old/install/hooks/scripts/writ-statusline.sh"}
        s = tmp_path / "settings.json"
        _write_settings(s, status_line=stale)
        r = _run_patch(tmp_path, s)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        d = json.loads(s.read_text())
        assert d["statusLine"]["command"] == EXPECTED_CMD, (
            "a stale Writ statusLine path must be refreshed to the current install path"
        )

    def test_idempotent_second_run(self, tmp_path) -> None:
        s = tmp_path / "settings.json"
        _write_settings(s, status_line=None)
        _run_patch(tmp_path, s)
        first = s.read_text()
        r2 = _run_patch(tmp_path, s)
        assert s.read_text() == first, "second run must leave settings byte-identical"
        assert "No changes needed" in (r2.stdout + r2.stderr)

    def test_permissions_still_merged(self, tmp_path) -> None:
        s = tmp_path / "settings.json"
        _write_settings(s, status_line=None)
        r = _run_patch(tmp_path, s)
        assert r.returncode == 0, f"exit {r.returncode}; stderr={r.stderr[:300]!r}"
        d = json.loads(s.read_text())
        allow = d["permissions"]["allow"]
        assert any("writ-session.py" in a for a in allow), "Writ allow entries must still merge"
        assert "Bash(existing-user-entry *)" in allow, "existing user entries must be preserved"
