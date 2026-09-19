"""Cycle F skeletons: the hook-registration check must not fail a correct install.

`writ doctor` reports `cc-hook-registration` FAIL on this machine while every hook
demonstrably fires, including the Bash gate that refused three of my own commands
during this session.

THE MECHANISM, confirmed by calling the function rather than reading its message.
`_cc_registration_ok` (`doctor.py:601-628`) does `hooks_ref = plugin.get("hooks", "")`
then `hooks_path = (_PACKAGE_ROOT / str(hooks_ref).lstrip("./")).resolve()`. This
repo's `.claude-plugin/plugin.json` has no `hooks` key (its keys are `$schema, author,
commands, description, keywords, license, name, version`), so `hooks_ref` is `""`,
`hooks_path` becomes `_PACKAGE_ROOT` itself, and `open()` on a directory raises
`IsADirectoryError`, which `except (OSError, ValueError)` converts into
`return (False, [str(hooks_path)])`. Running it returns
`ok=False, missing=['<package root>']`. A DIRECTORY reported as a missing SCRIPT is
the tell, and one test below asserts exactly that shape can never recur.

THE INSTALL IS FINE, so the check's premise is what is wrong. `hooks/hooks.json`
exists (9952 bytes) and Claude Code locates a plugin's hooks by CONVENTION rather than
from a manifest key, so a manifest without `hooks` is normal.

DEFAULTING, NOT MUTING. Returning ok-when-the-key-is-absent would delete the coverage
this check exists for: a hook script that is missing or non-executable is a real
failure worth catching. Five of the nine tests here are must-not-regress for exactly
that reason, because a default that swallowed a genuine failure would be worse than
the false positive it replaces.

Per ENF-GATE-007: skeletons written and approved before implementation. The gate had
advanced to "implementation" on phase bookkeeping carried over from the previous
cycle, not because skeletons existed; writing them anyway is the point of the rule.
Per TEST-ISOLATE-003: every test builds a synthetic package root under tmp_path and
monkeypatches `doctor._PACKAGE_ROOT`, so none of them reads the developer's real
plugin manifest or hook scripts.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _require(module, *names) -> None:
    missing = [n for n in names if not hasattr(module, n)]
    if missing:
        pytest.fail(f"skeleton: {module.__name__} has no {', '.join(missing)} yet")


def _fake_install(
    root: Path,
    *,
    manifest: dict | None = None,
    hooks_doc: dict | None = None,
    hooks_rel: str = "hooks/hooks.json",
    script_rel: str = "hooks/scripts/probe.sh",
    write_script: bool = True,
    script_executable: bool = True,
) -> Path:
    """Build a synthetic plugin install and return its root.

    `manifest` defaults to a manifest with NO `hooks` key, which is the shape this
    repo actually ships and the one that currently fails.
    """
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    if manifest is None:
        manifest = {"name": "writ", "version": "0.0.0", "commands": "./commands"}
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest))

    if hooks_doc is None:
        hooks_doc = {
            "hooks": {
                "PreToolUse": [
                    {"hooks": [{"type": "command",
                                "command": "bash ${CLAUDE_PLUGIN_ROOT}/" + script_rel}]}
                ]
            }
        }
    hooks_path = root / hooks_rel
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(hooks_doc))

    if write_script:
        script = root / script_rel
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env bash\nexit 0\n")
        script.chmod(0o755 if script_executable else 0o644)
    return root


def _check(monkeypatch, root: Path):
    """Run the real check against a synthetic root."""
    from writ.session import doctor

    monkeypatch.setattr(doctor, "_PACKAGE_ROOT", root)
    return doctor._cc_registration_ok()


# --------------------------------------------------------------------------- #
# Capability 1, 7: the default, and an explicit key still winning
# --------------------------------------------------------------------------- #

class TestTheManifestNeedNotDeclareHooks:

    def test_it_passes_when_the_manifest_has_no_hooks_key(self, tmp_path, monkeypatch) -> None:
        """THE DEFECT. This is the shape this repo ships, and it currently fails."""
        root = _fake_install(tmp_path)
        ok, missing = _check(monkeypatch, root)
        assert ok, (
            f"a manifest with no `hooks` key was reported broken: {missing}"
        )

    def test_the_real_repo_passes(self, monkeypatch) -> None:
        """The end-to-end claim, against the actual install whose hooks all fire."""
        from writ.session import doctor

        ok, missing = doctor._cc_registration_ok()
        assert ok, f"this repo's own install is reported broken: {missing}"

    def test_an_explicit_hooks_key_still_wins(self, tmp_path, monkeypatch) -> None:
        """A default must not shadow a declaration. The manifest points somewhere
        non-standard and the check must follow it, not the convention."""
        root = _fake_install(
            tmp_path,
            manifest={"name": "writ", "hooks": "./config/other-hooks.json"},
            hooks_rel="config/other-hooks.json",
            script_rel="hooks/scripts/declared.sh",
        )
        # A file at the DEFAULT path that would fail, to prove the declared one was
        # the one read.
        (root / "hooks").mkdir(exist_ok=True)
        (root / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [
                {"hooks": [{"type": "command",
                            "command": "bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/absent.sh"}]}
            ]}})
        )
        ok, missing = _check(monkeypatch, root)
        assert ok, (
            f"the check read the default path instead of the declared one: {missing}"
        )


# --------------------------------------------------------------------------- #
# Capability 2, 3, 4, 5: the failures that must keep failing
# --------------------------------------------------------------------------- #

class TestGenuineFailuresStillFail:
    """Five must-not-regress cases. A default that swallowed any of these would be
    worse than the false positive it replaces, because a muted check is
    indistinguishable from a passing one.
    """

    def test_an_unreadable_manifest_still_fails(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path)
        (root / ".claude-plugin" / "plugin.json").write_text("{ not json")
        ok, missing = _check(monkeypatch, root)
        assert not ok, "a malformed plugin.json was reported healthy"
        assert missing, "the failure named nothing"

    def test_an_absent_manifest_still_fails(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path)
        (root / ".claude-plugin" / "plugin.json").unlink()
        ok, _missing = _check(monkeypatch, root)
        assert not ok, "a missing plugin.json was reported healthy"

    def test_an_unreadable_declared_manifest_still_fails(self, tmp_path, monkeypatch) -> None:
        """An explicitly declared path that cannot be read is a real failure; only
        the ABSENCE of a declaration becomes benign."""
        root = _fake_install(
            tmp_path,
            manifest={"name": "writ", "hooks": "./config/other-hooks.json"},
            hooks_rel="config/other-hooks.json",
        )
        (root / "config" / "other-hooks.json").write_text("{ not json")
        ok, _missing = _check(monkeypatch, root)
        assert not ok, "an unreadable declared hooks manifest was reported healthy"

    def test_a_missing_hook_script_still_fails(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path, write_script=False)
        ok, missing = _check(monkeypatch, root)
        assert not ok, "a missing hook script was reported healthy"
        assert any("missing" in entry for entry in missing), missing

    def test_a_non_executable_hook_script_still_fails(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path, script_executable=False)
        ok, missing = _check(monkeypatch, root)
        assert not ok, "a non-executable hook script was reported healthy"
        assert any("executable" in entry for entry in missing), missing


# --------------------------------------------------------------------------- #
# Capability 6: the shape that would have caught this without the mechanism
# --------------------------------------------------------------------------- #

class TestTheReportNamesScriptsNotDirectories:
    """The tell. The detail string named the package root, and no correct code path
    reports a directory as a missing script. This is the assertion that would have
    caught the defect without knowing why it happened.
    """

    def test_no_reported_entry_is_a_directory(self, tmp_path, monkeypatch) -> None:
        root = _fake_install(tmp_path)
        _ok, missing = _check(monkeypatch, root)
        offenders = [
            entry for entry in missing
            if os.path.isdir(entry.split(" (")[0])
        ]
        assert not offenders, (
            f"the check reported a DIRECTORY as a missing script: {offenders}"
        )

    def test_no_reported_entry_is_a_directory_on_the_real_repo(self) -> None:
        from writ.session import doctor

        _ok, missing = doctor._cc_registration_ok()
        offenders = [
            entry for entry in missing
            if os.path.isdir(entry.split(" (")[0])
        ]
        assert not offenders, (
            f"the check reported a DIRECTORY as a missing script: {offenders}"
        )

    def test_the_default_path_is_a_named_constant(self) -> None:
        """The convention it encodes should be visible, not buried in an expression:
        Claude Code's hook discovery is a convention, not a contract, so the day it
        changes the reader needs one place to look."""
        from writ.session import doctor

        _require(doctor, "DEFAULT_HOOKS_MANIFEST")
        assert "hooks.json" in str(doctor.DEFAULT_HOOKS_MANIFEST), (
            doctor.DEFAULT_HOOKS_MANIFEST
        )
