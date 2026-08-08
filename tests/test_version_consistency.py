"""Cross-cutting: version string consistency across every manifest that declares one.

pyproject.toml, .claude-plugin/plugin.json and the .claude-plugin/marketplace.json entry
must all declare the same version. This is the ONE place the expected version is written
down; a release bumps `EXPECTED_VERSION` here and nowhere else in the test suite.

marketplace.json came back in v1.5.1: it had been removed when the project moved to private
distribution, and its absence meant `claude plugin marketplace add` failed, so nobody could
install Writ as a plugin. `claude plugin tag` validates that the plugin manifest and the
enclosing marketplace entry agree, which is why the duplication is enforced rather than
merely tolerated (DRY-CONFIG-001).

SKILL.md was removed in v1.5.0.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

SKILL_DIR = (Path(__file__).resolve().parent.parent)
EXPECTED_VERSION = "1.7.0"


@pytest.fixture(scope="module")
def pyproject() -> dict:
    with (SKILL_DIR / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


@pytest.fixture(scope="module")
def plugin_json() -> dict:
    with (SKILL_DIR / ".claude-plugin" / "plugin.json").open() as f:
        return json.load(f)


@pytest.fixture(scope="module")
def marketplace_json() -> dict:
    with (SKILL_DIR / ".claude-plugin" / "marketplace.json").open() as f:
        return json.load(f)


class TestPyprojectVersion:
    def test_pyproject_version_matches_expected(self, pyproject: dict) -> None:
        version = pyproject.get("project", {}).get("version")
        assert version == EXPECTED_VERSION, (
            f"pyproject.toml version must be '{EXPECTED_VERSION}'; got {version!r}"
        )


class TestPluginJsonVersion:
    def test_plugin_json_version_matches_expected(self, plugin_json: dict) -> None:
        version = plugin_json.get("version")
        assert version == EXPECTED_VERSION, (
            f"plugin.json version must be '{EXPECTED_VERSION}'; got {version!r}"
        )


class TestMarketplaceJsonVersion:
    def test_marketplace_entry_version_matches_expected(
        self, marketplace_json: dict
    ) -> None:
        version = marketplace_json["plugins"][0].get("version")
        assert version == EXPECTED_VERSION, (
            f"marketplace.json plugin entry version must be '{EXPECTED_VERSION}'; "
            f"got {version!r}"
        )


class TestVersionConsistencyAcrossFiles:
    def test_every_manifest_agrees(
        self,
        pyproject: dict,
        plugin_json: dict,
        marketplace_json: dict,
    ) -> None:
        versions = {
            "pyproject.toml": pyproject.get("project", {}).get("version"),
            "plugin.json:version": plugin_json.get("version"),
            "marketplace.json:plugins[0].version": (
                marketplace_json["plugins"][0].get("version")
            ),
            "marketplace.json:metadata.version": (
                marketplace_json.get("metadata", {}).get("version")
            ),
        }
        wrong = {k: v for k, v in versions.items() if v != EXPECTED_VERSION}
        assert not wrong, (
            f"The following manifests do not declare version '{EXPECTED_VERSION}': "
            + ", ".join(f"{k}={v!r}" for k, v in wrong.items())
        )
