"""The per-prompt path had no budget guard, so its cost was free to drift.

WHY THIS EXISTS SEPARATELY FROM THE WRITE PATH. The two paths fail differently. A file
write fires 15 hooks that are individually cheap, so its cost is process COUNT. A user
prompt fires 3 hooks, and its cost is python INTERPRETER STARTS: measured 2026-08-07,
writ-rag-inject.sh spent ~145ms of its 247ms on 8 of them, while all 5 of its daemon round
trips together came to ~62ms. Optimising the round trips first was the intuitive move and
would have bought ~30ms while leaving the real cost untouched.

THE COST MODEL THE BUDGETS BELOW ARE DERIVED FROM (measured, same machine, median of 10):
    bare `python3 -c pass`      9.5 ms      `import json`             +4.9 ms
    `python3 -S -c pass`        6.7 ms      `import writ.shared.logging` +12.5 ms
    `jq -n 1`                   2.3 ms      `import urllib.request`   +22.0 ms
So converting one call from python to jq saves ~7ms before its imports, and any hot-path
module that reaches urllib pays more for the import than for the work.
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
HOOKS = REPO / "hooks" / "scripts"

# `python3 -c "` / `python3 -S -c '` -- the opener of an embedded program. The quote it
# captures is what closes the block, which is what makes the scan below span lines.
INLINE_PYTHON_OPENER = re.compile(r"""python3\s+(?:-S\s+)?-c\s+(["'])""")


def inline_python_blocks(source: str) -> list[tuple[int, str]]:
    """Every `python3 -c "..."` program embedded in a shell source, as
    (1-based line number of the opener, program text).

    SPANS NEWLINES, and that is the whole point. The scan this replaces asked whether one
    LINE held both `python3 -c` and `json`, but every embedded program in the hook opens
    with a bare `python3 -c "` and does its json work on a later line, so a per-line scan
    could not see a single one of them and answered zero while five were sitting there.

    Inside a double-quoted opener a backslash escapes the next character (bash); inside a
    single-quoted one nothing escapes, so the first quote closes it.
    """
    blocks: list[tuple[int, str]] = []
    pos = 0
    while True:
        opener = INLINE_PYTHON_OPENER.search(source, pos)
        if opener is None:
            return blocks
        quote = opener.group(1)
        start = i = opener.end()
        while i < len(source):
            if source[i] == "\\" and quote == '"':
                i += 2
                continue
            if source[i] == quote:
                break
            i += 1
        blocks.append((source.count("\n", 0, opener.start()) + 1, source[start:i]))
        pos = i + 1

PROMPT_PATH_HOOKS = [
    "writ-manual-test-grant.sh",
    "auto-approve-gate.sh",
    "writ-rag-inject.sh",
]

# Measured 11 on this envelope after converting the escalation check to json_transform.
#
# I first set this to 10 from a hand-rolled probe that measured 9, and the test failed at
# 11. The probe and the test send different session ids, which takes different branches
# (recall and escalation fire for one and not the other), so the probe was measuring one
# path and calling it the cost. The number here comes from the test's own envelope, which
# is the one that will keep running.
PYTHON_BUDGET = 10
# Baseline before this cycle touched the prompt path, so the ratchet demonstrably ratchets.
BASELINE_PYTHON = 13
#
# MEASURED BEFORE/AFTER for the friction change, clean cache, identical envelope,
# writ-rag-inject.sh alone: python 6 -> 5, git 32 -> 0, execve 71 -> 40. The git figure is
# the interesting one: friction-append's `emit` shelled out to resolve the log root, the
# same pattern that dominated the write path, so removing the spawn took its children too.
#
# I briefly set this ratchet to 10 on the strength of a warm-session probe that showed
# 4 -> 3. The clean-cache path is different and only reached 11, so 10 was a number I
# wanted rather than one I measured. It reached 10 for real once the /prompt-bundle
# REQUEST BUILDER moved to `jq -n` (that snippet was a whole interpreter start to
# assemble five strings and a boolean already sitting in shell variables).

# Inline `python3 -c` JSON reads in writ-rag-inject.sh: FIVE remain, at lines 277, 307,
# 330, 423 and 526, each ~14ms (9.5 interpreter floor plus 4.9 for `import json`, to
# answer what jq answers in 2.3).
#
# THIS FILE PREVIOUSLY RECORDED THE COUNT AS ZERO, AND THE ZERO WAS THE TEST'S OWN FAULT.
# The scan asked whether a single LINE contained both `python3 -c` and `json`. Every one
# of these five is a heredoc-style block: `python3 -c "` opens the line, and the `import
# json` / `json.dumps` lands one or more lines BELOW. So the scan could not observe the
# thing it was named after, reported an empty list, and that empty list was written up as
# "now genuinely zero" -- a claim the suite appeared to back. A scan that cannot see its
# subject does not prove absence, it proves nothing, and it is worse than no scan because
# it is quoted as evidence. inline_python_blocks() above now walks from each opener to its
# closing quote, so a block is measured whole.
INLINE_JSON_PYTHON_BLOCKS = 5

ENVELOPE = json.dumps({
    "session_id": "prompt-path-budget-probe",
    "hook_event_name": "UserPromptSubmit",
    "prompt": "add retry logic and error handling to the payment client",
})

pytestmark = pytest.mark.skipif(
    shutil.which("strace") is None, reason="strace unavailable; cannot count execve"
)


def _python_starts(hook: str, cache_dir: Path) -> int:
    trace = Path("/tmp") / f"writ-prompt-budget-{hook}.trace"
    # ISOLATED CACHE, and this is load-bearing rather than tidiness. Without it the hooks
    # read and write the real session cache, so the count depends on state left by earlier
    # runs (a session that has already tripped should-skip takes a shorter path and starts
    # fewer interpreters). The first version of this file omitted it and measured a
    # different number than a standalone probe, which is what exposed it.
    env = {**os.environ, "WRIT_CACHE_DIR": str(cache_dir),
           "WRIT_LOG_ROOT": str(cache_dir / "logs")}
    subprocess.run(
        ["strace", "-f", "-qq", "-e", "trace=execve", "-o", str(trace),
         "bash", str(HOOKS / hook)],
        input=ENVELOPE, capture_output=True, text=True, timeout=180, env=env,
    )
    if not trace.exists():
        pytest.skip(f"strace produced no trace for {hook}")
    text = trace.read_text(errors="replace")
    trace.unlink(missing_ok=True)
    return sum(1 for ln in text.splitlines() if 'python3"' in ln)


@pytest.fixture(scope="module")
def python_total(tmp_path_factory) -> int:
    cache = tmp_path_factory.mktemp("prompt-budget-cache")
    return sum(
        _python_starts(hook, cache) for hook in PROMPT_PATH_HOOKS
        if (HOOKS / hook).exists()
    )


class TestPromptPathBudget:
    def test_python_startups_within_budget(self, python_total: int) -> None:
        assert python_total <= PYTHON_BUDGET, (
            f"{python_total} python interpreter startups per prompt exceeds the ratchet "
            f"of {PYTHON_BUDGET}. Each costs ~9.5ms before it does any work, and ~14ms "
            f"once it imports json. If this is deliberate, say why in the constant."
        )

    def test_the_ratchet_still_ratchets(self, python_total: int) -> None:
        """Anti-vacuity: a budget at or above the starting point guards nothing."""
        assert PYTHON_BUDGET < BASELINE_PYTHON, (
            f"ratchet {PYTHON_BUDGET} is not below the {BASELINE_PYTHON} baseline"
        )

    def test_the_counter_can_see_python(self, tmp_path) -> None:
        """Anti-vacuity: a counter stuck at zero would satisfy every budget above."""
        assert _python_starts("writ-rag-inject.sh", tmp_path) > 0, (
            "counted zero python starts for the RAG hook, which certainly starts python"
        )

    def test_the_count_is_stable_across_runs(self, tmp_path) -> None:
        """A ratchet that drifts is a ratchet nobody trusts.

        Two runs against SEPARATE clean caches must agree. Against a shared cache they do
        not: the second run sees state the first left behind and can take a shorter path.
        """
        a = _python_starts("writ-rag-inject.sh", tmp_path / "a")
        b = _python_starts("writ-rag-inject.sh", tmp_path / "b")
        assert a == b, (
            f"python starts varied between identical clean-cache runs ({a} vs {b}); the "
            f"measurement depends on leftover state and cannot anchor a budget"
        )


class TestPromptStateFallsBackRatherThanGuessing:
    """The skip decision must come from an ANSWER, never from a missing field.

    The hook reads should_skip out of /session/{id}/prompt-state. A daemon too old to have
    that route replies `{"detail":"Not Found"}` with HTTP 404, and writ_http_get (no
    --fail) hands that body back like any other. The first version of the filter tested
    `.should_skip == true`, which is false for an absent field, so a 404 read as "do not
    skip" and the separate should-skip call was never consulted. A session whose budget was
    exhausted would then keep injecting rules for the rest of its life, with nothing
    failing. The filter tests PRESENCE first and emits nothing when the field is missing,
    which is what routes the hook to the fallback.
    """

    FILTER = (
        'if has("should_skip") then (if .should_skip == true then "yes" else "no" end) '
        "else empty end"
    )
    PYEXPR = "(('yes' if d.get('should_skip') is True else 'no') if 'should_skip' in d else None)"

    def _run(self, payload: str, no_jq: bool) -> str:
        env = os.environ.copy()
        if no_jq:
            env["WRIT_NO_JQ"] = "1"
        else:
            env.pop("WRIT_NO_JQ", None)
        script = (
            f'source "{REPO / "bin" / "lib" / "common.sh"}" >/dev/null 2>&1\n'
            f"printf '%s' {json.dumps(payload)} | json_transform "
            f"{json.dumps(self.FILTER)} {json.dumps(self.PYEXPR)} 2>/dev/null || true\n"
        )
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=60, env=env)
        return proc.stdout.strip()

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("payload,expected", [
        ('{"detail":"Not Found"}', ""),          # stale daemon: MUST fall back
        ("", ""),                                # daemon down
        ("garbage", ""),                         # malformed
        ('{"should_skip":true,"cache":{}}', "yes"),
        ('{"should_skip":false,"cache":{}}', "no"),
        ('{"should_skip":null}', "no"),          # present but null is an answer, not a miss
    ])
    def test_both_arms_agree_and_absent_means_fallback(
        self, payload: str, expected: str, no_jq: bool
    ) -> None:
        assert self._run(payload, no_jq) == expected

    def test_the_hook_consults_the_fallback_when_the_field_is_absent(self) -> None:
        """Structural: the empty case must reach `_writ_session should-skip`, not just
        decline to skip. An `if/elif` that lost its elif would still pass the filter
        tests above while silently dropping the policy check."""
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        assert "elif _writ_session should-skip" in source, (
            "the fallback to the separate should-skip call is gone, so a daemon without "
            "/prompt-state would never skip"
        )


class TestEscalationCheckIsNotInlinePython:
    """The converted call, pinned. It read the escalation response through
    `python3 -c 'import sys,json; ...'`, which is 13.7ms measured to answer one boolean."""

    def test_the_escalation_check_no_longer_starts_python(self) -> None:
        """The specific call that was converted, pinned so it cannot quietly revert.

        SCANNED AS BLOCKS, not lines. This test used to ask whether one LINE held both
        `python3 -c` and `needed`, which is the identical shape as the zero-that-proved-
        nothing described at INLINE_JSON_PYTHON_BLOCKS: the opener sits on its own line and
        the program below it. It was not lying today only because no current block happens
        to contain the word, so it was the next instance waiting to happen rather than a
        second live defect. Reverting the conversion would restore a block reading
        `resp["escalation"]["needed"]` under a bare opener, and the per-line form would
        have reported that as clean.
        """
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        reverted = [
            (line, body.strip()[:80])
            for line, body in inline_python_blocks(source)
            if "needed" in body
        ]
        assert reverted == [], (
            f"the escalation check is back to inline python: {reverted}"
        )

    def test_that_scan_sees_a_reverted_escalation_check(self) -> None:
        """Anti-vacuity for the scan above, in the shape the revert would actually take.

        An empty list is the passing result, so the scan has to be shown finding the thing
        before its emptiness means anything -- and shown finding it SPANNING LINES, which
        is exactly what the previous per-line version could not do.
        """
        reverted_form = (
            'ESC_NEEDED=$(printf \'%s\' "$RESP" | python3 -c "\n'
            "import sys, json\n"
            'print(json.load(sys.stdin)[\'escalation\'][\'needed\'])\n'
            '" 2>/dev/null)\n'
        )
        old_scan = [
            ln for ln in reverted_form.splitlines()
            if "python3 -c" in ln and "needed" in ln
        ]
        new_scan = [b for _l, b in inline_python_blocks(reverted_form) if "needed" in b]
        assert old_scan == [], "the per-line scan is supposed to be blind to this revert"
        assert len(new_scan) == 1, f"the block scan missed the revert: {new_scan}"

    def test_escalation_uses_json_transform(self) -> None:
        """Positive half: asserting the old form is gone proves nothing if the new form
        is not there either (deleting the line would pass the test above)."""
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        assert "json_transform" in source and "ESC_NEEDED" in source, (
            "the escalation check no longer routes through json_transform"
        )

    def test_inline_json_python_blocks_do_not_grow(self) -> None:
        """A does-not-grow ratchet at the REAL count, which is five, not zero.

        The previous version of this test asserted zero and passed, because it matched
        `python3 -c` and `json` on the same LINE and every block puts them on different
        lines (see INLINE_JSON_PYTHON_BLOCKS for the full account). The number below is
        measured by walking each block whole.

        `<=` rather than `==` on purpose: converting one of these to json_transform is the
        goal, and a test that fails when the code gets better is a test people delete. It
        fails when a SIXTH appears, and the constant is lowered by whoever removes one.
        """
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        offenders = [
            (line, body.strip().splitlines()[0][:60] if body.strip() else "")
            for line, body in inline_python_blocks(source)
            if "json" in body
        ]
        assert len(offenders) <= INLINE_JSON_PYTHON_BLOCKS, (
            f"inline `python3 -c` JSON parsing grew to {len(offenders)} blocks, above the "
            f"{INLINE_JSON_PYTHON_BLOCKS} recorded here. Each is ~14ms to read one field; "
            f"use json_transform. Blocks at: {offenders}"
        )

    def test_the_scan_sees_a_multi_line_block_the_old_one_missed(self) -> None:
        """Anti-vacuity, and the direct proof that the old scan was blind.

        This sample is the exact shape of all five real blocks: the opener on one line,
        the json usage below it. The old per-line predicate scores it zero; a scan that
        answers zero here would answer zero for the hook as well, which is precisely how a
        false "no inline JSON python remains" claim got made.
        """
        sample = (
            'REQ=$(WRIT_ROOT="$PWD" python3 -c "\n'
            "import os, json\n"
            "print(json.dumps({'root': os.environ['WRIT_ROOT']}))\n"
            '" 2>/dev/null)\n'
        )
        old_scan = [
            ln for ln in sample.splitlines() if "python3 -c" in ln and "json" in ln
        ]
        new_scan = [b for _line, b in inline_python_blocks(sample) if "json" in b]
        assert old_scan == [], "the old per-line scan is supposed to be blind to this"
        assert len(new_scan) == 1, (
            f"the block scan missed a multi-line `python3 -c` block: {new_scan}"
        )
        assert "json.dumps" in new_scan[0], "the block was truncated before its json use"

    def test_the_scan_does_not_swallow_the_rest_of_the_file(self) -> None:
        """The other way to be vacuous: a scanner whose block never ends would report one
        enormous hit containing every word in the file, and 'json' would always be in it.
        Two adjacent blocks must come back as two, and a block with no json must not."""
        sample = (
            'A=$(python3 -c "\nimport json\nprint(json.dumps({}))\n")\n'
            'B=$(python3 -c "\nprint(1 + 1)\n")\n'
        )
        blocks = inline_python_blocks(sample)
        assert len(blocks) == 2, f"expected two separate blocks, got {blocks}"
        assert [line for line, _b in blocks] == [1, 5]
        assert sum(1 for _l, b in blocks if "json" in b) == 1


class TestTheEscalationRoundTripIsSkippedWhenNothingIsPending:
    """/prompt-state already answers "is escalation pending", from the same cache file
    /check-escalation reads, so when the answer is no the round trip only confirms it
    (12.2ms measured).

    THE STALENESS QUESTION HAD TO BE ANSWERED, NOT ASSUMED. An earlier pass kept this call
    fresh because reusing the earlier snapshot could miss an escalation that arrived
    mid-hook. The `escalation` field is written in exactly three places
    (writ/session/approval_workflow.py, violations.py, budget_tracking.py) and none of them
    run during a UserPromptSubmit hook: they fire on gate approvals and write violations.
    Nothing can set it between the two reads in this hook's lifetime, so the snapshot is
    safe HERE, which is not the same as safe in general.
    """

    FILTER = (
        'if has("escalation") then (if .escalation == true then "yes" else "no" end) '
        "else empty end"
    )
    PYEXPR = "(('yes' if d.get('escalation') is True else 'no') if 'escalation' in d else None)"

    def _run(self, payload: str, no_jq: bool) -> str:
        env = os.environ.copy()
        if no_jq:
            env["WRIT_NO_JQ"] = "1"
        else:
            env.pop("WRIT_NO_JQ", None)
        script = (
            f'source "{REPO / "bin" / "lib" / "common.sh"}" >/dev/null 2>&1\n'
            f"printf '%s' {json.dumps(payload)} | json_transform "
            f"{json.dumps(self.FILTER)} {json.dumps(self.PYEXPR)} 2>/dev/null || true\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=60, env=env).stdout.strip()

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("payload,expected", [
        ('{"escalation":false}', "no"),          # skip the round trip
        ('{"escalation":true}', "yes"),          # fetch the full object
        ('{"detail":"Not Found"}', ""),          # stale daemon: MUST fall through
        ("{}", ""),
        ("", ""),
        ("garbage", ""),
    ])
    def test_both_arms_agree_and_absent_means_fetch(self, payload, expected, no_jq) -> None:
        assert self._run(payload, no_jq) == expected

    def test_only_an_explicit_no_skips_the_call(self) -> None:
        """The condition is `= "no"`, not `!= "yes"`. Inverting it would make every
        unavailable answer suppress the escalation warning instead of fetching it."""
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        assert '[ "$PS_ESC" = "no" ]' in source, (
            "the guard no longer requires an explicit no; an empty or error response would "
            "now suppress escalation rather than fall through to the real call"
        )

    def test_the_full_object_is_still_fetched_when_escalation_is_pending(self) -> None:
        """gate, diagnosis, cycles and feedback_sent are read further down and
        /prompt-state returns only the boolean, so the else branch must remain."""
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        assert "_writ_session check-escalation" in source, (
            "the full escalation fetch is gone; the fields below it would all be defaults"
        )
        for field in ("gate", "diagnosis", "cycles", "feedback_sent"):
            assert field in source, f"{field} is no longer read from the escalation object"


class TestTheRendererIsNotSpawnedToDiscoverNothingToDo:
    """writ_render_backward_context.py prints the INVALIDATED block, or nothing at all.

    For a session with no invalidation history it prints nothing, which is nearly every
    session (measured 0 across live sessions), and finding that out cost a 19.5ms
    interpreter start on EVERY prompt. Same shape as the dead mktemp removed earlier this
    cycle: work spawned to learn there was no work.

    The guard is a NECESSARY-condition test, not a copy of the renderer's logic. An empty
    history guarantees no output; a non-empty one still needs the per-gate `.approved`
    check, which stays in the renderer. So the guard can only skip what the renderer would
    have skipped anyway, and that asymmetry is what makes it safe.
    """

    GUARD_JQ = (
        'if ((.invalidation_history // {}) | to_entries '
        "| map(select((.value | length) > 0)) | length) > 0 then \"yes\" else \"no\" end"
    )
    GUARD_PY = "('yes' if any((d.get('invalidation_history') or {}).values()) else 'no')"

    def _guard(self, payload: str, no_jq: bool) -> str:
        env = os.environ.copy()
        if no_jq:
            env["WRIT_NO_JQ"] = "1"
        else:
            env.pop("WRIT_NO_JQ", None)
        script = (
            f'source "{REPO / "bin" / "lib" / "common.sh"}" >/dev/null 2>&1\n'
            f"printf '%s' {json.dumps(payload)} | json_transform "
            f"{json.dumps(self.GUARD_JQ)} {json.dumps(self.GUARD_PY)} 2>/dev/null || true\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=60, env=env).stdout.strip()

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("payload,expected", [
        ("{}", "no"),
        ('{"invalidation_history":{}}', "no"),
        ('{"invalidation_history":{"plan":[]}}', "no"),
        ('{"invalidation_history":{"plan":[{"cycle":1}]}}', "yes"),
        ('{"invalidation_history":{"plan":[],"test":[{"cycle":2}]}}', "yes"),
        ("garbage", ""),      # unreadable: empty, and the hook defaults to running it
    ])
    def test_the_guard_agrees_on_both_arms(self, payload, expected, no_jq) -> None:
        assert self._guard(payload, no_jq) == expected

    def test_the_renderer_really_prints_nothing_without_history(self, tmp_path) -> None:
        """The premise. If the renderer produced output for an empty history, skipping it
        would drop a real warning rather than an empty string."""
        proc = subprocess.run(
            ["python3", str(REPO / "bin" / "lib" / "writ_render_backward_context.py"),
             "{}", str(tmp_path)],
            capture_output=True, text=True, timeout=60)
        assert proc.stdout.strip() == ""

    def test_the_renderer_does_print_when_a_gate_is_invalidated(self, tmp_path) -> None:
        """Anti-vacuity for the test above: a renderer that printed nothing under all
        conditions would make the guard look free while hiding a broken renderer."""
        cache = json.dumps({"invalidation_history": {"plan": [
            {"cycle": 1, "rule_id": "R-1", "file": "a.py", "evidence": "e"}]}})
        proc = subprocess.run(
            ["python3", str(REPO / "bin" / "lib" / "writ_render_backward_context.py"),
             cache, str(tmp_path)],
            capture_output=True, text=True, timeout=60)
        assert "INVALIDATED" in proc.stdout

    def test_the_hook_fails_open(self) -> None:
        """An unreadable cache must RUN the renderer, not skip it: losing a gate
        invalidation warning is worse than paying for a spawn."""
        source = (HOOKS / "writ-rag-inject.sh").read_text()
        assert '"${_HAS_INVALIDATION:-yes}" != "no"' in source, (
            "the guard no longer defaults to running the renderer when the check fails"
        )


class TestNullDefaultsAreNotTheStringNone:
    """A deliberate behaviour change, recorded because it is not a pure refactor.

    The converted reads used `d.get('gate', '?')`, which returns the DEFAULT only when the
    key is absent. A key present with a null value returns None, and `print(None)` emits
    the four characters "None". That was not cosmetic:

      - ORCH_REMAINING_BUDGET feeds `[ "$X" -gt 600 ]`. Bash exits 2 comparing "None",
        so a null budget silently disabled the orchestrator companion branch.
      - ESC_CYCLES is interpolated into user-facing text as "invalidated None times".

    Both arms now map absent AND null to the same default. This DIVERGES from the original
    on the null input, on purpose, and these tests pin the new contract so the divergence
    stays a decision instead of drifting back.
    """

    def _run(self, payload: str, jq_filter: str, pyexpr: str, no_jq: bool) -> str:
        env = os.environ.copy()
        if no_jq:
            env["WRIT_NO_JQ"] = "1"
        else:
            env.pop("WRIT_NO_JQ", None)
        script = (
            f'source "{REPO / "bin" / "lib" / "common.sh"}" >/dev/null 2>&1\n'
            f"printf '%s' {json.dumps(payload)} | json_transform "
            f"{json.dumps(jq_filter)} {json.dumps(pyexpr)} 2>/dev/null || true\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              timeout=60, env=env).stdout.strip()

    FIELDS = {
        "cycles": ('if (.cycles // null) == null then 0 else .cycles end',
                   "(0 if d.get('cycles') is None else d.get('cycles'))", "0"),
        "remaining_budget": (
            'if (.remaining_budget // null) == null then 8000 else .remaining_budget end',
            "(8000 if d.get('remaining_budget') is None else d.get('remaining_budget'))",
            "8000"),
        "gate": ('if (.gate // null) == null then "?" else .gate end',
                 "('?' if d.get('gate') is None else d.get('gate'))", "?"),
    }

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("field", sorted(FIELDS))
    def test_explicit_null_yields_the_default_not_none(self, field: str, no_jq: bool) -> None:
        jq_filter, pyexpr, default = self.FIELDS[field]
        out = self._run(json.dumps({field: None}), jq_filter, pyexpr, no_jq)
        assert out == default, f"{field} on explicit null gave {out!r}, wanted {default!r}"
        assert out != "None", "the string 'None' is what broke bash arithmetic"

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("field", sorted(FIELDS))
    def test_a_real_value_is_passed_through(self, field: str, no_jq: bool) -> None:
        """Anti-vacuity: a filter that always returned the default would pass above."""
        jq_filter, pyexpr, default = self.FIELDS[field]
        value = 42 if field != "gate" else "plan"
        out = self._run(json.dumps({field: value}), jq_filter, pyexpr, no_jq)
        assert out == str(value) and out != default

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    def test_a_falsy_but_real_value_is_not_replaced(self, no_jq: bool) -> None:
        """0 is a legitimate remaining_budget and must NOT become 8000. This is why the
        filter tests `== null` rather than using jq's `//`, which swallows 0 and false."""
        jq_filter, pyexpr, _ = self.FIELDS["remaining_budget"]
        assert self._run('{"remaining_budget":0}', jq_filter, pyexpr, no_jq) == "0"

    def test_bash_cannot_compare_the_string_none(self) -> None:
        """The evidence for calling the old behaviour a bug rather than a style choice."""
        proc = subprocess.run(
            ["bash", "-c", 'X=None; [ "$X" -gt 600 ]'], capture_output=True, text=True)
        assert proc.returncode == 2, (
            "if bash compared 'None' cleanly, the old default would have been harmless"
        )
