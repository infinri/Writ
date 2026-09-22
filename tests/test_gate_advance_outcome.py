"""Tests for the advance-phase response classifier (bin/lib/gate_advance_outcome.py)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin", "lib"))

from gate_advance_outcome import classify  # noqa: E402


# -- Existing tests (must stay GREEN) ----------------------------------------

def test_advanced_response_yields_outcome_and_phase():
    r = classify('{"advanced": true, "phase": "testing"}')
    assert r["outcome"] == "advanced"
    assert r["phase"] == "testing"


def test_rejected_response_yields_reason():
    r = classify('{"advanced": false, "error": "plan.md validation failed: Files section"}')
    assert r["outcome"] == "rejected"
    assert "Files" in r["error"]


def test_error_field_alone_is_rejected():
    r = classify('{"error": "Invalid or missing gate token"}')
    assert r["outcome"] == "rejected"
    assert "token" in r["error"].lower()


def test_empty_response_is_none():
    assert classify("")["outcome"] == "none"
    assert classify("   ")["outcome"] == "none"


def test_malformed_json_is_none_not_raise():
    assert classify("not json at all")["outcome"] == "none"


def test_advanced_without_phase_is_none():
    # advanced:true but no phase -> nothing to confirm, treat as no-op.
    assert classify('{"advanced": true}')["outcome"] == "none"


# -- New noop tests (RED until classify gains the "noop" outcome) -------------

def test_noop_response_with_reason_and_no_error():
    """Server's benign no-advance path: advanced=false + reason, no error key -> noop."""
    r = classify('{"advanced": false, "reason": "No pending gate to advance", "phase": "planning"}')
    assert r["outcome"] == "noop"
    assert r["error"] == ""


def test_noop_passes_phase_through():
    """The phase from the server response is echoed in the noop result."""
    r = classify('{"advanced": false, "reason": "No pending gate to advance", "phase": "implementation"}')
    assert r["outcome"] == "noop"
    assert r["phase"] == "implementation"


def test_noop_with_different_reason():
    """Any truthy reason without error -> noop, regardless of reason text."""
    r = classify('{"advanced": false, "reason": "All gates already approved"}')
    assert r["outcome"] == "noop"


def test_advanced_false_no_reason_no_error_is_rejected():
    """Defensive: unexplained refusal (no reason, no error) -> rejected, fail toward safe."""
    r = classify('{"advanced": false}')
    assert r["outcome"] == "rejected"


def test_error_wins_over_reason():
    """Error key takes priority: advanced=false + reason + error -> rejected, not noop."""
    r = classify('{"advanced": false, "reason": "x", "error": "bad token"}')
    assert r["outcome"] == "rejected"


def test_error_wins_even_with_reason_populated():
    """Confirm the ordering: error present always -> rejected, even if reason is also present."""
    r = classify('{"advanced": false, "error": "x", "reason": "y"}')
    assert r["outcome"] == "rejected"


# -- The tab-separated transport the hook parses with `cut` -------------------
#
# The stdout contract grew from 3 fields to 5 (validated + token_spent inserted before
# the error), and auto-approve-gate.sh reads them by position: outcome=-f1, phase=-f2,
# validated=-f3, token_spent=-f4, error=-f5-. A silent position shift would make the hook
# print an artifact path where the error belongs, or claim the wrong token state. Nothing
# tested the emitted line at all, so these pin the wire format itself.

import json  # noqa: E402
import subprocess  # noqa: E402

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "bin", "lib", "gate_advance_outcome.py")

FIELDS = ("outcome", "phase", "validated", "token_spent", "error")


def _emit(payload: dict) -> list[str]:
    """Run the script the way the hook does and split the first line into fields."""
    proc = subprocess.run(
        [sys.executable, _SCRIPT, json.dumps(payload)],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split("\n")[0].split("\t")


def test_emitted_line_has_five_fields_in_order():
    fields = _emit({"phase": "testing", "project_root": "/p", "root_tier": "cwd",
                    "validated": "/p/plan.md"})
    assert len(fields) == len(FIELDS), f"field count changed: {fields}"
    assert fields[0] == "advanced"
    assert fields[1] == "testing"
    assert "/p/plan.md" in fields[2]
    assert fields[3] == ""          # a successful advance reports no token_spent
    assert fields[4] == ""          # no error


def test_error_is_the_last_field():
    fields = _emit({"advanced": False, "error": "bad plan", "token_spent": True})
    assert fields[0] == "rejected"
    assert fields[3] == "true"
    assert fields[4] == "bad plan"


def test_not_spent_refusal_reports_false():
    fields = _emit({"advanced": False, "error": "no root", "token_spent": False})
    assert fields[3] == "false", "the hook decides its re-approve wording from this field"


def test_missing_token_spent_stays_empty():
    """An older daemon omits the flag; the hook must not infer either state."""
    fields = _emit({"advanced": False, "error": "bad plan"})
    assert fields[3] == ""


def test_validated_never_contains_a_tab_or_newline():
    """A path may legally contain either, and would shift every later field."""
    fields = _emit({"phase": "testing", "project_root": "/p",
                    "validated": "/p/we\tird\nplan.md", "root_tier": "marker"})
    assert len(fields) == len(FIELDS)
    assert "\t" not in fields[2]


def test_multiline_error_keeps_the_fixed_fields_on_line_one():
    """The hook does `head -1 | cut -f4` for token_spent, then `cut -f5-` for the error."""
    proc = subprocess.run(
        [sys.executable, _SCRIPT,
         json.dumps({"advanced": False, "token_spent": True,
                     "error": "line one\nline two\nline three"})],
        capture_output=True, text=True, timeout=30,
    )
    first = proc.stdout.split("\n")[0].split("\t")
    assert first[0] == "rejected"
    assert first[3] == "true"
    assert first[4] == "line one"
    assert "line two" in proc.stdout


# -- The hook's parse of that transport, re-anchored to its post plan.md 2412ba38 shape --
#
# `test_hook_cut_positions_match_the_emitter` used to hand-roll the hook's cut
# expressions directly in a `bash -c` string. Plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3
# ("a multi-line refusal must reach the rejection branch, not the outage branch") changes
# what those expressions ARE: the four FIXED fields (outcome, phase/advanced_to,
# validated, token_spent) move from two guarded (`head -1`) and two unguarded reads to
# ALL FOUR reading a single first-line slice, `OUTCOME_LINE=${OUTCOME_RAW%%$'\n'*}`, while
# the trailing, deliberately unbounded error field keeps reading the raw value. The old
# hand-rolled model happened to still produce the right OUTPUT for every single-line
# payload this file's fixtures use, so it would have stayed GREEN after the fix landed
# while describing a parse the hook no longer has (plan.md: "closed while it is cheap").
#
# THE FIX: the model below is updated to the post-fix expressions, AND anchored -- each
# expression it runs is asserted present, byte for byte, in
# hooks/scripts/auto-approve-gate.sh, so a future drift in the hook's real parse reddens
# this test instead of leaving it green over a dead model. The structural property itself
# (every fixed site is first-line-restricted, the trailing site is not) belongs to the
# derived detector in tests/test_tab_record_cut_guard.py; this test is only the anchor
# for the specific expressions this file's `bash -c` model runs.

_HOOK_PATH = os.path.join(os.path.dirname(__file__), "..", "hooks", "scripts", "auto-approve-gate.sh")

# Each string here must appear verbatim as its own line (module-scope, so both tests
# below read the same literal set) in hooks/scripts/auto-approve-gate.sh once plan.md
# 2412ba38's fix lands. RED today: the hook has not yet been changed to read OUTCOME,
# VALIDATED, TOKEN_SPENT and ADVANCED_TO from a first-line slice named OUTCOME_LINE.
_HOOK_CUT_EXPRESSIONS = (
    "OUTCOME_LINE=${OUTCOME_RAW%%$'\\n'*}",
    'OUTCOME=$(printf \'%s\' "$OUTCOME_LINE" | cut -f1)',
    'VALIDATED=$(printf \'%s\' "$OUTCOME_LINE" | cut -f3)',
    'TOKEN_SPENT=$(printf \'%s\' "$OUTCOME_LINE" | cut -f4)',
    'ADVANCED_TO=$(printf \'%s\' "$OUTCOME_LINE" | cut -f2)',
    'GATE_ERROR=$(printf \'%s\' "$OUTCOME_RAW" | cut -f5-)',
)


def test_every_anchored_cut_expression_is_present_verbatim_in_the_hook():
    """Every string in `_HOOK_CUT_EXPRESSIONS` must appear, byte for byte, in
    hooks/scripts/auto-approve-gate.sh -- the anchor that keeps this file's `bash -c`
    model from silently drifting away from the hook's real parse."""
    with open(_HOOK_PATH, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle.read().split("\n")]
    for expression in _HOOK_CUT_EXPRESSIONS:
        assert lines.count(expression) == 1, (
            f"the model below runs {expression!r}, which appears "
            f"{lines.count(expression)} time(s) as its own line in the hook: the model "
            "would describe a parse the hook does not have"
        )


def _parse_with_the_hooks_expressions(record: str) -> list[str]:
    """Run the anchored expressions themselves, in a real shell, over `record`."""
    program = "\n".join(
        ["OUTCOME_RAW=$(cat)", *_HOOK_CUT_EXPRESSIONS]
        # GATE_ERROR is printed LAST because it is the only field that may carry a
        # newline; everything before it is one line each.
        + ['printf \'%s\\n\' "$OUTCOME" "$ADVANCED_TO" "$VALIDATED" "$TOKEN_SPENT" "$GATE_ERROR"']
    )
    proc = subprocess.run(
        ["bash", "-c", program], input=record, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.split("\n")


def test_hook_cut_positions_match_the_emitter():
    """Parse with the exact, anchored post-fix cut expressions the hook uses, via a
    real shell: the four fixed fields come from OUTCOME_LINE (the first-line slice of
    OUTCOME_RAW) and the error still comes from OUTCOME_RAW itself, unrestricted."""
    advanced = subprocess.run(
        [sys.executable, _SCRIPT, json.dumps({"phase": "testing", "project_root": "/p",
                                              "root_tier": "cwd", "validated": "/p/plan.md"})],
        capture_output=True, text=True, timeout=30,
    ).stdout
    outcome, advanced_to, validated, token_spent, _rest = _parse_with_the_hooks_expressions(
        advanced
    )[:5]
    assert outcome == "advanced"
    assert advanced_to == "testing"
    assert "/p/plan.md" in validated
    assert token_spent == ""

    rejected = subprocess.run(
        [sys.executable, _SCRIPT, json.dumps({"advanced": False, "token_spent": True,
                                              "error": "line one\nline two\nline three"})],
        capture_output=True, text=True, timeout=30,
    ).stdout
    fields = _parse_with_the_hooks_expressions(rejected)
    assert fields[0] == "rejected", (
        "the verdict is read from the FIRST LINE of the record, so a multi-line error "
        f"cannot smear into it: {fields[0]!r}"
    )
    assert fields[1] == ""
    assert fields[2] == ""
    assert fields[3] == "true"
    assert fields[4:7] == ["line one", "line two", "line three"], (
        "the trailing read stays unrestricted, so every line of the error survives: "
        f"{fields[4:]!r}"
    )
