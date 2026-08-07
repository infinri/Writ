"""The shared json_transform helper: jq path and python path must agree exactly.

Pins the capabilities.md section "Each converted snippet keeps its exact behavior".

Why this exists: 24 `python3 -c` snippets fire per file write, each ~18ms, doing
JSON reshaping that jq does in ~3ms. Converting them is worth ~360ms per write, and
is only safe if the two paths are interchangeable. This guards the helper that all
24 call sites will use, so the equivalence argument is made once rather than
twenty-four times.

Same contract and same seam as `parsed_field` (see tests/test_b2_json_helpers.py):
jq when available, python when not, `WRIT_NO_JQ=1` to force the fallback, and
byte-identical output either way. Absence of jq changes speed, never behavior.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = str(REPO / "bin" / "lib" / "common.sh")

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; the python path is the fallback"
)

# (label, jq filter, python expression, input) tuples covering the shapes the hooks
# actually feed their inline snippets. The python expression receives the parsed
# object as `d` and returns the string to print, mirroring how the helper will
# accept a fallback expression.
CASES = [
    ("scalar_string", ".tool_name", "d['tool_name']", '{"tool_name":"Write"}'),
    ("nested_path", ".tool_input.file_path", "d['tool_input']['file_path']",
     '{"tool_input":{"file_path":"/tmp/a.py"}}'),
    ("missing_key_default", '.nope // ""', "d.get('nope','')", '{"a":1}'),
    ("null_becomes_default", '.x // ""', "d.get('x') or ''", '{"x":null}'),
    ("integer_render", ".count", "d['count']", '{"count":42}'),
    ("zero_is_not_missing", ".count", "d['count']", '{"count":0}'),
    ("false_is_not_missing", ".flag", "d['flag']", '{"flag":false}'),
    ("bool_true", ".flag", "d['flag']", '{"flag":true}'),
    ("empty_list_len", ".items | length", "len(d['items'])", '{"items":[]}'),
    ("list_len", ".items | length", "len(d['items'])", '{"items":[1,2,3]}'),
    ("unicode_value", ".msg", "d['msg']", '{"msg":"caf\\u00e9 \\u2713"}'),
    ("embedded_quote", ".msg", "d['msg']", '{"msg":"say \\"hi\\""}'),
    ("embedded_newline", ".msg", "d['msg']", '{"msg":"a\\nb"}'),
    ("backslash", ".msg", "d['msg']", '{"msg":"C:\\\\path"}'),
]


def _script(jq_filter: str, py_expr: str, payload: str, *, no_jq: bool, tail: str = "") -> str:
    """A harness that runs under the shell options EVERY HOOK SETS.

    `set -euo pipefail` is not decoration here. Without it this harness reported the two
    arms as equivalent while the jq arm was aborting its caller at exit 5 on malformed
    input and the python arm was carrying on: both wrote nothing to stdout, so a
    stdout-only comparison in a permissive shell saw no difference at all. The bug lived
    in the caller's control flow, not in the output.
    """
    env = "WRIT_NO_JQ=1 " if no_jq else ""
    return (
        "set -euo pipefail\n"
        f"source {shlex.quote(COMMON_SH)} >/dev/null 2>&1\n"
        f"printf '%s' {shlex.quote(payload)} | "
        f"{env}json_transform {shlex.quote(jq_filter)} {shlex.quote(py_expr)}\n"
        f"{tail}"
    )


def _transform(jq_filter: str, py_expr: str, payload: str, *, no_jq: bool = False) -> str:
    return subprocess.run(
        ["bash", "-c", _script(jq_filter, py_expr, payload, no_jq=no_jq)],
        capture_output=True, text=True, timeout=30,
    ).stdout


def _survives(jq_filter: str, py_expr: str, payload: str, *, no_jq: bool = False) -> bool:
    """True if the CALLER reaches the line after json_transform."""
    proc = subprocess.run(
        ["bash", "-c", _script(jq_filter, py_expr, payload, no_jq=no_jq,
                               tail="printf 'SURVIVED'\n")],
        capture_output=True, text=True, timeout=30,
    )
    return proc.stdout.endswith("SURVIVED")


class TestEquivalence:
    @pytest.mark.parametrize("label,jq_filter,py_expr,payload", CASES,
                             ids=[c[0] for c in CASES])
    def test_jq_and_python_paths_are_byte_identical(
        self, label: str, jq_filter: str, py_expr: str, payload: str
    ) -> None:
        jq_out = _transform(jq_filter, py_expr, payload)
        py_out = _transform(jq_filter, py_expr, payload, no_jq=True)
        assert jq_out == py_out, (
            f"{label}: paths diverge\n  jq:     {jq_out!r}\n  python: {py_out!r}"
        )

    @pytest.mark.parametrize("label,jq_filter,py_expr,payload", CASES,
                             ids=[c[0] for c in CASES])
    def test_output_is_non_empty_so_equivalence_is_not_vacuous(
        self, label: str, jq_filter: str, py_expr: str, payload: str
    ) -> None:
        """Two empty strings are equal. Before the helper exists both paths return
        nothing, and every assertion above would pass for the wrong reason.

        Compares the UNSTRIPPED output, because two of these cases (missing_key_default,
        null_becomes_default) have the empty string as their correct answer. A helper
        that ran prints a newline for those; a helper that does not exist prints
        nothing at all. Stripping erases exactly the difference this test is for, and
        would have failed a correct implementation on those two cases.
        """
        assert _transform(jq_filter, py_expr, payload) != "", (
            f"{label}: no output at all, so json_transform did not run"
        )

    def test_number_formatting_does_not_drift(self) -> None:
        """The classic jq-versus-python trap: jq may render an integer as a float.
        A rule id or a count reaching a hook as '42.0' would be a silent defect."""
        assert _transform(".count", "d['count']", '{"count":42}').strip() == "42"

    def test_large_integer_is_not_reformatted(self) -> None:
        assert _transform(".n", "d['n']", '{"n":9007199254740993}').strip() \
            == _transform(".n", "d['n']", '{"n":9007199254740993}', no_jq=True).strip()


class TestFailureParity:
    def test_malformed_json_fails_the_same_way_on_both_paths(self) -> None:
        bad = '{"a": '
        jq_out = _transform(".a", "d['a']", bad)
        py_out = _transform(".a", "d['a']", bad, no_jq=True)
        assert jq_out == py_out

    @pytest.mark.parametrize("payload", ['{"a": ', "{not json", "", "[1,2]", "null"])
    def test_bad_payload_does_not_kill_the_calling_hook(self, payload: str) -> None:
        """The finding this test exists for: jq exits 5 on malformed input, and under
        `set -euo pipefail` that aborted the CALLING HOOK, while the python arm exited 0
        and continued. Comparing stdout could never see it, because both arms print
        nothing. So assert on the caller's survival, on both arms.

        A hook that dies partway through is worse than one that reads an empty value:
        it skips whatever it was going to do next, including its gate.
        """
        assert _survives(".a", "d['a']", payload), (
            f"jq arm killed the caller on {payload!r}"
        )
        assert _survives(".a", "d['a']", payload, no_jq=True), (
            f"python arm killed the caller on {payload!r}"
        )

    def test_a_good_payload_also_survives(self) -> None:
        """Anti-vacuity: if the harness never reached the tail line, the survival
        assertions above would be measuring a broken harness."""
        assert _survives(".a", "d['a']", '{"a":1}')
        assert _survives(".a", "d['a']", '{"a":1}', no_jq=True)

    def test_missing_required_key_fails_the_same_way(self) -> None:
        jq_out = _transform(".absent", "d['absent']", '{"a":1}')
        py_out = _transform(".absent", "d['absent']", '{"a":1}', no_jq=True)
        assert jq_out == py_out

    def test_empty_stdin_fails_the_same_way(self) -> None:
        assert _transform(".a", "d['a']", "") == _transform(".a", "d['a']", "", no_jq=True)


class TestNoInlinePythonLeftOnTheWritePath:
    """The point of the helper is that the call sites stop spawning python.

    A RATCHET, not a zero-assertion. The zero form asserted the end state of a
    conversion that is not finished, so it was red for work not yet done and said
    nothing about regressions in the meantime. These counts are measured, and any
    NEW inline JSON snippet in one of these hooks turns it red immediately.

    Converting a site is expected to LOWER a number here. When one does, lower the
    constant in the same commit: that is the whole mechanism.
    """

    # hook -> inline python-JSON snippet count, measured 2026-08-07.
    # writ-pre-write-dispatch.sh went 5 -> 4: its consolidated stdin parse now runs as
    # bin/lib/pre-write-parse.jq with the python kept as the fallback arm (which is why
    # the count is 4 and not 3 -- a retained fallback is still a snippet, and the test
    # counts snippets, not spawns).
    BUDGET = {
        "writ-pre-write-dispatch.sh": 4,
        "writ-posttool-rag.sh": 4,
        "writ-bash-write-gate.sh": 4,
        "pre-validate-file.sh": 3,
        # Not on the write path: measured, ZERO of these 10 execute on a file write.
        # Converting them changes the cost of writing a RULE file. Kept in the ratchet
        # so the count cannot grow while it waits.
        "validate-rules.sh": 10,
    }

    @staticmethod
    def _snippets(text: str) -> list[int]:
        """Offsets of inline `python3 -c` blocks that do JSON work.

        Scans the SNIPPET BODY, not the line. These blocks open with `python3 -c "`
        and put `import json` on the NEXT line, so a line-based check reported zero
        offenders for a hook that has four of them, which is how an earlier version of
        this test passed while the condition it names was false.
        """
        return [
            m.start() for m in re.finditer(r"python3\s+-c", text)
            if re.search(r"\b(json|import)\b", text[m.start():m.start() + 400])
        ]

    @pytest.mark.parametrize("hook", sorted(BUDGET))
    def test_inline_python_json_snippets_do_not_increase(self, hook: str) -> None:
        budget = self.BUDGET[hook]
        found = self._snippets((REPO / "hooks" / "scripts" / hook).read_text())
        assert len(found) <= budget, (
            f"{hook} has {len(found)} inline python JSON snippets, above the measured "
            f"{budget}. Each one costs ~15ms of interpreter startup every time the hook "
            f"runs. Use json_transform, or a filter file with the python kept as the "
            f"fallback arm."
        )

    @pytest.mark.parametrize("hook", sorted(BUDGET))
    def test_the_ratchet_matches_reality(self, hook: str) -> None:
        """A ratchet set above the true count is slack that hides the next addition.
        This pins it exactly, so converting a site FAILS here until the constant is
        lowered to match, which is what keeps the inventory honest."""
        found = self._snippets((REPO / "hooks" / "scripts" / hook).read_text())
        assert len(found) == self.BUDGET[hook], (
            f"{hook}: ratchet says {self.BUDGET[hook]}, found {len(found)}. If a site "
            f"was converted, lower the constant in this commit."
        )

    def test_the_detector_would_see_a_planted_snippet(self) -> None:
        """Anti-vacuity for the checks above: prove the scan fires on the shape it is
        looking for, so a matching count means matching and not a broken pattern."""
        planted = 'X=$(python3 -c "\nimport json, sys\nprint(1)\n")'
        assert len(self._snippets(planted)) == 1

    def test_the_detector_ignores_python_that_is_not_json_work(self) -> None:
        """The other half of anti-vacuity: a detector that matched every `python3 -c`
        would make the ratchet a count of python calls rather than of JSON snippets,
        and would block conversions that legitimately keep a python call."""
        assert self._snippets('X=$(python3 -c "print(1)")') == []
