"""The global-config patcher must work for a Writ installed at ANY path.

Two entries in scripts/patch-global-config.sh were hardcoded to one developer's
home (`Edit(/home/original-dev/.claude/skills/writ/**)` and an `analysis/Writ`
sibling), so every other install got allow rules pointing at directories it does
not have: self-edits prompted, and the dead entries accumulated across moves.

The entries are now derived from the resolved install dir ($SKILL_DIR, which the
script computes from its own location), and location-tied entries from an earlier
version are pruned when their directory is gone.

Each case copies the real script into a fake install tree under tmp_path, so
SKILL_DIR resolves somewhere new and the real ~/.claude is never touched.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
PATCH_SCRIPT = SKILL_ROOT / "scripts" / "patch-global-config.sh"

# No tool-presence skip guard: the patcher is a shim over the stdlib-only
# bin/lib/writ_install.py, so there is nothing left to skip for.
INSTALL_MODULE = SKILL_ROOT / "bin" / "lib" / "writ_install.py"


def _fake_install(tmp_path: Path, name: str = "writ") -> Path:
    """A minimal relocated Writ tree containing what the patcher needs."""
    root = tmp_path / "opt" / name
    (root / "scripts").mkdir(parents=True)
    (root / "templates").mkdir(parents=True)
    (root / "bin" / "lib").mkdir(parents=True)
    shutil.copy(PATCH_SCRIPT, root / "scripts" / PATCH_SCRIPT.name)
    # The shim resolves the module from its own SKILL_DIR, so a relocated tree must
    # carry it: this is what makes the relocation test exercise the real code path.
    shutil.copy(INSTALL_MODULE, root / "bin" / "lib" / INSTALL_MODULE.name)
    shutil.copy(SKILL_ROOT / "templates" / "CLAUDE.md", root / "templates" / "CLAUDE.md")
    return root


def _run(install: Path, settings: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "WRIT_SETTINGS_TARGET": str(settings),
        "WRIT_CLAUDE_MD_TARGET": str(settings.parent / "CLAUDE.md"),
    }
    return subprocess.run(
        ["bash", str(install / "scripts" / "patch-global-config.sh")],
        capture_output=True, text=True, cwd=str(install), env=env, timeout=60,
    )


def _allow(settings: Path) -> list[str]:
    return json.loads(settings.read_text())["permissions"]["allow"]


def _seed(settings: Path, allow: list[str], status_line=None) -> None:
    d: dict = {"permissions": {"allow": allow, "deny": []}}
    if status_line is not None:
        d["statusLine"] = {"type": "command", "command": status_line}
    settings.write_text(json.dumps(d, indent=2) + "\n")


def _foreign_abs_paths(entry: str, install: Path) -> list[str]:
    """Absolute paths in a permission entry that do not live under `install`.

    Only a `/` that starts a path counts: one preceded by a word character or `*` belongs
    to a relocatable wildcard like `Bash(bash *writ/bin/check-gates.sh*)`, which names no
    absolute location and is exactly what the patcher SHOULD emit.
    """
    tokens = re.findall(r"(?<![\w*.])/[^\s()*]+", entry)
    return [t for t in tokens if not t.startswith(str(install))]


class TestDerivedFromInstallDir:
    def test_self_edit_entry_points_at_this_install(self, tmp_path):
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        _seed(settings, ["Bash(existing-user-entry *)"])

        assert _run(install, settings).returncode == 0
        assert f"Edit({install}/**)" in _allow(settings)

    def test_self_run_entry_points_at_this_install(self, tmp_path):
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        _seed(settings, [])

        _run(install, settings)
        assert f"Bash({install}/*)" in _allow(settings)

    def test_no_hardcoded_path_outside_this_install_is_written(self, tmp_path):
        """Every absolute path the patch writes must live under THIS install.

        The earlier version of this test excluded the runner's own home from the check,
        which is exactly where the bug it guards against lived: the hardcoded entry was
        `Edit(/home/original-dev/.claude/skills/writ/**)`, so on that developer's machine
        the filter skipped it and the test passed against the very defect. Adversarial
        review caught it. Anchoring on the install dir instead of on "not my home" means
        the assertion holds no matter who runs it.
        """
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        _seed(settings, [])

        _run(install, settings)
        offenders = [e for e in _allow(settings) if _foreign_abs_paths(e, install)]
        assert offenders == [], f"absolute paths outside this install leaked in: {offenders}"

    def test_the_check_above_would_catch_a_hardcoded_home(self, tmp_path):
        """Prove the guard has teeth: a planted foreign entry must be reported.

        Without this, test_no_hardcoded_path_outside_this_install_is_written could pass
        because its detector is broken rather than because the config is clean.
        """
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        planted = "Edit(/home/someone-else/.claude/skills/writ/**)"
        _seed(settings, [planted])

        # The pruner would drop this one (writ-named dir that does not exist), so assert on
        # the DETECTOR rather than on the patched file: the point is that the check used by
        # the test above reports a hardcoded home instead of skipping it.
        assert _foreign_abs_paths(planted, install), "the detector must flag a foreign absolute path"
        assert not _foreign_abs_paths(f"Edit({install}/**)", install), (
            "the detector must not flag this install's own derived entry"
        )
        assert not _foreign_abs_paths("Bash(bash *writ/bin/check-gates.sh*)", install), (
            "relocatable wildcards carry no absolute path and must not be flagged"
        )

    def test_statusline_points_at_this_install(self, tmp_path):
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        _seed(settings, [], status_line="bash /gone/writ/hooks/scripts/writ-statusline.sh")

        _run(install, settings)
        cmd = json.loads(settings.read_text())["statusLine"]["command"]
        assert cmd == f"bash {install}/hooks/scripts/writ-statusline.sh"


class TestStaleEntryPruning:
    def test_dead_writ_install_entry_removed(self, tmp_path):
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        dead = "Edit(/home/olduser/.claude/skills/writ/**)"
        _seed(settings, [dead])

        _run(install, settings)
        assert dead not in _allow(settings), "an allow rule for a vanished install must be pruned"

    def test_legacy_wildcard_entries_removed(self, tmp_path):
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        _seed(settings, ["Bash(*/.claude/skills/writ/*)", "Bash(*/analysis/Writ/*)"])

        _run(install, settings)
        allow = _allow(settings)
        assert "Bash(*/.claude/skills/writ/*)" not in allow
        assert "Bash(*/analysis/Writ/*)" not in allow

    def test_live_second_checkout_is_kept(self, tmp_path):
        """A second checkout that still exists is a real dev path, not residue."""
        install = _fake_install(tmp_path)
        other = _fake_install(tmp_path / "second")
        settings = tmp_path / "settings.json"
        entry = f"Edit({other}/**)"
        _seed(settings, [entry])

        _run(install, settings)
        assert entry in _allow(settings)

    def test_unrelated_user_entry_is_kept(self, tmp_path):
        """Pruning is scoped to writ-named dirs; a foreign dead path is the user's business."""
        install = _fake_install(tmp_path)
        settings = tmp_path / "settings.json"
        entry = "Edit(/gone/some-other-project/**)"
        _seed(settings, [entry, "Bash(existing-user-entry *)"])

        _run(install, settings)
        allow = _allow(settings)
        assert entry in allow
        assert "Bash(existing-user-entry *)" in allow


def test_rerun_is_idempotent(tmp_path):
    install = _fake_install(tmp_path)
    settings = tmp_path / "settings.json"
    _seed(settings, [])

    _run(install, settings)
    first = settings.read_text()
    _run(install, settings)
    assert settings.read_text() == first, "a second patch run must not change settings"


class TestSourceShape:
    def test_no_hardcoded_developer_home(self):
        src = PATCH_SCRIPT.read_text()
        entries = [
            ln.strip() for ln in src.splitlines()
            if ln.strip().startswith(('"Edit(', '"Bash(', '"Write('))
        ]
        offenders = [e for e in entries if "/home/" in e]
        assert offenders == [], f"permission entries must be derived, not hardcoded: {offenders}"
