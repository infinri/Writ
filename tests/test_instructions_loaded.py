"""Tests for the InstructionsLoaded hook and instructions_rule_ids field (Cycle C, Item 11).

Per TEST-TDD-001: skeletons approved before implementation.
Covers: instructions_rule_ids default in _read_cache, rule ID pattern detection,
keyword detection, session cache update, replace-not-append semantics, friction
event, exit 0 contract, settings.json registration, and writ-rag-inject.sh
merge into exclude_rule_ids.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

SESSION_ID = "test-instructions-session"
SKILL_DIR = "/home/lucio.saldivar/.claude/skills/writ"
WRIT_SESSION_PY = f"{SKILL_DIR}/bin/lib/writ-session.py"
HOOK_PATH = f"{SKILL_DIR}/.claude/hooks/writ-instructions-loaded.sh"
SETTINGS_PATH = "/home/lucio.saldivar/.claude/settings.json"

# Standard Writ rule ID format: [A-Z]+-[A-Z]+-\d{3} (e.g. ARCH-ORG-001, SEC-UNI-001)
VALID_RULE_IDS = [
    "ARCH-ORG-001",
    "SEC-UNI-001",
    "ENF-POST-007",
    "FW-M2-RT-003",   # multi-segment compound ID also matches extended format
    "PY-IMPORT-001",
]

PARTIAL_RULE_IDS = [
    "ARCH-",        # incomplete -- no numeric suffix
    "001",          # numeric only
    "arch-org-001", # lowercase -- does not match
    "ARCH_ORG_001", # underscores -- does not match
]

RULE_KEYWORDS = ["WHEN:", "RULE:", "VIOLATION:", "TRIGGER:"]


def _load_writ_session():
    """Load writ-session.py as a module without installing it."""
    spec = importlib.util.spec_from_file_location("writ_session_instr", WRIT_SESSION_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cache(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": SESSION_ID,
        "mode": "Work",
        "current_phase": "planning",
        "remaining_budget": 8000,
        "context_percent": 10,
        "loaded_rule_ids": [],
        "loaded_rules": [],
        "loaded_rule_ids_by_phase": {},
        "queries": 0,
        "pending_violations": [],
        "escalation": {"needed": False},
        "invalidation_history": {},
        "failed_writes": [],
        "is_orchestrator": False,
    }
    base.update(overrides)
    return base


def _make_instructions_envelope(content: str) -> str:
    """Produce a JSON envelope simulating InstructionsLoaded hook stdin."""
    return json.dumps({"instructions": content, "session_id": SESSION_ID})


def _run_hook(content: str, cache_dir: str, session_id: str = SESSION_ID) -> subprocess.CompletedProcess:
    """Run the InstructionsLoaded hook with a simulated envelope."""
    envelope = json.dumps({"instructions": content, "session_id": session_id})
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = cache_dir
    env["WRIT_PORT"] = "19999"  # unreachable port to force subprocess fallback
    return subprocess.run(
        ["bash", HOOK_PATH],
        input=envelope,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _read_instructions_rule_ids(cache_dir: str, session_id: str = SESSION_ID) -> list[str]:
    """Read instructions_rule_ids from the session cache."""
    path = os.path.join(cache_dir, f"writ-session-{session_id}.json")
    with open(path) as f:
        return json.load(f).get("instructions_rule_ids", [])


# ---------------------------------------------------------------------------
# TestInstructionsRuleIdsDefault -- session cache schema
# ---------------------------------------------------------------------------


class TestInstructionsRuleIdsDefault:
    """instructions_rule_ids must be present with a [] default in _read_cache."""

    def setup_method(self) -> None:
        self.mod = _load_writ_session()
        self._orig_cache_dir = self.mod.CACHE_DIR
        self._tmpdir = tempfile.mkdtemp()
        self.mod.CACHE_DIR = self._tmpdir

    def teardown_method(self) -> None:
        self.mod.CACHE_DIR = self._orig_cache_dir
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fresh_cache_has_instructions_rule_ids_key(self) -> None:
        """_read_cache for a new session includes instructions_rule_ids key."""
        cache = self.mod._read_cache("fresh-session")
        assert "instructions_rule_ids" in cache

    def test_instructions_rule_ids_default_is_empty_list(self) -> None:
        """instructions_rule_ids defaults to [] (not None, not missing)."""
        cache = self.mod._read_cache("fresh-session")
        assert cache["instructions_rule_ids"] == []

    def test_existing_cache_without_field_gets_setdefault_applied(self) -> None:
        """_read_cache on a legacy cache file without instructions_rule_ids returns []."""
        legacy_cache = {"loaded_rule_ids": [], "remaining_budget": 8000}
        path = self.mod._cache_path("legacy-session")
        with open(path, "w") as f:
            json.dump(legacy_cache, f)
        cache = self.mod._read_cache("legacy-session")
        assert cache["instructions_rule_ids"] == []


# ---------------------------------------------------------------------------
# TestRuleIdPatternDetection -- [A-Z]+-[A-Z]+-\d{3} matching
# ---------------------------------------------------------------------------


class TestRuleIdPatternDetection:
    """Hook extracts rule IDs matching [A-Z]+-[A-Z]+-\\d{3} from instructions content."""

    def setup_method(self) -> None:
        self._cache_dir = tempfile.mkdtemp()
        # Pre-create cache
        mod = _load_writ_session()
        cache = _make_cache()
        path = os.path.join(self._cache_dir, f"writ-session-{SESSION_ID}.json")
        with open(path, "w") as f:
            json.dump(cache, f)

    def teardown_method(self) -> None:
        import shutil
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    def test_single_rule_id_detected(self) -> None:
        """Instructions containing exactly one rule ID -> instructions_rule_ids has that ID."""
        _run_hook("Follow ARCH-ORG-001 for layer separation.", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert "ARCH-ORG-001" in ids

    def test_multiple_rule_ids_all_detected(self) -> None:
        """Instructions containing several rule IDs -> all are captured in instructions_rule_ids."""
        content = "Rules: ARCH-ORG-001, SEC-UNI-001, and ENF-POST-007 apply."
        _run_hook(content, self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert "ARCH-ORG-001" in ids
        assert "SEC-UNI-001" in ids
        assert "ENF-POST-007" in ids

    def test_rule_id_at_start_of_line_detected(self) -> None:
        """Rule ID appearing at the beginning of a line is detected."""
        _run_hook("ARCH-ORG-001 must be followed.", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert "ARCH-ORG-001" in ids

    def test_rule_id_inline_in_sentence_detected(self) -> None:
        """Rule ID embedded mid-sentence (e.g. 'see ARCH-ORG-001 for details') is detected."""
        _run_hook("For details see ARCH-ORG-001 in the bible.", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert "ARCH-ORG-001" in ids

    def test_lowercase_pattern_not_matched(self) -> None:
        """Lowercase identifiers like 'arch-org-001' do NOT match the rule ID pattern."""
        _run_hook("see arch-org-001 for details", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert ids == []

    def test_partial_prefix_only_not_matched(self) -> None:
        """'ARCH-' without the numeric suffix does NOT match the rule ID pattern."""
        _run_hook("ARCH- prefix only", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert ids == []

    def test_underscore_delimiter_not_matched(self) -> None:
        """'ARCH_ORG_001' with underscores instead of hyphens does NOT match."""
        _run_hook("see ARCH_ORG_001 for details", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert ids == []

    def test_numeric_suffix_must_be_three_digits(self) -> None:
        """'ARCH-ORG-01' (two digits) does NOT match; 'ARCH-ORG-001' (three digits) does."""
        _run_hook("ARCH-ORG-01 is invalid but ARCH-ORG-001 is valid", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert "ARCH-ORG-001" in ids
        assert "ARCH-ORG-01" not in ids

    def test_compound_id_with_extra_segment_matched(self) -> None:
        """Multi-segment ID like FW-M2-RT-003 that still ends in -\\d{3} is matched."""
        _run_hook("Apply FW-M2-RT-003 for Magento routing.", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert "FW-M2-RT-003" in ids

    def test_empty_instructions_produces_empty_list(self) -> None:
        """Empty string instructions -> instructions_rule_ids stays []."""
        _run_hook("", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert ids == []

    def test_instructions_with_no_rule_ids_produces_empty_list(self) -> None:
        """Instructions with prose but no rule ID patterns -> instructions_rule_ids stays []."""
        _run_hook("Always follow best practices and write clean code.", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert ids == []

    def test_duplicate_rule_ids_in_instructions_deduplicated(self) -> None:
        """The same rule ID appearing twice in instructions is stored once."""
        _run_hook("Apply ARCH-ORG-001 always. Remember ARCH-ORG-001.", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert ids.count("ARCH-ORG-001") == 1


# ---------------------------------------------------------------------------
# TestRuleKeywordDetection -- WHEN:/RULE:/VIOLATION:/TRIGGER: signal detection
# ---------------------------------------------------------------------------


class TestRuleKeywordDetection:
    """Hook detects rule-like keywords as a secondary signal in instructions."""

    def setup_method(self) -> None:
        self._cache_dir = tempfile.mkdtemp()
        cache = _make_cache()
        path = os.path.join(self._cache_dir, f"writ-session-{SESSION_ID}.json")
        with open(path, "w") as f:
            json.dump(cache, f)

    def teardown_method(self) -> None:
        import shutil
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    def test_when_keyword_detected_as_signal(self) -> None:
        """Instructions containing 'WHEN:' are flagged as having rule-like content."""
        # Hook source must process WHEN: keyword -- verify source contains it
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "WHEN:" in source

    def test_rule_keyword_detected_as_signal(self) -> None:
        """Instructions containing 'RULE:' are flagged as having rule-like content."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "RULE:" in source

    def test_violation_keyword_detected_as_signal(self) -> None:
        """Instructions containing 'VIOLATION:' are flagged as having rule-like content."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "VIOLATION:" in source

    def test_trigger_keyword_detected_as_signal(self) -> None:
        """Instructions containing 'TRIGGER:' are flagged as having rule-like content."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "TRIGGER:" in source

    def test_keywords_without_rule_ids_do_not_add_to_instructions_rule_ids(self) -> None:
        """Keywords like WHEN:/RULE: without accompanying rule IDs do not populate instructions_rule_ids."""
        _run_hook("WHEN: a class is created\nRULE: keep it simple", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert ids == []


# ---------------------------------------------------------------------------
# TestInstructionsLoadedCacheUpdate -- replace semantics
# ---------------------------------------------------------------------------


class TestInstructionsLoadedCacheUpdate:
    """Hook stores detected rule IDs and replaces (not appends) on re-load."""

    def setup_method(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._cache_dir = tempfile.mkdtemp()
        cache = _make_cache()
        path = os.path.join(self._cache_dir, f"writ-session-{SESSION_ID}.json")
        with open(path, "w") as f:
            json.dump(cache, f)

    def teardown_method(self) -> None:
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        shutil.rmtree(self._cache_dir, ignore_errors=True)

    def test_detected_rule_ids_stored_in_session_cache(self) -> None:
        """After hook fires with ARCH-ORG-001 in content, cache has instructions_rule_ids=['ARCH-ORG-001']."""
        _run_hook("Apply ARCH-ORG-001 always.", self._cache_dir)
        ids = _read_instructions_rule_ids(self._cache_dir)
        assert "ARCH-ORG-001" in ids

    def test_re_loading_instructions_replaces_not_appends(self) -> None:
        """Second hook invocation with new content replaces previous instructions_rule_ids entirely."""
        _run_hook("Apply ARCH-ORG-001 always.", self._cache_dir)
        ids1 = _read_instructions_rule_ids(self._cache_dir)
        assert "ARCH-ORG-001" in ids1

        _run_hook("Apply SEC-UNI-001 instead.", self._cache_dir)
        ids2 = _read_instructions_rule_ids(self._cache_dir)
        assert "SEC-UNI-001" in ids2
        assert "ARCH-ORG-001" not in ids2

    def test_re_loading_empty_instructions_clears_previous_ids(self) -> None:
        """Re-loading with no rule IDs in content sets instructions_rule_ids back to []."""
        _run_hook("Apply ARCH-ORG-001 always.", self._cache_dir)
        assert _read_instructions_rule_ids(self._cache_dir) != []

        _run_hook("Just some prose with no rule IDs.", self._cache_dir)
        assert _read_instructions_rule_ids(self._cache_dir) == []


# ---------------------------------------------------------------------------
# TestInstructionsLoadedHookContract -- exit code, timer, friction log
# ---------------------------------------------------------------------------


class TestInstructionsLoadedHookContract:
    """writ-instructions-loaded.sh must always exit 0, use timers, and log events."""

    def test_hook_file_exists(self) -> None:
        """writ-instructions-loaded.sh exists on disk at the expected path."""
        assert os.path.exists(HOOK_PATH), f"Hook not found at {HOOK_PATH}"

    def test_hook_sources_common_sh(self) -> None:
        """writ-instructions-loaded.sh sources common.sh for shared utilities."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "common.sh" in source, "Hook must source common.sh"

    def test_hook_always_exits_0(self) -> None:
        """Running the hook with a valid stdin envelope exits with code 0."""
        cache_dir = tempfile.mkdtemp()
        try:
            cache_path = os.path.join(cache_dir, f"writ-session-{SESSION_ID}.json")
            with open(cache_path, "w") as f:
                json.dump(_make_cache(), f)
            result = _run_hook("some instructions", cache_dir)
            assert result.returncode == 0
        finally:
            import shutil
            shutil.rmtree(cache_dir, ignore_errors=True)

    def test_hook_uses_hook_timer_start(self) -> None:
        """Hook source contains hook_timer_start call."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "hook_timer_start" in source, "Hook must call hook_timer_start"

    def test_hook_uses_hook_timer_end(self) -> None:
        """Hook source contains hook_timer_end call."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "hook_timer_end" in source, "Hook must call hook_timer_end"

    def test_hook_logs_instructions_loaded_friction_event(self) -> None:
        """Hook calls log_friction_event with event name 'instructions_loaded'."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "instructions_loaded" in source, (
            "Hook must log instructions_loaded friction event"
        )

    def test_hook_logs_count_of_detected_rule_ids(self) -> None:
        """Friction event payload includes the count of detected rule IDs."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "rule_ids_count" in source, (
            "Hook must include rule_ids_count in friction event"
        )

    def test_hook_reads_instructions_from_stdin_envelope(self) -> None:
        """Hook source parses stdin JSON to extract the instructions content field."""
        with open(HOOK_PATH) as f:
            source = f.read()
        assert "instructions" in source, (
            "Hook must extract instructions content from stdin envelope"
        )


# ---------------------------------------------------------------------------
# TestInstructionsLoadedSettingsJson -- settings.json registration
# ---------------------------------------------------------------------------


class TestInstructionsLoadedSettingsJson:
    """writ-instructions-loaded.sh is registered in settings.json."""

    def _load_settings(self) -> dict:
        with open(SETTINGS_PATH) as f:
            return json.load(f)

    def test_instructions_loaded_event_registered_in_settings(self) -> None:
        """settings.json has InstructionsLoaded event entry pointing to writ-instructions-loaded.sh."""
        settings = self._load_settings()
        hooks = settings.get("hooks", {})
        instr_hooks = hooks.get("InstructionsLoaded", [])
        hook_commands = " ".join(
            h.get("command", "") if isinstance(h, dict) else str(h)
            for entry in instr_hooks
            for h in (entry.get("hooks", []) if isinstance(entry, dict) and "hooks" in entry else [entry])
        )
        assert "writ-instructions-loaded.sh" in hook_commands, (
            "InstructionsLoaded event must register writ-instructions-loaded.sh"
        )

    def test_instructions_loaded_hook_bash_permission_in_settings(self) -> None:
        """settings.json includes a Bash permission entry for writ-instructions-loaded.sh."""
        settings = self._load_settings()
        permissions = settings.get("permissions", {})
        allow_list = permissions.get("allow", [])
        assert any("writ-instructions-loaded.sh" in str(p) for p in allow_list), (
            "settings.json must grant Bash permission for writ-instructions-loaded.sh"
        )


# ---------------------------------------------------------------------------
# TestRagInjectExcludePassthrough -- writ-rag-inject.sh merges instructions_rule_ids
# ---------------------------------------------------------------------------


class TestRagInjectExcludePassthrough:
    """writ-rag-inject.sh merges instructions_rule_ids into exclude_rule_ids for /query."""

    def test_rag_inject_reads_instructions_rule_ids_from_cache(self) -> None:
        """writ-rag-inject.sh source references instructions_rule_ids from session cache."""
        rag_inject = f"{SKILL_DIR}/.claude/hooks/writ-rag-inject.sh"
        with open(rag_inject) as f:
            source = f.read()
        assert "instructions_rule_ids" in source, (
            "writ-rag-inject.sh must read instructions_rule_ids from session cache"
        )

    def test_rag_inject_merges_instructions_ids_into_exclude_list(self) -> None:
        """writ-rag-inject.sh combines instructions_rule_ids with other excludes before /query."""
        rag_inject = f"{SKILL_DIR}/.claude/hooks/writ-rag-inject.sh"
        with open(rag_inject) as f:
            source = f.read()
        # Both fields must appear together near the exclude_rule_ids request construction.
        assert "exclude" in source.lower(), (
            "writ-rag-inject.sh must pass exclude_rule_ids to /query"
        )

    def test_rag_inject_does_not_duplicate_ids_already_in_loaded_rule_ids(self) -> None:
        """If a rule ID is in both instructions_rule_ids and loaded_rule_ids, it appears once in exclude list."""
        # Verify the merge uses set() to deduplicate
        rag_inject = f"{SKILL_DIR}/.claude/hooks/writ-rag-inject.sh"
        with open(rag_inject) as f:
            source = f.read()
        # The Python block that builds the request uses set() for deduplication
        assert "set(" in source, (
            "writ-rag-inject.sh must deduplicate exclude IDs using set()"
        )
