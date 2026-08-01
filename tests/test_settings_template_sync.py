"""templates/settings.json: hook registrations for an install nothing auto-discovers.

SCOPE, verified before writing any of this: hooks are ALREADY global for a normal install.
`writ@skills-dir` is a user-scope plugin, and from an unrelated directory
`claude plugin details writ` reports `Hooks (12)`, which is why Writ's hooks fire in every
project with NO `hooks` block in ~/.claude/settings.json.

The gap this closes is narrower: Writ at a path that is neither ~/.claude/skills/* nor a
marketplace install is discovered by nothing, so hooks/hooks.json is never read and NO hooks
load -- no gate, no injection, no enforcement.

TWO DESIGN CONSTRAINTS, both tested here:

1. GENERATED, never hand-maintained. The template is 41 command paths across 12 events. A
   second hand-edited copy is the same shape as the defect fixed in ea5022f: one source moved
   and stale copies survived because nothing compared them. CHANGELOG.md:215 shows Writ has
   been here before ("keep registrations in sync between the two if you edit either").

2. SEEDING REFUSES UNDER A PLUGIN INSTALL. Two registration surfaces would very likely fire
   all 12 hooks twice: doubled injection, doubled gate evaluation, duplicated telemetry, and
   a single-use gate token one path could consume before the other reads it. The double-fire
   is NOT empirically proven (observing it needs an interactive session), so the design fails
   safe -- refusing costs nothing if CC deduplicates, and prevents a bad failure if it does
   not.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent
HOOKS_JSON = SKILL / "hooks" / "hooks.json"
TEMPLATE = SKILL / "templates" / "settings.json"
GENERATOR = SKILL / "scripts" / "render-settings-template.py"
PATCHER = SKILL / "scripts" / "patch-global-config.sh"

PLUGIN_VAR = "${CLAUDE_PLUGIN_ROOT}"
INSTALL_VAR = "${WRIT_DIR}"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _commands(doc: dict) -> dict[str, list[str]]:
    """{event: [command, ...]} flattened across matcher groups, in file order."""
    out: dict[str, list[str]] = {}
    for event, groups in (doc.get("hooks") or {}).items():
        cmds: list[str] = []
        for group in groups:
            for hook in group.get("hooks", []):
                cmds.append(hook.get("command", ""))
        out[event] = cmds
    return out


class TestTemplateExists:
    def test_the_template_is_present_and_valid_json(self):
        assert TEMPLATE.is_file(), f"{TEMPLATE} is missing"
        assert isinstance(_load(TEMPLATE), dict)

    def test_it_has_a_hooks_block(self):
        assert (_load(TEMPLATE).get("hooks") or {}), "template registers no hooks"

    def test_it_owns_only_hooks(self):
        """permissions and statusLine stay owned by patch-global-config.sh's own merge,
        so this file has exactly one job and cannot fight that step."""
        doc = _load(TEMPLATE)
        for key in ("permissions", "statusLine"):
            assert key not in doc, (
                f"template must not carry '{key}'; patch-global-config.sh owns it"
            )


class TestTemplateMatchesItsSource:
    def test_it_registers_the_same_events(self):
        assert set(_commands(_load(TEMPLATE))) == set(_commands(_load(HOOKS_JSON)))

    def test_it_registers_all_twelve_events(self):
        assert len(_commands(_load(TEMPLATE))) == 12

    def test_every_command_matches_modulo_the_path_variable(self):
        """Structural comparison, not text: reformatting either file must yield neither
        a false failure nor a false pass."""
        src = _commands(_load(HOOKS_JSON))
        tpl = _commands(_load(TEMPLATE))
        for event, src_cmds in src.items():
            expected = [c.replace(PLUGIN_VAR, INSTALL_VAR) for c in src_cmds]
            assert tpl[event] == expected, (
                f"{event} drifted from hooks/hooks.json; regenerate the template with "
                f"{GENERATOR.name}"
            )

    def test_it_never_references_the_plugin_root_variable(self):
        """CLAUDE_PLUGIN_ROOT is set only for plugin-loaded hooks; in settings.json it
        would expand to the empty string and every command would point at /hooks/..."""
        assert PLUGIN_VAR not in TEMPLATE.read_text()

    def test_it_references_the_install_dir_variable(self):
        assert INSTALL_VAR in TEMPLATE.read_text()


class TestGeneratorIsTheSourceOfTruth:
    def test_the_generator_exists(self):
        assert GENERATOR.is_file()

    def test_it_reproduces_the_committed_template_byte_for_byte(self):
        """The anti-drift guarantee: the committed artifact is exactly what the source
        produces, so `hooks/hooks.json` remains the only file to edit for a hook change."""
        r = subprocess.run(
            ["python3", str(GENERATOR), "--stdout"],
            capture_output=True, text=True, cwd=str(SKILL), timeout=30,
        )
        assert r.returncode == 0, f"generator failed: {r.stderr}"
        assert r.stdout == TEMPLATE.read_text(), (
            "templates/settings.json is stale; re-run the generator and commit the result"
        )

    def test_a_new_hook_in_the_source_makes_the_sync_check_fail(self, tmp_path):
        """Proves the sync check is load-bearing rather than vacuous.

        A copy of hooks.json gains an event; the generated output must differ from the
        committed template. Without this, a passing sync test could mean 'nothing compared'.
        """
        src = _load(HOOKS_JSON)
        src["hooks"]["SessionEnd"] = [
            {"matcher": "", "hooks": [
                {"type": "command", "command": f"bash {PLUGIN_VAR}/hooks/scripts/invented.sh"}
            ]}
        ]
        altered = tmp_path / "hooks.json"
        altered.write_text(json.dumps(src, indent=2))
        r = subprocess.run(
            ["python3", str(GENERATOR), "--stdout", "--source", str(altered)],
            capture_output=True, text=True, cwd=str(SKILL), timeout=30,
        )
        assert r.returncode == 0, r.stderr
        assert r.stdout != TEMPLATE.read_text(), (
            "the generator ignored its source file, so the sync test proves nothing"
        )


_HAVE_JQ = shutil.which("jq") is not None and shutil.which("envsubst") is not None
_needs_tools = pytest.mark.skipif(not _HAVE_JQ, reason="jq and envsubst required")


@pytest.fixture
def target(tmp_path):
    """A settings.json the patcher may write, never the developer's real one."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "theme": "dark",
        "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    }, indent=2))
    return p


def _patch(target: Path, *args: str, plugin_list: str = "[]") -> subprocess.CompletedProcess:
    """Run the patcher against an isolated target.

    WRIT_PLUGIN_LIST_CMD injects the plugin inventory, mirroring the WRIT_HEALTH_CMD /
    WRIT_SERVE_CMD injection seams already used by scripts/lib/writ-server-lib.sh, so the
    refusal path is testable without a real plugin install.
    """
    env = {
        **os.environ,
        "WRIT_SETTINGS_TARGET": str(target),
        "WRIT_CLAUDE_MD_TARGET": str(target.parent / "CLAUDE.md"),
        "WRIT_PLUGIN_LIST_CMD": f"printf '%s' {json.dumps(plugin_list)}",
    }
    return subprocess.run(
        ["bash", str(PATCHER), *args],
        capture_output=True, text=True, cwd=str(SKILL), env=env, timeout=60,
    )


def _seeded(target: Path) -> dict:
    """Assert the seeding actually happened, then return the parsed settings.

    Load-bearing: the patcher IGNORES an unrecognized argument, so before this feature
    exists `--hooks` is a silent no-op and every assertion about the written block passes
    vacuously (no hooks block means no duplicate commands, no unexpanded variable, and an
    unchanged file on a second run).
    """
    doc = _load(target)
    events = doc.get("hooks") or {}
    assert len(events) == 12, (
        f"expected 12 seeded hook events, got {len(events)}: the --hooks step did nothing"
    )
    return doc


@_needs_tools
class TestSeedingIsOptIn:
    def test_without_the_flag_no_hooks_block_is_written(self, target):
        """The script's documented promise: it does NOT touch the hooks block."""
        r = _patch(target)
        assert r.returncode == 0, r.stderr
        assert "hooks" not in _load(target), (
            "the default run wrote a hooks block; every existing invocation would start "
            "registering hooks"
        )

    def test_the_flag_merges_every_event(self, target):
        r = _patch(target, "--hooks")
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert len(_seeded(target)["hooks"]) == 12

    def test_the_install_path_is_expanded_not_left_as_a_variable(self, target):
        _patch(target, "--hooks")
        _seeded(target)
        body = target.read_text()
        assert INSTALL_VAR not in body and "WRIT_DIR" not in body, (
            "an unexpanded variable in settings.json makes every hook command invalid"
        )
        assert str(SKILL) in body, "commands must point at the real install directory"

    def test_it_is_idempotent(self, target):
        _patch(target, "--hooks")
        _seeded(target)
        first = target.read_text()
        r = _patch(target, "--hooks")
        assert r.returncode == 0
        assert target.read_text() == first, "a second run changed the file"

    def test_it_does_not_duplicate_registrations(self, target):
        _patch(target, "--hooks")
        _patch(target, "--hooks")
        for event, cmds in _seeded(target)["hooks"].items():
            flat = [h["command"] for g in cmds for h in g.get("hooks", [])]
            assert len(flat) == len(set(flat)), f"{event} has duplicate commands"

    def test_unrelated_keys_survive(self, target):
        _patch(target, "--hooks")
        doc = _seeded(target)
        assert doc.get("theme") == "dark"
        assert "Bash(ls:*)" in doc["permissions"]["allow"]

    def test_a_users_own_hook_survives(self, target):
        doc = _load(target)
        doc["hooks"] = {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": "bash /my/own.sh"}]}
        ]}
        target.write_text(json.dumps(doc, indent=2))

        _patch(target, "--hooks")
        _seeded(target)
        cmds = _commands(_load(target))["SessionStart"]
        assert "bash /my/own.sh" in cmds, "the user's own hook was clobbered"


@_needs_tools
class TestSeedingRefusesUnderAPluginInstall:
    def _listing(self) -> str:
        return json.dumps([{
            "id": "writ@skills-dir", "version": "1.5.1", "scope": "user",
            "enabled": True, "installPath": str(SKILL),
        }])

    def test_it_exits_non_zero(self, target):
        r = _patch(target, "--hooks", plugin_list=self._listing())
        assert r.returncode != 0, (
            "seeding proceeded under a plugin install; both surfaces would register the "
            "same 12 hooks"
        )

    def test_it_writes_no_hooks_block(self, tmp_path):
        """A contrast, not a bare absence: the SAME invocation seeds 12 events without the
        plugin present, so 'no hooks block' here is attributable to the refusal rather than
        to the feature not existing.
        """
        def fresh(name: str) -> Path:
            p = tmp_path / name
            p.write_text(json.dumps({"theme": "dark"}, indent=2))
            return p

        without = fresh("no-plugin.json")
        _patch(without, "--hooks", plugin_list="[]")
        assert len(_load(without).get("hooks") or {}) == 12, (
            "control case did not seed, so this test cannot attribute anything"
        )

        with_plugin = fresh("with-plugin.json")
        _patch(with_plugin, "--hooks", plugin_list=self._listing())
        assert "hooks" not in _load(with_plugin)

    def test_the_message_is_actionable(self, target):
        r = _patch(target, "--hooks", plugin_list=self._listing())
        out = (r.stdout + r.stderr).lower()
        assert "plugin" in out, f"refusal must say why: {out[:400]}"

    def test_a_plugin_at_a_different_path_does_not_block_seeding(self, target):
        """Only a plugin resolving to THIS install is a double registration."""
        other = json.dumps([{
            "id": "somethingelse@skills-dir", "scope": "user",
            "enabled": True, "installPath": "/opt/unrelated",
        }])
        r = _patch(target, "--hooks", plugin_list=other)
        assert r.returncode == 0, f"{r.stdout}\n{r.stderr}"
        assert len(_load(target).get("hooks") or {}) == 12


class TestDoctorDetectsDoubleRegistration:
    """If the condition arises another way (a move into ~/.claude/skills after seeding),
    it must be visible rather than silent."""

    def _opts(self):
        from writ.session.doctor import DoctorOptions
        return DoctorOptions()

    def test_a_double_registration_is_reported(self, tmp_path, monkeypatch):
        from writ.session import doctor

        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": {
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": f"bash {SKILL}/hooks/scripts/x.sh"}
            ]}]
        }}))
        monkeypatch.setattr(doctor, "_global_settings_path", lambda: settings)
        monkeypatch.setattr(doctor, "_loaded_plugin_paths", lambda: [str(SKILL)])

        res = doctor.check_duplicate_hook_registration(self._opts())
        assert res.status != "ok", "a double registration must not report ok"
        assert "hook" in res.detail.lower()

    def test_the_normal_plugin_only_install_is_clean(self, tmp_path, monkeypatch):
        from writ.session import doctor

        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"theme": "dark"}))  # no hooks block
        monkeypatch.setattr(doctor, "_global_settings_path", lambda: settings)
        monkeypatch.setattr(doctor, "_loaded_plugin_paths", lambda: [str(SKILL)])

        assert doctor.check_duplicate_hook_registration(self._opts()).status == "ok"

    def test_settings_hooks_without_a_plugin_is_clean(self, tmp_path, monkeypatch):
        """The arbitrary-path install this feature exists for: settings.json is the ONLY
        registration surface, which is correct and must not warn."""
        from writ.session import doctor

        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({"hooks": {
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": f"bash {SKILL}/hooks/scripts/x.sh"}
            ]}]
        }}))
        monkeypatch.setattr(doctor, "_global_settings_path", lambda: settings)
        monkeypatch.setattr(doctor, "_loaded_plugin_paths", lambda: [])

        assert doctor.check_duplicate_hook_registration(self._opts()).status == "ok"

    def test_the_check_is_registered_in_the_run_order(self):
        from writ.session.doctor import _CHECKS
        assert "duplicate-hook-registration" in [name for name, _ in _CHECKS]


# docs/install-writ.md states there is NO settings.json hook seeding step, which this
# change makes conditionally untrue. The doc is corrected directly and NOT pinned by a test:
# a standing project directive prohibits assertions on documentation content, because a
# suite that goes red on reworded prose stops meaning "something is broken". Two such tests
# were removed from tests/test_plugin_manifest.py earlier for the same reason.
