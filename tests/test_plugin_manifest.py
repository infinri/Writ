"""Writ as a real Claude Code plugin: the install path has never worked end to end.

Three independent breakages, all verified live against Claude Code 2.1.220:

  1. No `.claude-plugin/marketplace.json`, so README.md's very first install command
     (`claude plugin marketplace add infinri/Writ`) fails outright. Nothing downstream of
     it has ever been reachable for a real user.
  2. `plugin.json` declared `"hooks": "./hooks/hooks.json"`, which IS the path Claude Code
     auto-discovers. The declaration collides with auto-discovery and ALL 12 hooks fail to
     load, so a marketplace-installed Writ has no gate, no rule injection, no enforcement.
  3. `claude plugin path` is not a subcommand. README and bootstrap-plugin.sh wrap it in
     `$(...)`, so a copy-paste expands to the empty string.

WHY THESE TESTS DO NOT CALL `claude plugin validate` AND STOP THERE: validate checks
manifest SHAPE, not whether declared components load. It prints "Validation passed" on the
manifest that reports `Agents (0)` for 5 declared, on-disk agent files. Passing validation
is not evidence of a working install (ENF-OPS-001), so the static tests below assert the
specific invariants directly, and the `integration` tests install for real and read the
CLI's own error list and component counts.

Split of duties:
  static tests       the regression guards; they fail if a defect is reintroduced
  integration tests  the acceptance checks; they need the `claude` CLI and do a real
                     install into a throwaway HOME (nothing on the real machine is touched)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO / ".claude-plugin" / "plugin.json"
MARKETPLACE_MANIFEST = REPO / ".claude-plugin" / "marketplace.json"
AUTO_DISCOVERED_HOOKS = REPO / "hooks" / "hooks.json"

_HAVE_CLAUDE = shutil.which("claude") is not None
_needs_claude = pytest.mark.skipif(_HAVE_CLAUDE is False, reason="claude CLI not on PATH")


def _plugin_manifest() -> dict:
    return json.loads(PLUGIN_MANIFEST.read_text())


def _marketplace_manifest() -> dict:
    return json.loads(MARKETPLACE_MANIFEST.read_text())


class TestMarketplaceManifestExists:
    """Capability: `claude plugin marketplace add <repo>` succeeds against the repo root."""

    def test_marketplace_manifest_exists(self):
        assert MARKETPLACE_MANIFEST.is_file(), (
            f"{MARKETPLACE_MANIFEST} is missing, which is why "
            "`claude plugin marketplace add infinri/Writ` fails with "
            "'Marketplace file not found'"
        )

    def test_it_is_valid_json(self):
        assert isinstance(_marketplace_manifest(), dict)

    def test_it_names_the_marketplace_and_an_owner(self):
        """`install writ@writ` in the README requires the marketplace to be named `writ`."""
        mkt = _marketplace_manifest()
        assert mkt.get("name") == "writ", (
            "README documents `claude plugin install writ@writ`, so the marketplace name "
            f"must be 'writ', got {mkt.get('name')!r}"
        )
        assert mkt.get("owner", {}).get("name"), "marketplace requires an owner.name"

    def test_it_declares_exactly_one_plugin_named_writ_sourced_at_the_repo_root(self):
        plugins = _marketplace_manifest().get("plugins")
        assert isinstance(plugins, list) and len(plugins) == 1, (
            f"expected exactly one plugin entry, got {plugins!r}"
        )
        entry = plugins[0]
        assert entry.get("name") == "writ"
        assert entry.get("source") == "./", (
            "the repo root IS the plugin, so source must be './'; "
            f"got {entry.get('source')!r}"
        )


class TestHooksAreNotDoubleDeclared:
    """Capability: hooks load on a marketplace install.

    The failure this guards is not hypothetical. With the `hooks` key present, a real
    install reports:
      Hook load failed: Duplicate hooks file detected: ./hooks/hooks.json resolves to
      already-loaded file .../hooks/hooks.json. The standard hooks/hooks.json is loaded
      automatically, so manifest.hooks should only reference additional hook files.
    """

    def test_the_auto_discovered_hooks_file_still_exists(self):
        """Deleting the manifest key is only safe because auto-discovery finds this."""
        assert AUTO_DISCOVERED_HOOKS.is_file()

    def test_it_still_registers_all_twelve_events(self):
        events = json.loads(AUTO_DISCOVERED_HOOKS.read_text()).get("hooks", {})
        assert len(events) == 12, (
            f"expected 12 registered hook events, got {len(events)}: {sorted(events)}"
        )

    def test_no_declared_hooks_path_resolves_to_the_auto_discovered_file(self):
        """The general invariant, not just 'the key is absent'.

        `"hooks": "hooks/hooks.json"`, `"./hooks/hooks.json"` and
        `["./hooks/hooks.json"]` are all the same collision, so the check resolves
        whatever is declared and compares real paths.
        """
        declared = _plugin_manifest().get("hooks")
        if declared is None:
            return
        paths = [declared] if isinstance(declared, str) else list(declared)
        auto = AUTO_DISCOVERED_HOOKS.resolve()
        collisions = [p for p in paths if (REPO / p).resolve() == auto]
        assert collisions == [], (
            f"plugin.json declares {collisions} which is the auto-discovered "
            "hooks/hooks.json; the declaration collides and ALL hooks fail to load. "
            "manifest.hooks is for ADDITIONAL hook files only."
        )

    def test_the_manifest_declares_no_hooks_key_at_all(self):
        """Writ has no additional hook files, so the key has no legitimate use here."""
        assert "hooks" not in _plugin_manifest(), (
            "hooks/hooks.json is auto-discovered; declaring it is the defect"
        )


class TestManifestAndMarketplaceCannotDrift:
    def test_the_versions_agree(self):
        """`claude plugin tag` validates that the two manifests agree (DRY-CONFIG-001).

        The version has to exist in two files, so this test is the mitigation for the
        duplication it cannot remove.
        """
        plugin_version = _plugin_manifest().get("version")
        entry_version = _marketplace_manifest()["plugins"][0].get("version")
        assert plugin_version == entry_version, (
            f"plugin.json says {plugin_version!r} and the marketplace entry says "
            f"{entry_version!r}; `claude plugin tag` rejects a mismatch"
        )

    def test_every_declared_component_path_exists(self):
        """A declared path that does not exist is a silent no-op component."""
        manifest = _plugin_manifest()
        missing = []
        for key in ("commands", "agents", "skills", "hooks", "mcpServers"):
            declared = manifest.get(key)
            if declared is None:
                continue
            paths = [declared] if isinstance(declared, str) else list(declared)
            for rel in paths:
                if not isinstance(rel, str):
                    continue
                if not (REPO / rel).exists():
                    missing.append(f"{key}: {rel}")
        assert missing == [], f"plugin.json declares paths that do not exist: {missing}"


class TestTheDeadSubcommandIsGoneFromTheCLI:
    """Capability: the docs stop telling the user to run a subcommand that does not exist.

    `claude plugin path` returns `error: unknown command 'path'` and is absent from
    `claude plugin --help`. Wrapped in `$(...)` as the README does, it expands to the
    empty string, so `bash $(claude plugin path writ)/scripts/bootstrap-plugin.sh` runs
    `bash /scripts/bootstrap-plugin.sh`.

    There is deliberately NO test asserting what README.md or CHANGELOG.md contain. Per a
    standing project directive, a doc's wording is not a code defect and a suite that goes
    red on a reworded paragraph stops meaning "something is broken". The docs are corrected
    directly instead. What IS pinned here is the CLI fact that makes the correction
    necessary, and the replacement command is exercised for real in
    TestRealInstallLoadsEverything.
    """

    @_needs_claude
    def test_the_cli_really_has_no_path_subcommand(self):
        r = subprocess.run(
            ["claude", "plugin", "path", "writ"], capture_output=True, text=True
        )
        assert r.returncode != 0, (
            "`claude plugin path` now works; the docs were corrected on the premise that "
            "it does not exist, so revisit them"
        )
        assert "unknown command" in (r.stderr + r.stdout).lower()


@pytest.mark.integration
class TestValidatePasses:
    """Necessary but NOT sufficient, per this module's docstring. Asserted anyway."""

    @_needs_claude
    def test_the_plugin_manifest_validates_strictly(self):
        """Targets plugin.json by path, deliberately.

        `claude plugin validate <dir>` resolves to the MARKETPLACE manifest once
        marketplace.json exists alongside plugin.json, so passing the directory would
        silently stop checking the plugin manifest and this test would pass for the
        wrong reason.
        """
        r = subprocess.run(
            ["claude", "plugin", "validate", str(PLUGIN_MANIFEST), "--strict"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"validate --strict failed: {r.stdout}{r.stderr}"
        assert "plugin manifest" in r.stdout.lower(), (
            f"expected to validate the plugin manifest, got: {r.stdout}"
        )

    @_needs_claude
    def test_the_marketplace_manifest_validates(self):
        r = subprocess.run(
            ["claude", "plugin", "validate", str(MARKETPLACE_MANIFEST)],
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"marketplace validate failed: {r.stdout}{r.stderr}"


@pytest.mark.integration
class TestRealInstallLoadsEverything:
    """The acceptance check: install for real and read what the CLI reports.

    This is the only method that catches the defects in this cycle. Reading the manifest
    cannot: the manifest validates clean while components silently fail to load.

    Runs against a throwaway HOME so the developer's real plugin config is never touched.
    """

    @pytest.fixture(scope="class")
    def installed(self, tmp_path_factory):
        if not _HAVE_CLAUDE:
            pytest.skip("claude CLI not on PATH")
        root = tmp_path_factory.mktemp("plugin-install")
        home, mkt = root / "home", root / "mkt"
        home.mkdir()
        shutil.copytree(
            REPO, mkt,
            ignore=shutil.ignore_patterns(".git", ".venv", "var", "__pycache__", "node_modules"),
        )
        env = {**os.environ, "HOME": str(home)}

        add = subprocess.run(
            ["claude", "plugin", "marketplace", "add", str(mkt)],
            capture_output=True, text=True, env=env,
        )
        assert add.returncode == 0, (
            f"marketplace add failed, so no user can install Writ: {add.stdout}{add.stderr}"
        )
        inst = subprocess.run(
            ["claude", "plugin", "install", "writ@writ"],
            capture_output=True, text=True, env=env,
        )
        assert inst.returncode == 0, f"install failed: {inst.stdout}{inst.stderr}"

        listing = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            capture_output=True, text=True, env=env,
        )
        details = subprocess.run(
            ["claude", "plugin", "details", "writ"],
            capture_output=True, text=True, env=env,
        )
        return {
            "listing": json.loads(listing.stdout),
            "details": details.stdout,
            "env": env,
        }

    def test_the_install_reports_no_errors(self, installed):
        """The duplicate-hooks failure surfaces here and nowhere else."""
        errors = []
        for entry in installed["listing"]:
            errors.extend(entry.get("errors") or [])
        assert errors == [], f"a marketplace install reported load errors: {errors}"

    def test_all_twelve_hooks_load(self, installed):
        """Zero hooks means no gate, no injection, no enforcement."""
        m = re.search(r"Hooks \((\d+)\)", installed["details"])
        assert m, f"no Hooks count in plugin details:\n{installed['details']}"
        assert int(m.group(1)) == 12, (
            f"expected 12 hooks on a marketplace install, got {m.group(1)}"
        )

    def test_the_documented_install_path_command_prints_the_install_dir(self, installed):
        """Capability: the replacement for `claude plugin path`, run exactly as documented."""
        cmd = (
            "claude plugin list --json | python3 -c "
            "\"import json,sys; print(next(p['installPath'] for p in json.load(sys.stdin) "
            "if p['id'].split('@')[0] == 'writ'))\""
        )
        r = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, env=installed["env"]
        )
        assert r.returncode == 0, f"documented command failed: {r.stderr}"
        resolved = r.stdout.strip()
        assert resolved, "the documented command printed nothing"
        assert Path(resolved).is_dir(), f"not a directory: {resolved!r}"
        assert (Path(resolved) / "hooks" / "hooks.json").is_file(), (
            f"{resolved} does not look like a Writ install"
        )
