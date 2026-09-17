"""Structural + behavior guard for the Wave 2 writ-rag-inject.sh python-extraction split.

writ-rag-inject.sh is the UserPromptSubmit RAG bridge -- the hottest hook in the system
(fires every user turn). The split moves four self-contained `python3 -c "..."` bodies
VERBATIM into standalone stdlib-only bin/lib/*.py files, invoked as `python3 <file>.py args`.
It is spawn-neutral (1 python spawn per block before and after) and leaves every
grep-pinned shell substring (checked by test_pol5b3a et al.) inside the hook.

Extracted:
  648-679 FAILURE_HISTORY render  -> bin/lib/writ_render_failure_history.py   (argv-driven, cold)
  696-723 C10 feedback POST       -> bin/lib/writ_send_escalation_feedback.py (argv-driven, cold)
  740-771 BACKWARD_CTX render     -> bin/lib/writ_render_backward_context.py   (argv-driven, cold)
  86-206  prompt parser           -> bin/lib/writ-prompt-parse.py             (stdin-driven, HOT)

Was RED before the split (the four bin/lib/*.py files did not exist and the hook defined the
blocks inline); GREEN once the extraction landed. It stays as the forward regression guard.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from tests.test_prompt_parse_field_frame import FIELD_CONTRACT

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-rag-inject.sh"
LIB = REPO / "bin" / "lib"
FAILHIST_PY = LIB / "writ_render_failure_history.py"
FEEDBACK_PY = LIB / "writ_send_escalation_feedback.py"
BACKWARD_PY = LIB / "writ_render_backward_context.py"
PARSE_PY = LIB / "writ-prompt-parse.py"

EXTRACTED = [FAILHIST_PY, FEEDBACK_PY, BACKWARD_PY, PARSE_PY]

# Where each field sits in the parser's record, DERIVED from the canonical contract in
# tests/test_prompt_parse_field_frame.py rather than re-pinned as literals here. The payload
# moved to the end of the record so a multi-line prompt could no longer truncate itself and
# shift every field after it; a second hand-written copy of a position is free to drift from
# the first, which is how these two indexes came to need updating at all.
_FIELD_ORDER = list(FIELD_CONTRACT.values())
PROMPT_LINE = _FIELD_ORDER.index("prompt")
HINT_LINE = _FIELD_ORDER.index("hint")

# body-internal signatures that must LEAVE the hook (they live inside the moved python bodies)
MOVED_SIGNATURES = [
    "def extract_keywords",              # block 2 (prompt parser)
    "Same rule triggered all cycles",    # FAILURE_HISTORY render
    "'signal': 'negative'",              # feedback POST
    "do not resubmit unchanged",         # BACKWARD_CTX render
]


class TestExtractedFilesExistAndStdlibOnly:
    def test_all_four_files_exist(self) -> None:
        missing = [str(p) for p in EXTRACTED if not p.exists()]
        assert missing == [], f"extracted helper(s) missing: {missing}"

    def test_files_are_stdlib_only(self) -> None:
        # These run on the per-turn hot path; importing the `writ` PACKAGE would couple
        # them to it and could flake the hook. The standalone sibling module
        # `writ_mode_hint` (stdlib-only, load-robust, resolved via bin/lib on sys.path) is
        # allowed -- the original inline hook code imported it too. So forbid a whole-word
        # `writ` import (from writ / from writ. / import writ) but permit `writ_mode_hint`.
        forbidden = re.compile(r"\b(?:from|import)\s+writ\b")
        for p in EXTRACTED:
            if not p.exists():
                continue
            src = p.read_text()
            assert not forbidden.search(src), (
                f"{p.name} must not import the writ package (writ_mode_hint sibling is OK)"
            )


class TestHookInvokesExtractedFiles:
    def test_hook_references_all_four_files(self) -> None:
        src = HOOK.read_text()
        for p in EXTRACTED:
            assert p.name in src, f"hook must invoke {p.name}"

    def test_moved_bodies_no_longer_inline(self) -> None:
        src = HOOK.read_text()
        still_inline = [s for s in MOVED_SIGNATURES if s in src]
        assert still_inline == [], (
            f"these moved-body signatures must leave the hook: {still_inline}"
        )


class TestInvocationGuardsPreserved:
    """Each invocation keeps its source site's exact stderr redirect + ||-guard (or the
    deliberate lack of one). Diverging would change set -euo pipefail behavior on the
    per-turn hot path."""

    def test_failure_history_unguarded_2devnull(self) -> None:
        src = HOOK.read_text()
        line = _line_containing(src, FAILHIST_PY.name)
        assert "2>/dev/null" in line and "|| true" not in line, (
            f"failure-history invocation must keep 2>/dev/null and NO || true; got: {line!r}"
        )

    def test_backward_context_unguarded_2devnull(self) -> None:
        src = HOOK.read_text()
        line = _line_containing(src, BACKWARD_PY.name)
        assert "2>/dev/null" in line and "|| true" not in line, (
            f"backward-context invocation must keep 2>/dev/null and NO || true; got: {line!r}"
        )

    def test_feedback_redirects_to_sink_and_guarded(self) -> None:
        src = HOOK.read_text()
        line = _line_containing(src, FEEDBACK_PY.name)
        assert 'WRIT_HOOK_LOG_SINK' in line and "|| true" in line, (
            f"feedback invocation must keep 2>>\"$WRIT_HOOK_LOG_SINK\" || true; got: {line!r}"
        )

    def test_prompt_parse_guarded(self) -> None:
        src = HOOK.read_text()
        line = _line_containing(src, PARSE_PY.name)
        assert "|| true" in line, f"prompt-parse invocation must keep || true; got: {line!r}"


class TestRenderFailureHistoryMatches:
    def test_renders_cycle_lines_and_diagnosis(self) -> None:
        cache = {
            "invalidation_history": {
                "phase-a": [
                    {"cycle": 1, "rule_id": "SOLID-SRP-001", "file": "foo.py", "evidence": "bad split"},
                    {"cycle": 2, "rule_id": "SOLID-SRP-001", "file": "bar.py", "evidence": "again"},
                ]
            }
        }
        out = _run_py(FAILHIST_PY, [json.dumps(cache), "phase-a", "same-rule"])
        assert "Cycle 1: SOLID-SRP-001 violated in foo.py" in out.stdout
        assert "Cycle 2: SOLID-SRP-001 violated in bar.py" in out.stdout
        assert "Same rule triggered all cycles" in out.stdout
        assert out.returncode == 0

    def test_malformed_cache_exits_zero_no_cycles(self) -> None:
        out = _run_py(FAILHIST_PY, ["{not json", "phase-a", "different-rules"])
        assert out.returncode == 0
        assert "Cycle" not in out.stdout  # no records rendered; safe fallback


class TestRenderBackwardContextMatches:
    def test_renders_invalidated_gate_block(self, tmp_path) -> None:
        cache = {
            "invalidation_history": {
                "phase-a": [
                    {"cycle": 1, "rule_id": "DRY-DUP-001", "file": "x.py", "evidence": "dup"},
                ]
            }
        }
        # gate_dir has NO phase-a.approved -> gate reads as invalidated
        out = _run_py(BACKWARD_PY, [json.dumps(cache), str(tmp_path)])
        assert out.returncode == 0
        assert "[Writ: phase-a INVALIDATED -- cycle 1 of 3]" in out.stdout
        assert "DRY-DUP-001" in out.stdout

    def test_no_history_empty_output(self, tmp_path) -> None:
        out = _run_py(BACKWARD_PY, [json.dumps({}), str(tmp_path)])
        assert out.returncode == 0
        assert out.stdout.strip() == ""


class TestPromptParseMatches:
    def _parse(self, envelope: dict):
        out = _run_py_stdin(PARSE_PY, json.dumps(envelope))
        lines = out.stdout.split("\n")
        return out, lines

    def test_record_shape_and_passthrough(self) -> None:
        out, lines = self._parse({"session_id": "sid-123", "prompt": "hello there world"})
        assert out.returncode == 0
        # one line per FIELD_CONTRACT field (print adds a trailing newline)
        assert lines[0] == "sid-123"
        # short prompt passes through unchanged, and it is the LAST field
        assert lines[PROMPT_LINE] == "hello there world"

    def test_build_prompt_classifies_work(self) -> None:
        _, lines = self._parse(
            {"session_id": "s", "prompt": "implement the export endpoint from the approved plan"}
        )
        assert lines[HINT_LINE] == "work", (
            f"build prompt must classify work; got {lines[HINT_LINE]!r}"
        )

    def test_audit_prompt_classifies_investigate(self) -> None:
        _, lines = self._parse(
            {"session_id": "s", "prompt": "audit the codebase for security issues"}
        )
        assert lines[HINT_LINE] == "investigate", (
            f"audit prompt must classify investigate; got {lines[HINT_LINE]!r}"
        )

    def test_chat_prompt_no_hint(self) -> None:
        _, lines = self._parse({"session_id": "s", "prompt": "how do I center a div in CSS"})
        assert lines[HINT_LINE] == "", (
            f"chat prompt must yield empty hint; got {lines[HINT_LINE]!r}"
        )

    def test_malformed_stdin_empty_lines_exit_zero(self) -> None:
        out = _run_py_stdin(PARSE_PY, "{not valid json")
        assert out.returncode == 0
        # the except branch prints one empty field per contract, and this count is DERIVED
        # from FIELD_CONTRACT: it is order-invariant but NOT count-invariant, so a literal
        # here goes stale the next time the record gains or loses a field.
        assert out.stdout == "\n" * len(FIELD_CONTRACT)


class TestSendEscalationFeedbackFailOpen:
    """The feedback POSTer's URL is the hardcoded live daemon (a preserved pre-existing
    quirk), so we cannot exercise the real POST without side effects. These pin the two
    side-effect-free paths: no invalidation records -> no network call, clean exit; and a
    malformed cache -> the except -> sys.exit(0) contract. Both prove the extracted script
    loads and fails open without a traceback."""

    def test_no_records_makes_no_post_and_exits_zero(self) -> None:
        # empty invalidation_history for the gate -> rule_ids is empty -> the POST loop is a
        # no-op -> no network call is made (safe against the live daemon), exit 0.
        cache = {"invalidation_history": {}}
        out = _run_py(FEEDBACK_PY, [json.dumps(cache), "phase-a"])
        assert out.returncode == 0, out.stderr
        assert "Traceback" not in out.stderr
        assert out.stdout.strip() == ""

    def test_malformed_cache_exits_zero(self) -> None:
        out = _run_py(FEEDBACK_PY, ["{not json", "phase-a"])
        assert out.returncode == 0, out.stderr
        assert "Traceback" not in out.stderr


class TestBashSyntaxOk:
    def test_hook_still_parses(self) -> None:
        r = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
        assert r.returncode == 0, f"bash -n failed:\n{r.stderr}"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _line_containing(src: str, needle: str) -> str:
    for line in src.splitlines():
        if needle in line:
            return line
    return ""


def _run_py(path: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(path), *args], capture_output=True, text=True, timeout=20
    )


def _run_py_stdin(path: Path, stdin: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(path)], input=stdin, capture_output=True, text=True, timeout=20
    )
