"""Tests for bin/lib/writ_phase_scoped_rules.py (Wave-3 DRY dedup).

Three hooks currently inline the same "phase-scoped rule-id selection" body
as a `python3 -c` one-liner:

  - hooks/scripts/writ-read-rag.sh:153-162        (guarded: `2>/dev/null || echo '[]'`)
  - hooks/scripts/writ-rag-inject.sh:253-262       (byte-identical to read-rag; sets
                                                     ORCH_LOADED_RULE_IDS; same guard)
  - hooks/scripts/writ-posttool-rag.sh:168-183     (fused with budget/mode on 3 stdout
                                                     lines consumed via `sed -n 'Np'`;
                                                     has its OWN try/except; UNGUARDED)

The planned module extracts the pure selection logic into
`phase_scoped_ids(cache: dict) -> list` plus a stdlib `__main__` block that
reads a cache dict as JSON on stdin and prints the selected rule-id list as
JSON on stdout (no try/except -- malformed input must raise and exit non-zero,
which is what lets the *callers'* existing shell guards keep degrading to
`[]`).

Import style mirrors tests/test_approval_patterns.py (sys.path.insert into
bin/lib, then a plain `from <module> import <fn>`), except the import is
guarded here so a missing module fails each dependent test individually
(clear per-test RED signal) instead of erroring collection for the whole
file -- TestHooksAdoptHelper (source-only, no import needed) must still run
and report its own independent RED reason (hooks not yet adopted).
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = REPO_ROOT / "bin" / "lib"
MODULE_PATH = LIB_DIR / "writ_phase_scoped_rules.py"

HOOKS_DIR = REPO_ROOT / "hooks" / "scripts"
READ_RAG_HOOK = HOOKS_DIR / "writ-read-rag.sh"
RAG_INJECT_HOOK = HOOKS_DIR / "writ-rag-inject.sh"
POSTTOOL_RAG_HOOK = HOOKS_DIR / "writ-posttool-rag.sh"

sys.path.insert(0, str(LIB_DIR))

try:
    from writ_phase_scoped_rules import phase_scoped_ids  # noqa: E402  # RED until module exists
    _IMPORT_ERROR = None
except ImportError as exc:  # RED until module exists
    phase_scoped_ids = None
    _IMPORT_ERROR = exc


def _require_module():
    """Fail the calling test with a clear reason if the module isn't importable yet.

    Used as a setup_method guard so each test in a dependent class reports its
    own RED failure, rather than one opaque collection error for the file.
    """
    if _IMPORT_ERROR is not None:
        pytest.fail(
            f"bin/lib/writ_phase_scoped_rules.py is not importable: {_IMPORT_ERROR!r}"
        )


# The exact HEAD inline body shared (byte-for-byte) by writ-read-rag.sh:153-162
# and writ-rag-inject.sh:253-262, run as `python3 -c HEAD_BODY` with the cache
# JSON piped on stdin. Used as the differential reference in
# TestDifferentialVsHeadInline -- the new module's __main__ path must produce
# byte-identical stdout to this snippet for every well-formed cache shape.
HEAD_BODY = """import sys, json
cache = json.load(sys.stdin)
by_phase = cache.get('loaded_rule_ids_by_phase', {})
current_phase = cache.get('current_phase', '')
if by_phase and current_phase:
    print(json.dumps(by_phase.get(current_phase, [])))
else:
    print(json.dumps(cache.get('loaded_rule_ids', [])))
"""


# Well-formed cache variants shared across TestPhaseScopedIdsFunction (list-type
# check), TestMainStdinStdout, and TestDifferentialVsHeadInline. Excludes the
# malformed-stdin case, which is covered only in TestMainStdinStdout (both the
# new script and HEAD_BODY raise on malformed input, so there is nothing to
# differential-compare there).
CACHE_VARIANTS = [
    pytest.param({}, id="empty_cache"),
    pytest.param({"loaded_rule_ids": ["A", "B"]}, id="flat_list_no_by_phase"),
    pytest.param(
        {
            "loaded_rule_ids_by_phase": {"planning": ["P1", "P2"]},
            "current_phase": "planning",
            "loaded_rule_ids": ["A"],
        },
        id="phase_bucket_matches_current_phase",
    ),
    pytest.param(
        {
            "loaded_rule_ids_by_phase": {"planning": ["P1", "P2"]},
            "current_phase": "",
            "loaded_rule_ids": ["A", "B"],
        },
        id="empty_current_phase_falls_back_to_flat_list",
    ),
    pytest.param(
        {
            "loaded_rule_ids_by_phase": {"planning": ["P1", "P2"]},
            "current_phase": "testing",
        },
        id="current_phase_not_a_key_in_by_phase",
    ),
    pytest.param(
        {
            "loaded_rule_ids_by_phase": {},
            "current_phase": "planning",
            "loaded_rule_ids": ["A", "B"],
        },
        id="empty_by_phase_dict_falls_back_to_flat_list",
    ),
]


# -- 1. Unit tests for the pure function -------------------------------------

class TestPhaseScopedIdsFunction:
    def setup_method(self):
        _require_module()

    def test_empty_cache_returns_empty_list(self):
        assert phase_scoped_ids({}) == []

    def test_flat_loaded_rule_ids_returned_when_no_by_phase_key(self):
        cache = {"loaded_rule_ids": ["A", "B"]}
        assert phase_scoped_ids(cache) == ["A", "B"]

    def test_phase_bucket_wins_over_flat_list_when_current_phase_set(self):
        """THE branch the existing behavioral tests never hit: when both
        loaded_rule_ids_by_phase and current_phase are populated, the
        current phase's bucket is returned instead of the flat
        loaded_rule_ids list, even though the flat list is also present."""
        cache = {
            "loaded_rule_ids_by_phase": {"planning": ["P1", "P2"]},
            "current_phase": "planning",
            "loaded_rule_ids": ["A"],
        }
        assert phase_scoped_ids(cache) == ["P1", "P2"]

    def test_falls_back_to_flat_list_when_current_phase_is_empty_string(self):
        cache = {
            "loaded_rule_ids_by_phase": {"planning": ["P1", "P2"]},
            "current_phase": "",
            "loaded_rule_ids": ["A", "B"],
        }
        assert phase_scoped_ids(cache) == ["A", "B"]

    def test_returns_empty_list_when_current_phase_not_a_key_in_by_phase(self):
        cache = {
            "loaded_rule_ids_by_phase": {"planning": ["P1", "P2"]},
            "current_phase": "testing",
        }
        assert phase_scoped_ids(cache) == []

    def test_falls_back_to_flat_list_when_by_phase_dict_is_empty(self):
        """by_phase = {} is falsy, so the `by_phase and current_phase` guard
        takes the flat-list branch even though current_phase is set."""
        cache = {
            "loaded_rule_ids_by_phase": {},
            "current_phase": "planning",
            "loaded_rule_ids": ["A", "B"],
        }
        assert phase_scoped_ids(cache) == ["A", "B"]

    @pytest.mark.parametrize("cache", CACHE_VARIANTS)
    def test_return_value_is_always_a_list(self, cache):
        assert isinstance(phase_scoped_ids(cache), list)


# -- 2. The CLI path: stdin -> stdout ----------------------------------------

class TestMainStdinStdout:
    def setup_method(self):
        _require_module()

    @staticmethod
    def _run_main(stdin_text):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize("cache", CACHE_VARIANTS)
    def test_main_stdout_matches_function_result_and_exits_zero(self, cache):
        result = self._run_main(json.dumps(cache))
        assert result.returncode == 0
        assert json.loads(result.stdout) == phase_scoped_ids(cache)

    def test_malformed_stdin_exits_non_zero(self):
        """No try/except in __main__: malformed JSON must raise and exit
        non-zero. This is the behavior read-rag.sh and rag-inject.sh rely on
        -- their shell guard (`2>/dev/null || echo '[]'`) only degrades to
        `[]` on a genuinely non-zero exit."""
        result = self._run_main("not json{")
        assert result.returncode != 0


# -- 3. Differential test: new module vs. the HEAD inline body --------------

class TestDifferentialVsHeadInline:
    """Proves the new __main__ path is byte-identical to the inline
    `python3 -c` body read-rag.sh and rag-inject.sh run at HEAD, for every
    well-formed cache shape. Does not need the phase_scoped_ids import --
    both sides are exercised purely as subprocesses."""

    @staticmethod
    def _run_new_module(stdin_text):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH)],
            input=stdin_text,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _run_head_inline(stdin_text):
        return subprocess.run(
            [sys.executable, "-c", HEAD_BODY],
            input=stdin_text,
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize("cache", CACHE_VARIANTS)
    def test_new_module_stdout_byte_identical_to_head_inline_body(self, cache):
        stdin_text = json.dumps(cache)
        new_result = self._run_new_module(stdin_text)
        head_result = self._run_head_inline(stdin_text)
        assert new_result.stdout == head_result.stdout


# -- 4. Source guard: hooks adopt the helper, inline body is gone -----------

# THE ADOPTER POPULATIONS ARE DERIVED, NEVER LISTED.
#
# `bin/lib/writ_phase_scoped_rules.py` is still live and still needs its adopters
# pinned; what changed is WHICH hooks adopt it. Plan dfacff61 moved the rag-inject
# integration server-side, so a per-hook test naming writ-rag-inject.sh went red
# for a correct deletion while proving nothing about the helper.
#
# Two populations, because there are two adoption idioms and they carry different
# obligations. A hook that runs the helper as a SCRIPT gets a string back through
# a command substitution and must degrade to '[]' when it fails. A hook that
# IMPORTS phase_scoped_ids calls it in-process, where there is no substitution to
# degrade, and writ-posttool-rag.sh is deliberately unguarded there.
def _hooks_matching(needle: str) -> list[str]:
    return sorted(
        path.name for path in HOOKS_DIR.glob("*.sh") if needle in path.read_text()
    )


# Runs the helper as a script: `python3 .../writ_phase_scoped_rules.py`.
HELPER_SCRIPT_ADOPTERS = _hooks_matching("writ_phase_scoped_rules.py")
# Imports the function: `from writ_phase_scoped_rules import phase_scoped_ids`.
HELPER_IMPORT_ADOPTERS = _hooks_matching("from writ_phase_scoped_rules import phase_scoped_ids")
# Either idiom. Used for the "nobody inlines the selection any more" sweep.
HELPER_ADOPTERS = sorted(set(HELPER_SCRIPT_ADOPTERS) | set(HELPER_IMPORT_ADOPTERS))


class TestHooksAdoptHelper:
    """Reads hook source text directly (no subprocess, no import). This class
    does not depend on writ_phase_scoped_rules.py existing, so it reports its own
    independent RED reason: the hooks have not been repointed at the helper.

    RE-KEYED from three hardcoded hook names to two derived populations. The
    hardcoded version had writ-rag-inject.sh in it and went red when that hook
    stopped using the helper, which is a correct change; worse, it could never
    have noticed a NEW adopter that inlined the selection anyway.

    MUTATION: putting the literal `by_phase.get(current_phase, [])` back into
    writ-read-rag.sh or writ-posttool-rag.sh turns the matching parameter of
    test_adopter_no_longer_inlines_phase_bucket_selection red. Dropping
    `|| echo '[]'` from writ-read-rag.sh turns its guard parameter red. Adding a
    THIRD adopter covers it automatically, with nothing to remember here.

    DELETED with this re-key, and recorded rather than silently dropped:
    test_rag_inject_hook_preserves_orch_loaded_rule_ids_var_name, which pinned the
    shell variable name ORCH_LOADED_RULE_IDS. That variable existed only inside
    the hand-rolled orchestrator companion block, which no longer exists, so the
    property is gone rather than moved. It was a name pin in the first place: it
    could not have detected the variable being computed wrongly, only renamed.
    """

    def test_the_adopter_populations_are_not_empty(self) -> None:
        """Anti-vacuity for every parametrized test below: an empty population
        makes them all vanish and the class read green while asserting nothing.
        Both idioms must still have at least one adopter, or the helper is dead
        code and this class should go with it."""
        assert HELPER_SCRIPT_ADOPTERS, (
            "no hook runs writ_phase_scoped_rules.py as a script; the helper may "
            "be dead code, or the detector needle has drifted"
        )
        assert HELPER_IMPORT_ADOPTERS, (
            "no hook imports phase_scoped_ids from writ_phase_scoped_rules"
        )

    @pytest.mark.parametrize("hook_name", HELPER_SCRIPT_ADOPTERS)
    def test_script_adopter_invokes_the_helper(self, hook_name: str) -> None:
        content = (HOOKS_DIR / hook_name).read_text()
        assert "writ_phase_scoped_rules.py" in content, (
            f"{hook_name} does not invoke writ_phase_scoped_rules.py; "
            "it may still be running the inline python3 -c selection body."
        )

    @pytest.mark.parametrize("hook_name", HELPER_IMPORT_ADOPTERS)
    def test_import_adopter_imports_phase_scoped_ids_function(self, hook_name: str) -> None:
        content = (HOOKS_DIR / hook_name).read_text()
        assert "from writ_phase_scoped_rules import phase_scoped_ids" in content, (
            f"{hook_name} does not import phase_scoped_ids from "
            "writ_phase_scoped_rules; it may still be running its own fused "
            "inline python3 -c body."
        )

    @pytest.mark.parametrize("hook_name", HELPER_ADOPTERS)
    def test_adopter_no_longer_inlines_phase_bucket_selection(self, hook_name: str) -> None:
        content = (HOOKS_DIR / hook_name).read_text()
        assert content.count("by_phase.get(current_phase, [])") == 0, (
            f"{hook_name} still contains the inline phase-bucket selection line; "
            "the logic must live only in the helper module."
        )

    def test_no_hook_outside_the_adopters_inlines_the_selection(self) -> None:
        """The sweep the per-hook list could not do: a hook that never adopted
        the helper and still carries its own copy of the selection line is the
        duplication this refactor existed to remove, and it would be invisible to
        a population derived from adopters alone."""
        offenders = sorted(
            path.name for path in HOOKS_DIR.glob("*.sh")
            if "by_phase.get(current_phase, [])" in path.read_text()
        )
        assert offenders == [], (
            f"hook(s) still inline the phase-bucket selection: {offenders}"
        )

    @pytest.mark.parametrize("hook_name", HELPER_SCRIPT_ADOPTERS)
    def test_script_adopter_preserves_guard_fallback_to_empty_list(
        self, hook_name: str
    ) -> None:
        """Guard asymmetry: a hook that runs the helper as a SCRIPT reads its
        answer through a command substitution, so it must keep degrading to '[]'
        when the helper fails. The import adopters are excluded on purpose:
        writ-posttool-rag.sh calls the function in-process and is intentionally
        unguarded there."""
        content = (HOOKS_DIR / hook_name).read_text()
        assert "|| echo '[]'" in content, (
            f"{hook_name} runs the helper as a script but no longer degrades to "
            "'[]' when it fails"
        )

    def test_posttool_rag_hook_preserves_three_line_sed_consumption(self):
        """posttool-rag.sh's fused shape (rule_ids/budget/mode on 3 stdout
        lines) must remain intact after adoption: only the selection body moves
        into the module, not the fused 3-line print/consume contract.

        Named rather than derived, because this pins ONE hook's own stdout
        contract with its own inline block, not a property of adopting the
        helper."""
        content = POSTTOOL_RAG_HOOK.read_text()
        assert "sed -n '1p'" in content
        assert "sed -n '2p'" in content
        assert "sed -n '3p'" in content
