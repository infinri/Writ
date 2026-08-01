"""The five writ-* sub-agent roles must live at agents/, not .claude/agents/.

MEASURED, by installing a modified copy from a local-path marketplace into a throwaway HOME
and reading Claude Code's own component inventory:

    files at .claude/agents/ + a manifest array of 5  ->  Agents (0)
    files at agents/, no manifest key                ->  Agents (5)  writ-explorer,
                                                          writ-reviewer, writ-planner,
                                                          writ-implementer, writ-test-writer

So the manifest array registers nothing and the documented plugin-root `agents/` location
works. The docs agree: "All other directories must be at the plugin root level."

WHO WAS ACTUALLY AFFECTED. Not everyone outside the repo, which is what a note in this
project previously claimed. scripts/bootstrap.sh symlinks the role files into
~/.claude/agents/, which are USER-level agents available in every project, so
standalone-bootstrap users have had them all along. scripts/bootstrap-plugin.sh creates no
such symlinks, so it is plugin-path installs that have never had the writ-* roles.

NOT IN SCOPE, after reading it: hooks/scripts/writ-dispatch-discipline.sh needs no change.
It allows any subagent_type not in GENERIC = {"", "general-purpose", "explore", "claude"},
so both `writ-explorer` and `writ:writ-explorer` already pass.

THE MIGRATION HAZARD this pins: after the move, existing ~/.claude/agents/writ-*.md symlinks
point at deleted files. Re-running bootstrap.sh repairs them (link_all relinks any target
that is a symlink), but an upgrade without a re-run leaves five dangling links, so the doctor
check below exists to make that visible instead of silent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
AGENTS_DIR = SKILL / "agents"
OLD_AGENTS_DIR = SKILL / ".claude" / "agents"
PLUGIN_MANIFEST = SKILL / ".claude-plugin" / "plugin.json"
BOOTSTRAP = SKILL / "scripts" / "bootstrap.sh"

ROLES = [
    "writ-explorer",
    "writ-implementer",
    "writ-planner",
    "writ-reviewer",
    "writ-test-writer",
]

# The stale directory string the sweep below looks for.
STALE_AGENTS_PATH = ".claude/agents"

# tests/test_gate_claude_dir_scope.py uses ".claude/agents/..." as SAMPLE PATHS to exercise
# the gate's glob matching (including `..` traversal). Its subject is the matcher, not the
# location of the real role files, so it is exempt from the no-stale-path sweep.
_SWEEP_EXEMPT = {"test_gate_claude_dir_scope.py", Path(__file__).name}


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


class TestLayout:
    def test_the_agents_dir_exists_at_the_plugin_root(self):
        assert AGENTS_DIR.is_dir(), (
            f"{AGENTS_DIR} is missing; plugin agents must live at the plugin root's agents/"
        )

    @pytest.mark.parametrize("role", ROLES)
    def test_each_role_file_is_present(self, role):
        assert (AGENTS_DIR / f"{role}.md").is_file()

    def test_exactly_the_five_roles_are_present(self):
        found = sorted(p.stem for p in AGENTS_DIR.glob("*.md"))
        assert found == sorted(ROLES), f"unexpected role set: {found}"

    def test_the_old_project_agents_dir_is_gone(self):
        """While it exists, the roles register as PROJECT agents, so they exist only when
        the session cwd is the Writ repo."""
        assert not OLD_AGENTS_DIR.exists(), (
            f"{OLD_AGENTS_DIR} still exists; the roles would still load as project agents"
        )

    @pytest.mark.parametrize("role", ROLES)
    def test_the_front_matter_name_matches_the_filename(self, role):
        """role_id is derived from the file (ROL-{NAME}-001, WRIT- stripped), so the name
        inside must keep matching the basename or the graph node would be renamed by a move.

        Asserting `Path(f"{role}.md").name == f"{role}.md"` would be a tautology that passes
        without the file existing, so this reads the file and compares its declared name.
        """
        body = (AGENTS_DIR / f"{role}.md").read_text()
        assert f"name: {role}" in body, (
            f"{role}.md must declare `name: {role}` in its front matter"
        )


class TestManifest:
    def test_it_declares_no_agents_key(self):
        """The declaration is what yields Agents (0); auto-discovery at agents/ is what works."""
        assert "agents" not in json.loads(PLUGIN_MANIFEST.read_text()), (
            "plugin.json still declares agents; the array registers nothing"
        )

    def test_it_still_declares_commands(self):
        """Only the agents directory was wrong. .claude/commands is declared and verified
        working (Skills (1) writ-approve), so it must not be collateral damage."""
        assert json.loads(PLUGIN_MANIFEST.read_text()).get("commands")


class TestNoSourceStillPointsAtTheOldPath:
    # Only unambiguous PATH CONSTRUCTION against the install root. Deliberately not a bare
    # ".claude/agents" text search: that also hits `$HOME/.claude/agents` (the user-level
    # symlink TARGET, which is still correct and still used), comments explaining the move,
    # and docstrings recounting the old layout. A sweep that flags the comment documenting
    # the fix is noise, not a signal.
    _STALE_CONSTRUCTIONS = (
        '/ ".claude" / "agents"',      # Python: Path(...) / ".claude" / "agents"
        'WRIT_DIR/.claude/agents',     # shell: "$WRIT_DIR/.claude/agents"
        'WRIT_DIR}/.claude/agents',    # shell: "${WRIT_DIR}/.claude/agents"
        'SKILL_DIR/.claude/agents',
        'CLAUDE_PLUGIN_ROOT}/.claude/agents',
    )

    def test_no_source_resolves_the_roles_from_the_old_dir(self):
        offenders = []
        for root in ("scripts", "hooks", "bin", "writ", "tests"):
            for path in sorted((SKILL / root).rglob("*")):
                if path.suffix not in {".py", ".sh"} or not path.is_file():
                    continue
                if path.name in _SWEEP_EXEMPT:
                    continue
                for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                    # ~/.claude/agents is the user-level symlink target and is still correct
                    # (bootstrap.sh links into it, and doctor checks it for dangling links).
                    if "home()" in line or "HOME" in line:
                        continue
                    if any(frag in line for frag in self._STALE_CONSTRUCTIONS):
                        offenders.append(f"{path.relative_to(SKILL)}:{i}")
        assert offenders == [], (
            f"these still build a path to the old role directory: {offenders}"
        )

    def test_the_manifest_does_not_name_the_old_dir(self):
        assert STALE_AGENTS_PATH not in PLUGIN_MANIFEST.read_text()


class TestGraphRoundTripFollowsTheMove:
    """Both directions must move together: if only one did, `export --check` would compare
    an empty directory and report success while detecting nothing."""

    def test_ingest_resolves_the_new_location(self):
        mod = _load(SKILL / "scripts" / "ingest_subagent_roles.py")
        assert mod.AGENTS_DIR == AGENTS_DIR, (
            f"ingest reads {mod.AGENTS_DIR}, expected {AGENTS_DIR}"
        )

    def test_export_resolves_the_new_location(self):
        mod = _load(SKILL / "scripts" / "export_subagent_roles.py")
        assert mod.AGENTS_DIR == AGENTS_DIR, (
            f"export writes {mod.AGENTS_DIR}, expected {AGENTS_DIR}"
        )

    def test_ingest_finds_all_five_role_files_there(self):
        mod = _load(SKILL / "scripts" / "ingest_subagent_roles.py")
        assert sorted(p.stem for p in mod.AGENTS_DIR.glob("*.md")) == sorted(ROLES)


class TestBootstrapLinksFromTheNewSource:
    def test_it_links_the_new_dir(self):
        """link_all's source must be the new path, so a re-run repairs symlinks that
        dangle after the move."""
        body = BOOTSTRAP.read_text()
        assert 'link_all "$WRIT_DIR/agents"' in body, (
            "bootstrap.sh must link role files from agents/; otherwise a re-run recreates "
            "symlinks pointing at the deleted .claude/agents"
        )

    def test_it_no_longer_links_the_old_dir(self):
        assert 'link_all "$WRIT_DIR/.claude/agents"' not in BOOTSTRAP.read_text()


class TestDoctorReportsDanglingRoleSymlinks:
    """The upgrade failure mode: ~/.claude/agents/writ-*.md pointing at deleted files."""

    def _opts(self):
        from writ.session.doctor import DoctorOptions
        return DoctorOptions()

    def test_a_dangling_symlink_is_reported(self, tmp_path, monkeypatch):
        from writ.session import doctor

        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "writ-explorer.md").symlink_to(tmp_path / "gone" / "writ-explorer.md")
        monkeypatch.setattr(doctor, "_user_agents_dir", lambda: agents)

        res = doctor.check_role_symlinks(self._opts())
        assert res.status != "ok"
        assert "writ-explorer" in res.detail

    def test_resolving_symlinks_are_clean(self, tmp_path, monkeypatch):
        from writ.session import doctor

        real = tmp_path / "src"
        real.mkdir()
        (real / "writ-explorer.md").write_text("x")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "writ-explorer.md").symlink_to(real / "writ-explorer.md")
        monkeypatch.setattr(doctor, "_user_agents_dir", lambda: agents)

        assert doctor.check_role_symlinks(self._opts()).status == "ok"

    def test_no_symlinks_at_all_is_clean(self, tmp_path, monkeypatch):
        """A plugin-only install has no user-level links; that is correct, not a fault."""
        from writ.session import doctor

        agents = tmp_path / "agents"
        agents.mkdir()
        monkeypatch.setattr(doctor, "_user_agents_dir", lambda: agents)
        assert doctor.check_role_symlinks(self._opts()).status == "ok"

    def test_a_missing_agents_dir_is_clean(self, tmp_path, monkeypatch):
        from writ.session import doctor
        monkeypatch.setattr(doctor, "_user_agents_dir", lambda: tmp_path / "nope")
        assert doctor.check_role_symlinks(self._opts()).status == "ok"

    def test_the_check_is_registered(self):
        from writ.session.doctor import _CHECKS
        assert "role-symlinks" in [name for name, _ in _CHECKS]


@pytest.mark.integration
class TestRealInstallLoadsTheAgents:
    """Acceptance from Claude Code's own inventory: reading the manifest cannot prove this.

    The manifest validated clean while reporting Agents (0), which is exactly why this
    installs for real into a throwaway HOME.
    """

    @pytest.fixture(scope="class")
    def installed(self, tmp_path_factory):
        if shutil.which("claude") is None:
            pytest.skip("claude CLI not on PATH")
        root = tmp_path_factory.mktemp("agents-install")
        home, mkt = root / "home", root / "mkt"
        home.mkdir()
        shutil.copytree(
            SKILL, mkt,
            ignore=shutil.ignore_patterns(".git", ".venv", "var", "__pycache__", "node_modules"),
        )
        env = {**os.environ, "HOME": str(home)}
        add = subprocess.run(["claude", "plugin", "marketplace", "add", str(mkt)],
                             capture_output=True, text=True, env=env)
        assert add.returncode == 0, f"{add.stdout}{add.stderr}"
        inst = subprocess.run(["claude", "plugin", "install", "writ@writ"],
                              capture_output=True, text=True, env=env)
        assert inst.returncode == 0, f"{inst.stdout}{inst.stderr}"
        details = subprocess.run(["claude", "plugin", "details", "writ"],
                                 capture_output=True, text=True, env=env)
        listing = subprocess.run(["claude", "plugin", "list", "--json"],
                                 capture_output=True, text=True, env=env)
        return {"details": details.stdout, "listing": json.loads(listing.stdout)}

    def test_all_five_agents_load(self, installed):
        import re as _re
        m = _re.search(r"Agents \((\d+)\)", installed["details"])
        assert m, f"no Agents count in inventory:\n{installed['details']}"
        assert int(m.group(1)) == 5, (
            f"expected 5 agents, got {m.group(1)}\n{installed['details']}"
        )

    def test_the_five_expected_names_are_listed(self, installed):
        for role in ROLES:
            assert role in installed["details"], f"{role} missing from the inventory"

    def test_the_hooks_fix_from_the_previous_cycle_is_intact(self, installed):
        import re as _re
        m = _re.search(r"Hooks \((\d+)\)", installed["details"])
        assert m and int(m.group(1)) == 12, "the agents move broke hook loading"

    def test_the_install_reports_no_errors(self, installed):
        errors = []
        for entry in installed["listing"]:
            errors.extend(entry.get("errors") or [])
        assert errors == [], f"install reported errors: {errors}"
