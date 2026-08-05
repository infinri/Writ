"""Contract for `bin/lib/writ_install.py` (plan.md items 1-9 / capabilities.md 1-9).

This module does not exist yet -- every test below is RED until it is written. It pins
the module's CLI surface and behavior so the collapse of patch-global-config.sh's jq/
envsubst merges into stdlib Python reproduces the old behavior one-for-one (create-if-
absent being the one deliberate change) and keeps the shim scripts' flags/exit codes.

ASSUMPTIONS FIXED HERE FOR THE IMPLEMENTER (plan.md does not spell out the exact argv
shape, only the subcommand names and behavior; this is the contract the tests bind to
-- change the CLI, change these tests deliberately, do not silently drift):

  settings   --target PATH [--skill-dir DIR] [--dry-run]
  claude-md  --target PATH [--template PATH] [--skill-dir DIR] [--dry-run]
             (--template overrides; default is resolved from <skill-dir>/templates/CLAUDE.md)
  hooks      --target PATH --skill-dir DIR [--dry-run]
             (honors WRIT_PLUGIN_LIST_CMD as a test-injection seam for the loaded-plugin
             refusal check, mirroring patch-global-config.sh's own WRIT_PLUGIN_LIST_CMD
             and writ-server-lib.sh's WRIT_HEALTH_CMD/WRIT_SERVE_CMD injection pattern)
  commands   --target DIR --skill-dir DIR [--dry-run]
  all        --settings-target PATH --claude-md-target PATH --commands-target DIR
             [--skill-dir DIR] [--dry-run]
  http-get   URL [--fail]
  http-post  URL BODY [--fail]

  --skill-dir, when omitted, resolves from the module's own __file__ two levels up
  (bin/lib/writ_install.py -> bin/lib -> bin -> skill root), exactly as
  patch-global-config.sh's SKILL_DIR derivation does today.

  Exit codes (unchanged from patch-global-config.sh per plan.md's Analysis section):
    0 patched / already up to date / dry-run success
    1 missing template, missing settings target (hooks), or the plugin-install refusal
    2 write failure

Every test runs the module as a subprocess under bare system `python3` (never the
project .venv) against tmp_path targets, exactly like every real caller (it must work
before the venv exists). The real ~/.claude is never touched.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

SKILL_ROOT = Path(__file__).resolve().parent.parent
MODULE = SKILL_ROOT / "bin" / "lib" / "writ_install.py"
TEMPLATES_DIR = SKILL_ROOT / "templates"
CLAUDE_MD_TEMPLATE = TEMPLATES_DIR / "CLAUDE.md"
SETTINGS_TEMPLATE = TEMPLATES_DIR / "settings.json"
COMMANDS_TEMPLATE_DIR = TEMPLATES_DIR / "commands"
PATCH_SCRIPT = SKILL_ROOT / "scripts" / "patch-global-config.sh"
INSTALL_COMMANDS_SCRIPT = SKILL_ROOT / "scripts" / "install-user-commands.sh"

SL_HOOK_REL = "hooks/scripts/writ-statusline.sh"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def run_module(*args: str, env: dict | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    """Invoke writ_install.py under bare system python3, cwd-independent."""
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["python3", str(MODULE), *args],
        capture_output=True, text=True, cwd=str(SKILL_ROOT), env=full_env, timeout=timeout,
    )


def _fake_install(tmp_path: Path, name: str = "writ") -> Path:
    """A minimal relocated Writ tree: the module itself, both shims, and the
    templates the module reads -- enough to prove item 9 (relocated-tree portability)
    and to exercise --skill-dir-derived paths (statusLine command, hooks merge)."""
    root = tmp_path / "opt" / name
    (root / "bin" / "lib").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "templates" / "commands").mkdir(parents=True)
    (root / "hooks" / "scripts").mkdir(parents=True)
    shutil.copy(MODULE, root / "bin" / "lib" / MODULE.name) if MODULE.exists() else None
    shutil.copy(PATCH_SCRIPT, root / "scripts" / PATCH_SCRIPT.name)
    shutil.copy(INSTALL_COMMANDS_SCRIPT, root / "scripts" / INSTALL_COMMANDS_SCRIPT.name)
    shutil.copy(CLAUDE_MD_TEMPLATE, root / "templates" / "CLAUDE.md")
    if SETTINGS_TEMPLATE.exists():
        shutil.copy(SETTINGS_TEMPLATE, root / "templates" / "settings.json")
    for cmd in COMMANDS_TEMPLATE_DIR.glob("*.md"):
        shutil.copy(cmd, root / "templates" / "commands" / cmd.name)
    (root / "hooks" / "scripts" / "writ-statusline.sh").write_text("#!/usr/bin/env bash\necho stub\n")
    return root


def _seed(path: Path, *, allow=None, deny=None, status_line=None, extra: dict | None = None) -> None:
    doc: dict = {"permissions": {"allow": allow or [], "deny": deny or []}}
    if status_line is not None:
        doc["statusLine"] = status_line
    if extra:
        doc.update(extra)
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _allow(path: Path) -> list[str]:
    return json.loads(path.read_text())["permissions"]["allow"]


def _run_shim(script: Path, *args: str, settings_target: Path, claude_md_target: Path,
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "WRIT_SETTINGS_TARGET": str(settings_target),
        "WRIT_CLAUDE_MD_TARGET": str(claude_md_target),
        **(extra_env or {}),
    }
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, cwd=str(script.parent.parent), env=env, timeout=60,
    )


# --------------------------------------------------------------------------- #
# 1. settings: create-if-absent
# --------------------------------------------------------------------------- #


class TestSettingsCreateIfAbsent:
    def test_creates_settings_json_when_absent(self, tmp_path):
        target = tmp_path / "settings.json"
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert target.is_file()

    def test_creates_parent_directories_when_absent(self, tmp_path):
        target = tmp_path / "nested" / "deeper" / "settings.json"
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert target.is_file()

    def test_no_backup_written_for_a_file_that_never_existed(self, tmp_path):
        target = tmp_path / "settings.json"
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert not list(tmp_path.glob("settings.json.bak.*")), (
            "a freshly created settings.json must not also get a .bak file"
        )

    def test_writ_allow_entries_present_in_fresh_file(self, tmp_path):
        target = tmp_path / "settings.json"
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        allow = _allow(target)
        assert any("writ-session.py" in a for a in allow)

    def test_writ_deny_entries_present_in_fresh_file(self, tmp_path):
        target = tmp_path / "settings.json"
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        deny = json.loads(target.read_text())["permissions"]["deny"]
        assert "AskUserQuestion" in deny

    def test_statusline_present_in_fresh_file(self, tmp_path):
        target = tmp_path / "settings.json"
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        sl = json.loads(target.read_text())["statusLine"]
        assert sl["type"] == "command"
        assert sl["command"].endswith(SL_HOOK_REL)


class TestSkillDirDefaultResolution:
    def test_settings_resolves_skill_dir_from_own_module_location_when_omitted(self, tmp_path):
        """--skill-dir omitted must resolve exactly as SKILL_DIR does in
        patch-global-config.sh: two levels up from the module's own file."""
        target = tmp_path / "settings.json"
        r = run_module("settings", "--target", str(target))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        cmd = json.loads(target.read_text())["statusLine"]["command"]
        assert cmd == f"bash {SKILL_ROOT}/{SL_HOOK_REL}"


# --------------------------------------------------------------------------- #
# 2. settings: idempotent merge
# --------------------------------------------------------------------------- #


class TestSettingsIdempotentMerge:
    def test_second_run_is_byte_identical(self, tmp_path):
        target = tmp_path / "settings.json"
        _seed(target, allow=["Bash(existing-user-entry *)"])
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        first = target.read_text()
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert target.read_text() == first, "a second run changed an already-patched file"

    def test_no_duplicate_allow_entries_after_second_run(self, tmp_path):
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        allow = _allow(target)
        assert len(allow) == len(set(allow)), "duplicate allow entries after a second run"

    def test_no_duplicate_deny_entries_after_second_run(self, tmp_path):
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        deny = json.loads(target.read_text())["permissions"]["deny"]
        assert len(deny) == len(set(deny)), "duplicate deny entries after a second run"

    def test_unrelated_keys_survive(self, tmp_path):
        target = tmp_path / "settings.json"
        _seed(target, allow=[], extra={"theme": "dark"})
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        doc = json.loads(target.read_text())
        assert doc.get("theme") == "dark"

    def test_original_entry_order_preserved(self, tmp_path):
        target = tmp_path / "settings.json"
        _seed(target, allow=["Bash(existing-user-entry *)", "Bash(another-entry *)"])
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        allow = _allow(target)
        assert allow[0] == "Bash(existing-user-entry *)"
        assert allow[1] == "Bash(another-entry *)"


# --------------------------------------------------------------------------- #
# 3. settings: stale-entry pruning
# --------------------------------------------------------------------------- #


class TestStaleEntryPruning:
    def test_legacy_wildcard_entries_removed(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=["Bash(*/.claude/skills/writ/*)", "Bash(*/analysis/Writ/*)"])
        r = run_module("settings", "--target", str(target), "--skill-dir", str(install))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        allow = _allow(target)
        assert "Bash(*/.claude/skills/writ/*)" not in allow
        assert "Bash(*/analysis/Writ/*)" not in allow

    def test_dead_writ_named_directory_entry_removed(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        dead = "Edit(/gone/somewhere/writ/**)"
        _seed(target, allow=[dead])
        run_module("settings", "--target", str(target), "--skill-dir", str(install))
        assert dead not in _allow(target)

    def test_live_second_checkout_entry_kept(self, tmp_path):
        install = _fake_install(tmp_path)
        other = _fake_install(tmp_path / "second")
        target = tmp_path / "settings.json"
        entry = f"Edit({other}/**)"
        _seed(target, allow=[entry])
        run_module("settings", "--target", str(target), "--skill-dir", str(install))
        assert entry in _allow(target), "a still-existing second checkout must not be pruned"

    def test_unrelated_dead_path_entry_kept(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        entry = "Edit(/gone/some-other-project/**)"
        _seed(target, allow=[entry])
        run_module("settings", "--target", str(target), "--skill-dir", str(install))
        assert entry in _allow(target), "pruning must be scoped to writ-named dead dirs only"


# --------------------------------------------------------------------------- #
# 4. settings: statusLine policy
# --------------------------------------------------------------------------- #


class TestStatusLinePolicy:
    def test_absent_statusline_added(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        run_module("settings", "--target", str(target), "--skill-dir", str(install))
        sl = json.loads(target.read_text())["statusLine"]
        assert sl["type"] == "command"
        assert sl["command"] == f"bash {install}/hooks/scripts/writ-statusline.sh"

    def test_existing_writ_statusline_refreshed_to_this_install(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[], status_line={
            "type": "command", "command": "bash /old/install/hooks/scripts/writ-statusline.sh",
        })
        run_module("settings", "--target", str(target), "--skill-dir", str(install))
        cmd = json.loads(target.read_text())["statusLine"]["command"]
        assert cmd == f"bash {install}/hooks/scripts/writ-statusline.sh"

    def test_foreign_statusline_left_untouched(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        foreign = {"type": "command", "command": "bash /usr/local/bin/my-statusline.sh"}
        _seed(target, allow=[], status_line=foreign)
        run_module("settings", "--target", str(target), "--skill-dir", str(install))
        assert json.loads(target.read_text())["statusLine"] == foreign

    def test_foreign_statusline_emits_informational_message(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[], status_line={"type": "command", "command": "bash /usr/local/bin/x.sh"})
        r = run_module("settings", "--target", str(target), "--skill-dir", str(install))
        out = (r.stdout + r.stderr).lower()
        assert "statusline" in out, f"no informational message about the foreign statusLine: {out!r}"


# --------------------------------------------------------------------------- #
# 5. claude-md render
# --------------------------------------------------------------------------- #


class TestClaudeMdRender:
    def test_substitutes_dollar_home_form(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("Home is $HOME/.claude\n")
        target = tmp_path / "CLAUDE.md"
        r = run_module("claude-md", "--target", str(target), "--template", str(template))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert target.read_text() == f"Home is {os.environ['HOME']}/.claude\n"

    def test_substitutes_braced_home_form(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("Home is ${HOME}/.claude\n")
        target = tmp_path / "CLAUDE.md"
        run_module("claude-md", "--target", str(target), "--template", str(template))
        assert target.read_text() == f"Home is {os.environ['HOME']}/.claude\n"

    def test_skips_write_when_target_already_matches(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("static content\n")
        target = tmp_path / "CLAUDE.md"
        target.write_text("static content\n")
        before = target.stat().st_mtime_ns
        run_module("claude-md", "--target", str(target), "--template", str(template))
        assert target.stat().st_mtime_ns == before, "an identical target must not be rewritten"
        assert not list(tmp_path.glob("CLAUDE.md.bak.*"))

    def test_backs_up_existing_different_target_before_replace(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("new content\n")
        target = tmp_path / "CLAUDE.md"
        target.write_text("old content\n")
        run_module("claude-md", "--target", str(target), "--template", str(template))
        backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "old content\n"
        assert target.read_text() == "new content\n"

    def test_backup_filename_is_timestamped(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("new content\n")
        target = tmp_path / "CLAUDE.md"
        target.write_text("old content\n")
        run_module("claude-md", "--target", str(target), "--template", str(template))
        backups = list(tmp_path.glob("CLAUDE.md.bak.*"))
        assert len(backups) == 1
        assert re.match(r"^CLAUDE\.md\.bak\.\d{14}$", backups[0].name), backups[0].name

    def test_creates_target_when_absent_with_no_backup(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("fresh\n")
        target = tmp_path / "CLAUDE.md"
        run_module("claude-md", "--target", str(target), "--template", str(template))
        assert target.read_text() == "fresh\n"
        assert not list(tmp_path.glob("CLAUDE.md.bak.*"))

    def test_missing_template_returns_one_and_writes_nothing(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        r = run_module("claude-md", "--target", str(target), "--template", str(tmp_path / "nope.tpl"))
        assert r.returncode == 1
        assert not target.exists()

    def test_default_template_resolves_from_skill_dir(self, tmp_path):
        install = _fake_install(tmp_path)
        (install / "templates" / "CLAUDE.md").write_text("Skill-dir template $HOME\n")
        target = tmp_path / "CLAUDE.md"
        r = run_module("claude-md", "--target", str(target), "--skill-dir", str(install))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert target.read_text() == f"Skill-dir template {os.environ['HOME']}\n"


# --------------------------------------------------------------------------- #
# 6. hooks: render, merge, refusal
# --------------------------------------------------------------------------- #


class TestHooksRenderAndMerge:
    def test_expands_writ_dir_variable(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r = run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        body = target.read_text()
        assert "${WRIT_DIR}" not in body
        assert str(install) in body

    def test_merges_all_twelve_events(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        doc = json.loads(target.read_text())
        assert len(doc.get("hooks") or {}) == 12

    def test_idempotent_second_run(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        first = target.read_text()
        run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        assert target.read_text() == first

    def test_no_duplicate_registrations_after_second_run(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        for event, groups in json.loads(target.read_text())["hooks"].items():
            cmds = [h["command"] for g in groups for h in g.get("hooks", [])]
            assert len(cmds) == len(set(cmds)), f"{event} has duplicate commands"

    def test_preserves_users_own_hook_on_same_event(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        doc = {
            "permissions": {"allow": [], "deny": []},
            "hooks": {"SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": "bash /my/own.sh"},
            ]}]},
        }
        target.write_text(json.dumps(doc, indent=2))
        run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        groups = json.loads(target.read_text())["hooks"]["SessionStart"]
        cmds = [h["command"] for g in groups for h in g.get("hooks", [])]
        assert "bash /my/own.sh" in cmds, "the user's own hook was clobbered"

    def test_refuses_when_loaded_plugin_resolves_to_this_install(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        listing = json.dumps([{"id": "writ@skills-dir", "enabled": True, "installPath": str(install)}])
        r = run_module(
            "hooks", "--target", str(target), "--skill-dir", str(install),
            env={"WRIT_PLUGIN_LIST_CMD": f"printf '%s' {shlex.quote(listing)}"},
        )
        assert r.returncode != 0

    def test_refusal_writes_nothing(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        before = target.read_text()
        listing = json.dumps([{"id": "writ@skills-dir", "enabled": True, "installPath": str(install)}])
        run_module(
            "hooks", "--target", str(target), "--skill-dir", str(install),
            env={"WRIT_PLUGIN_LIST_CMD": f"printf '%s' {shlex.quote(listing)}"},
        )
        assert target.read_text() == before
        assert "hooks" not in json.loads(target.read_text())

    def test_refusal_message_names_the_reason(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        listing = json.dumps([{"id": "writ@skills-dir", "enabled": True, "installPath": str(install)}])
        r = run_module(
            "hooks", "--target", str(target), "--skill-dir", str(install),
            env={"WRIT_PLUGIN_LIST_CMD": f"printf '%s' {shlex.quote(listing)}"},
        )
        out = (r.stdout + r.stderr).lower()
        assert "plugin" in out, f"refusal must say why: {out[:400]!r}"

    def test_plugin_at_a_different_path_does_not_block_seeding(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        other = json.dumps([{"id": "other@x", "enabled": True, "installPath": "/opt/unrelated"}])
        r = run_module(
            "hooks", "--target", str(target), "--skill-dir", str(install),
            env={"WRIT_PLUGIN_LIST_CMD": f"printf '%s' {shlex.quote(other)}"},
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert len(json.loads(target.read_text()).get("hooks") or {}) == 12

    def test_settings_without_a_hooks_key_still_merges(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"theme": "dark"}))
        r = run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert len(json.loads(target.read_text()).get("hooks") or {}) == 12


# --------------------------------------------------------------------------- #
# 7. commands copy
# --------------------------------------------------------------------------- #


class TestCommandsCopy:
    def test_copies_every_template_command(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "commands"
        r = run_module("commands", "--target", str(target), "--skill-dir", str(install))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        expected = {p.name for p in COMMANDS_TEMPLATE_DIR.glob("*.md")}
        assert expected, "templates/commands has no .md files to assert against"
        actual = {p.name for p in target.glob("*.md")}
        assert actual == expected

    def test_reports_the_count(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "commands"
        r = run_module("commands", "--target", str(target), "--skill-dir", str(install))
        count = len(list(COMMANDS_TEMPLATE_DIR.glob("*.md")))
        assert str(count) in r.stdout, f"expected the count {count} reported; got: {r.stdout!r}"

    def test_target_directory_created_if_absent(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "nested" / "commands"
        run_module("commands", "--target", str(target), "--skill-dir", str(install))
        assert target.is_dir()


# --------------------------------------------------------------------------- #
# 8. --dry-run
# --------------------------------------------------------------------------- #


class TestDryRun:
    def test_settings_dry_run_creates_no_file(self, tmp_path):
        target = tmp_path / "settings.json"
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT), "--dry-run")
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert not target.exists()

    def test_settings_dry_run_writes_no_backup(self, tmp_path):
        target = tmp_path / "settings.json"
        _seed(target, allow=["Bash(existing-user-entry *)"])
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT), "--dry-run")
        assert not list(tmp_path.glob("settings.json.bak.*"))

    def test_settings_dry_run_prints_a_diff(self, tmp_path):
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT), "--dry-run")
        combined = r.stdout + r.stderr
        assert "dry-run" in combined.lower()
        assert "+" in combined, "dry-run must show what would change, not just announce itself"

    def test_claude_md_dry_run_creates_no_file(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("content\n")
        target = tmp_path / "CLAUDE.md"
        run_module("claude-md", "--target", str(target), "--template", str(template), "--dry-run")
        assert not target.exists()

    def test_claude_md_dry_run_writes_no_backup_when_target_differs(self, tmp_path):
        template = tmp_path / "CLAUDE.md.tpl"
        template.write_text("new\n")
        target = tmp_path / "CLAUDE.md"
        target.write_text("old\n")
        run_module("claude-md", "--target", str(target), "--template", str(template), "--dry-run")
        assert target.read_text() == "old\n"
        assert not list(tmp_path.glob("CLAUDE.md.bak.*"))

    def test_hooks_dry_run_creates_no_hooks_block(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        run_module("hooks", "--target", str(target), "--skill-dir", str(install), "--dry-run")
        assert "hooks" not in json.loads(target.read_text())

    def test_hooks_dry_run_prints_a_diff(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r = run_module("hooks", "--target", str(target), "--skill-dir", str(install), "--dry-run")
        assert "dry-run" in (r.stdout + r.stderr).lower()


# --------------------------------------------------------------------------- #
# 9. Shim scripts preserve flags + exit codes + relocated-tree portability
# --------------------------------------------------------------------------- #


class TestShimScriptsPreserveContract:
    def test_patch_global_config_keeps_dry_run_flag(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r = _run_shim(
            install / "scripts" / "patch-global-config.sh", "--dry-run",
            settings_target=target, claude_md_target=tmp_path / "CLAUDE.md",
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_patch_global_config_keeps_hooks_flag(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r = _run_shim(
            install / "scripts" / "patch-global-config.sh", "--hooks",
            settings_target=target, claude_md_target=tmp_path / "CLAUDE.md",
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert len(json.loads(target.read_text()).get("hooks") or {}) == 12

    def test_patch_global_config_exit_zero_on_patch(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r = _run_shim(
            install / "scripts" / "patch-global-config.sh",
            settings_target=target, claude_md_target=tmp_path / "CLAUDE.md",
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"

    def test_patch_global_config_exit_one_on_missing_template(self, tmp_path):
        install = _fake_install(tmp_path)
        (install / "templates" / "CLAUDE.md").unlink()
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r = _run_shim(
            install / "scripts" / "patch-global-config.sh",
            settings_target=target, claude_md_target=tmp_path / "CLAUDE.md",
        )
        assert r.returncode == 1

    def test_install_user_commands_keeps_user_commands_dir_override(self, tmp_path):
        install = _fake_install(tmp_path)
        target_dir = tmp_path / "custom-commands"
        r = subprocess.run(
            ["bash", str(install / "scripts" / "install-user-commands.sh")],
            env={**os.environ, "USER_COMMANDS_DIR": str(target_dir)},
            capture_output=True, text=True, cwd=str(install), timeout=30,
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert target_dir.is_dir()

    def test_install_user_commands_prints_installed_per_file(self, tmp_path):
        install = _fake_install(tmp_path)
        target_dir = tmp_path / "custom-commands"
        r = subprocess.run(
            ["bash", str(install / "scripts" / "install-user-commands.sh")],
            env={**os.environ, "USER_COMMANDS_DIR": str(target_dir)},
            capture_output=True, text=True, cwd=str(install), timeout=30,
        )
        assert "installed:" in r.stdout

    def test_both_shims_work_from_a_relocated_install_tree(self, tmp_path):
        install = _fake_install(tmp_path / "relocated" / "second-level" / "writ-checkout")
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        r1 = _run_shim(
            install / "scripts" / "patch-global-config.sh",
            settings_target=target, claude_md_target=tmp_path / "CLAUDE.md",
        )
        assert r1.returncode == 0, f"{r1.stdout}\n{r1.stderr}"
        r2 = subprocess.run(
            ["bash", str(install / "scripts" / "install-user-commands.sh")],
            env={**os.environ, "USER_COMMANDS_DIR": str(tmp_path / "cmds")},
            capture_output=True, text=True, cwd=str(install), timeout=30,
        )
        assert r2.returncode == 0, f"{r2.stdout}\n{r2.stderr}"
        assert (tmp_path / "cmds").is_dir()


# --------------------------------------------------------------------------- #
# Exit codes (module-level, not just the shims)
# --------------------------------------------------------------------------- #


class TestExitCodes:
    def test_settings_zero_on_successful_write(self, tmp_path):
        target = tmp_path / "settings.json"
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert r.returncode == 0

    def test_settings_zero_when_already_up_to_date(self, tmp_path):
        target = tmp_path / "settings.json"
        run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert r.returncode == 0

    def test_claude_md_one_when_template_missing(self, tmp_path):
        target = tmp_path / "CLAUDE.md"
        r = run_module("claude-md", "--target", str(target), "--template", str(tmp_path / "missing.tpl"))
        assert r.returncode == 1

    def test_hooks_one_when_settings_target_missing(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "does-not-exist.json"
        r = run_module("hooks", "--target", str(target), "--skill-dir", str(install))
        assert r.returncode == 1

    def test_hooks_one_when_refused_by_a_loaded_plugin(self, tmp_path):
        install = _fake_install(tmp_path)
        target = tmp_path / "settings.json"
        _seed(target, allow=[])
        listing = json.dumps([{"id": "writ@skills-dir", "enabled": True, "installPath": str(install)}])
        r = run_module(
            "hooks", "--target", str(target), "--skill-dir", str(install),
            env={"WRIT_PLUGIN_LIST_CMD": f"printf '%s' {shlex.quote(listing)}"},
        )
        assert r.returncode == 1

    def test_settings_two_on_write_failure(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("running as root: a read-only directory does not block writes")
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        target = readonly_dir / "settings.json"
        _seed(target, allow=[])
        readonly_dir.chmod(0o500)
        try:
            r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
            assert r.returncode == 2
        finally:
            readonly_dir.chmod(0o700)


# --------------------------------------------------------------------------- #
# Boundary cases
# --------------------------------------------------------------------------- #


class TestBoundaryCases:
    def test_malformed_settings_json_is_refused_untouched(self, tmp_path):
        # Review fix: the module's core promise over the jq pipeline it replaced is
        # that a corrupt settings.json is REFUSED, never overwritten with nothing.
        target = tmp_path / "settings.json"
        target.write_text('{"permissions": {invalid')
        before = target.read_text()
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert r.returncode != 0, "a malformed settings.json must be a refusal, not a rewrite"
        assert "not valid JSON" in (r.stdout + r.stderr)
        assert target.read_text() == before, "the malformed file must be left byte-untouched"
        assert not list(tmp_path.glob("settings.json.bak*")), (
            "no backup should be minted for a refused write"
        )

    def test_empty_settings_file_is_still_patched(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text("{}")
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        doc = json.loads(target.read_text())
        assert doc["permissions"]["allow"]

    def test_settings_without_a_permissions_key_gets_one(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"theme": "dark"}))
        r = run_module("settings", "--target", str(target), "--skill-dir", str(SKILL_ROOT))
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        doc = json.loads(target.read_text())
        assert doc["theme"] == "dark"
        assert doc["permissions"]["allow"]


# --------------------------------------------------------------------------- #
# Direct module-CLI sanity for the http-get/http-post subcommands (the bash-level
# curl vs WRIT_NO_CURL=1 equivalence lives in tests/test_no_tool_prereqs.py -- not
# duplicated here).
# --------------------------------------------------------------------------- #


class TestHttpSubcommands:
    def test_http_get_prints_the_response_body(self, tmp_path):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *_a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            r = run_module("http-get", f"http://127.0.0.1:{port}/")
        finally:
            server.shutdown()
            server.server_close()
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert json.loads(r.stdout) == {"ok": True}

    def test_http_get_fail_mode_is_nonzero_and_empty_on_4xx(self, tmp_path):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"nope"}')

            def log_message(self, *_a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            r = run_module("http-get", f"http://127.0.0.1:{port}/", "--fail")
        finally:
            server.shutdown()
            server.server_close()
        assert r.returncode != 0
        assert r.stdout == ""


# --------------------------------------------------------------------------- #
# `all` subcommand smoke test
# --------------------------------------------------------------------------- #


class TestAllSubcommand:
    def test_all_runs_settings_claude_md_and_commands_in_one_call(self, tmp_path):
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        claude_md = tmp_path / "CLAUDE.md"
        commands = tmp_path / "commands"
        r = run_module(
            "all",
            "--settings-target", str(settings),
            "--claude-md-target", str(claude_md),
            "--commands-target", str(commands),
            "--skill-dir", str(install),
        )
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert settings.is_file()
        assert claude_md.is_file()
        assert commands.is_dir()
