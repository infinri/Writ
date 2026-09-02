"""Contract for the outputStyle managed-settings key (plan.md capabilities 1-13,
plus the safety guard the dispatch brief requires in addition to the capability
list).

Pins the three-state overwrite policy (ABSENT/EQUAL/DIFFERENT, plus the explicit-null
case) that `bin/lib/writ_install.py` gains through a `MANAGED_SETTINGS` declaration
and a shared state reader, the read-back checker `cmd_check_settings` grows to
answer, and the doctor seam `writ.session.doctor._missing_allow_entries` gains
through an optional `target` parameter.

Every test below is RED until that production code exists (ImportError on
`writ_install.MANAGED_SETTINGS`, a TypeError on the extra `target` argument to
`_missing_allow_entries`, or a missing/overwritten key in a written settings file),
with two named exceptions that pin invariants already true today and only go red
under the specific regression named in their own comment: the templates/settings.json
ownership boundary (capability 14, lives in tests/test_settings_template_sync.py, not
here) and the two argparse-required-target guards in
TestNoDefaultEverResolvesToTheRealSettingsFile below.

SAFETY (non-negotiable): every settings target in this module is a tmp_path file,
passed with --target (subprocess calls) or as the explicit `target` argument (the
doctor seam). No test here reads, writes, or otherwise touches the real
~/.claude/settings.json.

Run: .venv/bin/python3 -m pytest tests/test_managed_global_settings.py -q
(imports writ.session.doctor, which lives in the `writ` package; the isolated Neo4j
must be up first even though nothing here touches the graph, because conftest.py
refuses to collect otherwise).
"""
from __future__ import annotations

import argparse
import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL_MODULE = REPO / "bin" / "lib" / "writ_install.py"

OUTPUT_STYLE_KEY = "outputStyle"
SHIPPED_VALUE = "Concise"
REAL_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _run_installer(*argv: str) -> subprocess.CompletedProcess:
    """Run writ_install.py under bare system python3, exactly as every real
    caller does (scripts/patch-global-config.sh, scripts/bootstrap.sh) and as
    tests/test_writ_install.py's run_module() already does."""
    return subprocess.run(
        ["python3", str(INSTALL_MODULE), *argv],
        capture_output=True, text=True, cwd=str(REPO), timeout=60,
    )


def _writ_install_module():
    """Import bin/lib/writ_install.py in-process. Used only where the test needs
    to reach inside the module (the drift proof, the declaration pin) -- every
    behavioral assertion elsewhere in this file runs the module as a real
    subprocess, per the Testing shape section of plan.md. Mirrors
    _installer_entries() in tests/test_hygiene_cycle_d.py."""
    sys.path.insert(0, str(REPO / "bin" / "lib"))
    try:
        import writ_install

        return writ_install
    finally:
        sys.path.pop(0)


def _installer_entries() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(BASE_ALLOW, DENY) read from the module that owns them, never a second,
    driftable copy (HARD CONSTRAINT 1: never pin a count the repo grows)."""
    module = _writ_install_module()
    return module.BASE_ALLOW, module.DENY


_OMIT = object()


def _permissions_complete_doc(output_style: object = _OMIT) -> dict:
    """A settings document holding every shipped permission entry, with
    outputStyle omitted, or set to a caller-chosen value (including None, for the
    explicit-null case)."""
    allow, deny = _installer_entries()
    doc: dict = {"permissions": {"allow": list(allow), "deny": list(deny)}}
    if output_style is not _OMIT:
        doc[OUTPUT_STYLE_KEY] = output_style
    return doc


def _require(module, *names: str) -> None:
    """Fail with a legible skeleton message instead of a raw AttributeError from
    monkeypatch/getattr, matching tests/test_hygiene_cycle_d.py's convention
    (TEC-PROC-RED-VERIFY-001): a RED must read as missing production behavior,
    not as a broken test."""
    missing = [name for name in names if not hasattr(module, name)]
    if missing:
        pytest.fail(f"skeleton: {getattr(module, '__name__', module)} has no "
                    f"{', '.join(missing)} yet")


# --------------------------------------------------------------------------- #
# Capability 1: ABSENT -> written, alongside the permission merge
# --------------------------------------------------------------------------- #


class TestWriterCreatesFromNothing:
    """Capability 1: 'A settings.json created from nothing by `writ_install.py
    settings` carries `outputStyle` set to `Concise` alongside the permission
    entries.'

    Mutation: delete the write branch in cmd_settings (plan.md mutation 1) -> red,
    because neither the key nor the permission entries land in a file that never
    existed.
    """

    def test_fresh_settings_json_carries_outputstyle_and_permission_entries(self, tmp_path):
        target = tmp_path / "settings.json"
        assert not target.exists()
        proc = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        doc = json.loads(target.read_text())
        assert doc.get(OUTPUT_STYLE_KEY) == SHIPPED_VALUE, doc
        allow, deny = _installer_entries()
        assert set(allow) <= set(doc["permissions"]["allow"])
        assert set(deny) <= set(doc["permissions"]["deny"])


# --------------------------------------------------------------------------- #
# Capabilities 2 + 3: PRESENT AND DIFFERENT -> kept, informed, exit 0
# --------------------------------------------------------------------------- #


class TestWriterLeavesADifferentValue:
    """Capabilities 2 and 3: the user's differing value survives the patch, the
    permission merge still runs in the same pass, and the run prints an
    informational line naming both values while exiting 0.
    """

    def _seed_with_a_different_value(self, target: Path) -> None:
        target.write_text(json.dumps({
            "permissions": {"allow": [], "deny": []},
            OUTPUT_STYLE_KEY: "Explanatory",
        }))

    def test_keeps_the_users_value_and_still_merges_permissions(self, tmp_path):
        """Capability 2. Mutation: assign doc[key] = shipped unconditionally
        (plan.md mutation 2) -> red, the user's 'Explanatory' becomes 'Concise'."""
        target = tmp_path / "settings.json"
        self._seed_with_a_different_value(target)
        proc = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        doc = json.loads(target.read_text())
        assert doc[OUTPUT_STYLE_KEY] == "Explanatory", (
            "a foreign value was overwritten; Writ must never clobber a user's "
            f"/config choice: {doc!r}"
        )
        allow, _deny = _installer_entries()
        assert set(allow) <= set(doc["permissions"]["allow"]), (
            "the permission merge did not run in the same pass as the settings-key check"
        )

    def test_prints_an_informational_line_naming_both_values_and_exits_zero(self, tmp_path):
        """Capability 3. Mutation: make the DIFFERENT state silent (plan.md
        mutation 3) -> red, neither value appears in the output."""
        target = tmp_path / "settings.json"
        self._seed_with_a_different_value(target)
        proc = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        combined = proc.stdout + proc.stderr
        assert "Explanatory" in combined, combined
        assert SHIPPED_VALUE in combined, combined


# --------------------------------------------------------------------------- #
# Capability 4: PRESENT AND EQUAL -> silent, byte-identical on a second run
# --------------------------------------------------------------------------- #


class TestWriterIsIdempotentWhenEqual:
    """Capability 4: 'A settings.json already carrying `Concise` is byte-identical
    after a second patch run and prints no "leaving your choice untouched" line.'

    Mutation: treat PRESENT-AND-EQUAL as DIFFERENT (plan.md mutation 4) -> red, the
    second run either rewrites the file or prints the DIFFERENT-state message for
    a value that was never different.
    """

    def test_byte_identical_second_run_and_no_divergence_message(self, tmp_path):
        target = tmp_path / "settings.json"
        first = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert first.returncode == 0, first.stdout + first.stderr
        content_after_first = target.read_text()
        assert json.loads(content_after_first)[OUTPUT_STYLE_KEY] == SHIPPED_VALUE

        second = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert second.returncode == 0, second.stdout + second.stderr
        assert target.read_text() == content_after_first, (
            "a second run against an already-Concise file rewrote it"
        )
        assert "leaving your choice untouched" not in (second.stdout + second.stderr).lower(), (
            f"the DIFFERENT-state message printed for a value that was never "
            f"different: {second.stdout + second.stderr!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 5: explicit null is the user's value, kept, key-membership not
# truthiness
# --------------------------------------------------------------------------- #


class TestWriterKeepsAnExplicitNull:
    """Capability 5: presence is tested with `in`, not `.get()` truthiness, so an
    explicit `null` is the user's value and is left alone.

    Mutation: use doc.get(key) truthiness instead of `key in doc` (plan.md
    mutation 5) -> red, None is falsy so the writer overwrites it with 'Concise'.
    """

    def test_null_outputstyle_is_kept_and_reported_as_the_users_choice(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({
            "permissions": {"allow": [], "deny": []},
            OUTPUT_STYLE_KEY: None,
        }))
        proc = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        doc = json.loads(target.read_text())
        assert OUTPUT_STYLE_KEY in doc, "the key was dropped entirely, not merely left null"
        assert doc[OUTPUT_STYLE_KEY] is None, (
            f"an explicit null was overwritten: {doc[OUTPUT_STYLE_KEY]!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 6: --dry-run never claims the write, but the diff shows it
# --------------------------------------------------------------------------- #


class TestDryRunDoesNotClaimTheWrite:
    """Capability 6: 'settings --dry-run against a file with no outputStyle writes
    nothing and prints no line claiming the key was set, while the diff it prints
    shows the addition.'

    Mutation: move the 'Set outputStyle' print above the --dry-run branch (plan.md
    mutation 6) -> red, the claim-of-action line appears even though nothing was
    written.
    """

    def test_dry_run_writes_nothing_and_the_diff_shows_the_addition(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"permissions": {"allow": [], "deny": []}}))
        before = target.read_text()
        proc = _run_installer(
            "settings", "--target", str(target), "--skill-dir", str(REPO), "--dry-run",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert target.read_text() == before, "dry-run must write nothing"
        combined = proc.stdout + proc.stderr
        assert "Set outputStyle" not in combined, (
            f"dry-run must not claim the key was set: {combined!r}"
        )
        added_lines = [
            line for line in combined.splitlines()
            if line.startswith("+") and OUTPUT_STYLE_KEY in line and SHIPPED_VALUE in line
        ]
        assert added_lines, f"the diff must show outputStyle being added: {combined!r}"


# --------------------------------------------------------------------------- #
# Capabilities 7, 8, 9: the read-back checker's three states
# --------------------------------------------------------------------------- #


class TestCheckSettingsAbsentState:
    """Capability 7: 'check-settings against a file holding every permission
    entry but no outputStyle exits non-zero and prints the missing key and its
    value on a line carrying no [check-settings] prefix.'

    Mutation: leave cmd_check_settings checking permissions only (plan.md
    mutation 7) -> red, returncode stays 0 and no line names the key.
    """

    def test_reports_the_missing_key_and_value_without_the_prefix(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps(_permissions_complete_doc()))
        proc = _run_installer("check-settings", "--target", str(target))
        assert proc.returncode != 0, "a file missing outputStyle reported as complete"
        lines = (proc.stdout + proc.stderr).splitlines()
        finding_lines = [
            line for line in lines if OUTPUT_STYLE_KEY in line and SHIPPED_VALUE in line
        ]
        assert finding_lines, f"no line named the missing key and its value: {lines!r}"
        assert not any(line.startswith("[check-settings]") for line in finding_lines), (
            f"the missing-key finding is prefixed, so the doctor's parser would "
            f"drop it as a non-finding: {finding_lines!r}"
        )


class TestCheckSettingsAfterAFreshPatch:
    """Capability 8: 'check-settings against a file the patcher just wrote exits
    0.'

    Mutation: compare the checker against the wrong value, or drop the value from
    the comparison (plan.md mutation 8) -> red, a correctly patched file reads as
    incomplete.
    """

    def test_exits_zero_against_a_file_the_patcher_just_wrote(self, tmp_path):
        target = tmp_path / "settings.json"
        written = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert written.returncode == 0, written.stdout + written.stderr
        proc = _run_installer("check-settings", "--target", str(target))
        assert proc.returncode == 0, proc.stdout + proc.stderr


class TestCheckSettingsDifferentState:
    """Capability 9: 'check-settings against a file whose outputStyle differs
    from the shipped value exits 0 and prints the divergence on a
    [check-settings]-prefixed line, so the doctor does not count it as a
    finding.'

    Mutation: report the DIFFERENT state as missing, or print the finding without
    the [check-settings] prefix (plan.md mutation 9) -> red, either the exit code
    flips non-zero or the doctor's parser would treat the line as a finding.
    """

    def test_exits_zero_and_prefixes_the_divergence(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps(_permissions_complete_doc(output_style="Explanatory")))
        proc = _run_installer("check-settings", "--target", str(target))
        assert proc.returncode == 0, (
            f"a machine where the user chose a different outputStyle reported "
            f"non-zero, which would make the doctor permanently red: "
            f"{proc.stdout!r} {proc.stderr!r}"
        )
        lines = (proc.stdout + proc.stderr).splitlines()
        divergence_lines = [
            line for line in lines if OUTPUT_STYLE_KEY in line and "Explanatory" in line
        ]
        assert divergence_lines, f"no line named the diverging value: {lines!r}"
        assert all(line.startswith("[check-settings]") for line in divergence_lines), (
            f"a DIFFERENT-state line without the prefix would be parsed by the "
            f"doctor as a finding: {divergence_lines!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 10: the drift proof (the most important test in this cycle)
# --------------------------------------------------------------------------- #


class TestManagedSettingsDriftIsDetectedByConstruction:
    """Capability 10: 'Injecting a synthetic entry into MANAGED_SETTINGS makes the
    writer write that key and the checker report it missing, with no edit to
    either function.'

    A hardcoded `doc["outputStyle"] = "Concise"` inside cmd_settings, or a
    hardcoded `"outputStyle" in doc` / `doc.get("outputStyle")` inside
    cmd_check_settings, would pass every other test in this file -- they all use
    the one real, shipped entry. This injects a SYNTHETIC entry the source does
    not contain and requires both functions to still act on it correctly, which
    is only possible if they iterate MANAGED_SETTINGS rather than naming a key.

    Mutation: hardcode the key in cmd_settings or cmd_check_settings instead of
    iterating the declaration (plan.md mutation 10) -> both tests below go red
    while every count-based or literal-value check elsewhere in this file stays
    green.

    Runs in-process (imports bin/lib/writ_install.py directly and monkeypatches
    MANAGED_SETTINGS), per plan.md's Testing shape section: reading the
    declaration at call time is the property under test, which a subprocess call
    cannot inject into.
    """

    SYNTHETIC_ENTRY = ("__writDriftProbe__", "synthetic-probe-value")

    def test_writer_writes_an_entry_it_never_named(self, tmp_path, monkeypatch):
        module = _writ_install_module()
        _require(module, "MANAGED_SETTINGS", "cmd_settings", "EXIT_OK")
        monkeypatch.setattr(
            module, "MANAGED_SETTINGS", module.MANAGED_SETTINGS + (self.SYNTHETIC_ENTRY,),
        )
        target = tmp_path / "settings.json"
        args = argparse.Namespace(target=str(target), skill_dir=str(REPO), dry_run=False)
        rc = module.cmd_settings(args)
        assert rc == module.EXIT_OK, rc
        doc = json.loads(target.read_text())
        key, value = self.SYNTHETIC_ENTRY
        assert doc.get(key) == value, (
            f"the writer did not write the synthetic MANAGED_SETTINGS entry "
            f"{self.SYNTHETIC_ENTRY!r}; it is naming a key literally instead of "
            f"iterating the declaration: {doc!r}"
        )

    def test_checker_reports_it_missing(self, tmp_path, monkeypatch, capsys):
        module = _writ_install_module()
        _require(module, "MANAGED_SETTINGS", "cmd_check_settings", "EXIT_OK")
        monkeypatch.setattr(
            module, "MANAGED_SETTINGS", module.MANAGED_SETTINGS + (self.SYNTHETIC_ENTRY,),
        )
        target = tmp_path / "settings.json"
        target.write_text(json.dumps(_permissions_complete_doc(output_style=SHIPPED_VALUE)))
        args = argparse.Namespace(target=str(target))
        rc = module.cmd_check_settings(args)
        out = capsys.readouterr()
        combined = out.out + out.err
        key, value = self.SYNTHETIC_ENTRY
        assert rc != module.EXIT_OK, (
            "the checker reported success against a file missing the synthetic entry"
        )
        assert key in combined and value in combined, (
            f"the checker did not report the synthetic entry {self.SYNTHETIC_ENTRY!r} "
            f"as missing; it is checking a hardcoded key instead of iterating the "
            f"declaration: {combined!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 11: the pinned declaration
# --------------------------------------------------------------------------- #


class TestManagedSettingsDeclaration:
    """Capability 11: 'MANAGED_SETTINGS contains exactly the observed pair
    ("outputStyle", "Concise").'

    This is `in`, not `==`, deliberately: MANAGED_SETTINGS is expected to grow
    (HARD CONSTRAINT 1), so this pins the one entry this cycle ships, not the
    tuple's length.

    Mutation: misspell the declaration ('outputStyl') or the value ('concise')
    (plan.md mutation 11) -> red.
    """

    def test_contains_the_observed_pair(self):
        module = _writ_install_module()
        _require(module, "MANAGED_SETTINGS")
        assert ("outputStyle", "Concise") in module.MANAGED_SETTINGS, (
            f"MANAGED_SETTINGS does not contain the observed pair: "
            f"{module.MANAGED_SETTINGS!r}"
        )


# --------------------------------------------------------------------------- #
# Capability 12: the doctor's target seam
# --------------------------------------------------------------------------- #


class TestMissingAllowEntriesAcceptsATargetSeam:
    """Capability 12: 'doctor._missing_allow_entries(target) returns the missing
    settings key for a permissions-complete file that lacks it, and an empty list
    after the patcher has run against that same file.'

    Runs the real check-settings subprocess against a real temp file through the
    new seam (ENF-SYS-005): the claim under test is the whole doctor diagnosis
    path, not a mock's own return value.

    Mutation: never add the `target` parameter to _missing_allow_entries -> both
    tests below raise TypeError on the extra positional argument, which is a
    missing-production-behavior RED, not a broken test.
    """

    def test_reports_the_missing_settings_key_for_a_permissions_complete_file(self, tmp_path):
        from writ.session import doctor

        _require(doctor, "_missing_allow_entries")
        target = tmp_path / "settings.json"
        target.write_text(json.dumps(_permissions_complete_doc()))
        missing = doctor._missing_allow_entries(target)
        assert any(OUTPUT_STYLE_KEY in entry for entry in missing), (
            f"the settings key was not reported missing: {missing!r}"
        )

    def test_empty_after_the_patcher_has_run_against_that_file(self, tmp_path):
        from writ.session import doctor

        _require(doctor, "_missing_allow_entries")
        target = tmp_path / "settings.json"
        target.write_text(json.dumps(_permissions_complete_doc()))
        assert doctor._missing_allow_entries(target) != []

        seeded = _run_installer("settings", "--target", str(target), "--skill-dir", str(REPO))
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr
        assert doctor._missing_allow_entries(target) == []


# --------------------------------------------------------------------------- #
# Capability 13: the doctor's parse of a settings-key finding
# --------------------------------------------------------------------------- #


class TestCheckPermissionsAllowlistNamesASettingsKey:
    """Capability 13: 'check_permissions_allowlist reports a non-ok, fixable
    result when a settings key is the only missing item, with the count in its
    detail.'

    Mirrors the monkeypatch-with-a-zero-argument-lambda pattern already used by
    tests/test_hygiene_cycle_d.py::TestPermissionsAllowlistIsDiagnosable and
    tests/test_doctor.py, which the Verification section requires stay green
    against the new optional parameter.

    Mutation: check_permissions_allowlist counts only permission entries and
    drops the settings-key case (plan.md mutation 12) -> red, a lone settings-key
    miss no longer reports non-ok/fixable with the count.

    The status/fixable/count behavior alone is already satisfied by the existing
    generic implementation (it does not care what the missing strings mean), so
    the assertion that actually pins plan.md's described change -- the detail
    "names both kinds of item" rather than calling every miss a permission entry
    -- is the one that must fail against the CURRENT wording
    ("N Writ permission entr(ies) missing..."). Without it this test would be
    green before and after the doctor.py edit, proving nothing about the reword.
    """

    def test_reports_non_ok_and_fixable_when_a_settings_key_is_the_only_miss(self, monkeypatch):
        from writ.session import doctor

        _require(doctor, "_missing_allow_entries", "check_permissions_allowlist")
        monkeypatch.setattr(
            doctor, "_missing_allow_entries",
            lambda: [f'{OUTPUT_STYLE_KEY}: "{SHIPPED_VALUE}"'],
        )
        result = doctor.check_permissions_allowlist(doctor.DoctorOptions())
        assert result.status != doctor.STATUS_OK
        assert "1" in result.detail, result.detail
        assert result.fixable is True
        assert result.fix is not None
        detail_lower = result.detail.lower()
        # Deliberately not "settings" alone: the CURRENT wording already contains
        # that substring inside the path "~/.claude/settings.json", so a bare
        # substring check would pass against the unmodified code and prove
        # nothing about the reword.
        assert "settings key" in detail_lower or "settings entr" in detail_lower, (
            f"the detail still describes every miss as a permission entry; "
            f"plan.md requires it to name both kinds of item once a settings "
            f"key can be one of them: {result.detail!r}"
        )


# --------------------------------------------------------------------------- #
# THE ONE HARD SAFETY RULE: no path in this feature may default to the real file
# --------------------------------------------------------------------------- #


class TestNoDefaultEverResolvesToTheRealSettingsFile:
    """Explicit guard, required by the dispatch brief in addition to the
    capability list above. Neither installer subcommand this feature touches may
    gain an implicit default that lands on ~/.claude/settings.json; every caller
    (test or real) must say so explicitly with --target.

    The doctor's read-only seam is the one function allowed a default, and its
    default IS the real per-user file by design (a `writ doctor` run with no
    argument has to diagnose the actual machine, and it never writes). So the
    boundary this class holds is: the writer never defaults there, and the one
    function that legitimately does default there is read-only, with its default
    pinned so a silent change is caught immediately rather than discovered by a
    test that forgot to pass a target.
    """

    def test_settings_subcommand_has_no_default_target(self):
        """Mutation: give --target a default of str(REAL_SETTINGS_FILE) -> this
        goes green (returncode 0, file written under $HOME) instead of an
        argparse usage error, silently making every future bare `settings`
        invocation write to the real file."""
        proc = _run_installer("settings")
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, (
            f"the settings subcommand ran without --target: {combined!r}"
        )
        assert "required" in combined.lower(), combined
        # Deliberately no stat/read/exists() check against REAL_SETTINGS_FILE here
        # or anywhere else in this module: the safety rule this class guards is
        # "no test touches the real file", so the test itself must not touch it
        # either, not even to confirm it was left alone. The non-zero exit above
        # is the whole proof: argparse refuses before any write is attempted.

    def test_check_settings_subcommand_has_no_default_target(self):
        """Same guard, for the read-back checker."""
        proc = _run_installer("check-settings")
        combined = proc.stdout + proc.stderr
        assert proc.returncode != 0, (
            f"the check-settings subcommand ran without --target: {combined!r}"
        )
        assert "required" in combined.lower(), combined

    def test_doctor_seams_default_is_pinned_to_the_real_path(self):
        """The one intentional default in this feature, and it is read-only.

        Mutation: change the default to a relative path, an empty string, or drop
        the default entirely (so a bare call raises instead) -> this pin goes
        red, which is the point: nobody should be able to silently repoint (or
        silently un-default) the one function in this feature allowed to name the
        real file.
        """
        from writ.session import doctor

        _require(doctor, "_missing_allow_entries")
        sig = inspect.signature(doctor._missing_allow_entries)
        params = list(sig.parameters.values())
        assert params, "skeleton: _missing_allow_entries takes no target parameter yet"
        assert params[0].default == REAL_SETTINGS_FILE, (
            f"default target is {params[0].default!r}, expected {REAL_SETTINGS_FILE!r}"
        )
