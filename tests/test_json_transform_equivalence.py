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


class TestTheBashWriteGateDenyFilter:
    """The gate filter is extracted from the hook and exercised, because I shipped it
    wrong once in the space of one edit.

    First version used `.can_write // true`. jq's `//` falls through on FALSE as well as
    null, so `false // true` is true: every deny read as an allow and the gate stopped
    denying. This is the same divergence the parser's own header documents, which is
    what makes it worth a test rather than a comment. `//` is safe only on a field where
    false is not a legitimate value.
    """

    GATE = REPO / "hooks" / "scripts" / "writ-bash-write-gate.sh"

    def _filter(self) -> str:
        m = re.search(r"json_transform \\\n\s*'(if \(if has\(\"can_write\"\).*?)' \\",
                      self.GATE.read_text(), re.S)
        assert m, (
            "could not extract the deny filter from writ-bash-write-gate.sh; if the "
            "call site moved, update this extraction rather than pasting a copy"
        )
        return m.group(1)

    @pytest.mark.parametrize("payload,expect_block", [
        ('{"can_write":false,"reason":"blocked by phase-a"}', "blocked by phase-a"),
        ('{"can_write":false}', "Write blocked by a Writ gate."),
        ('{"can_write":false,"reason":""}', "Write blocked by a Writ gate."),
        ('{"can_write":false,"reason":null}', "Write blocked by a Writ gate."),
        ('{"can_write":true}', ""),
        ('{"can_write":true,"reason":"ignored"}', ""),
        ('{}', ""),
    ])
    def test_deny_survives_both_arms(self, payload: str, expect_block: str) -> None:
        py_expr = ("'' if d.get('can_write', True) else "
                   "(d.get('reason') or 'Write blocked by a Writ gate.')")
        for no_jq in (False, True):
            got = _transform(self._filter(), py_expr, payload, no_jq=no_jq).strip()
            arm = "python" if no_jq else "jq"
            assert got == expect_block, (
                f"{arm} arm on {payload}: expected {expect_block!r}, got {got!r}"
            )

    def test_the_broken_spelling_would_have_failed_this_test(self) -> None:
        """Anti-vacuity, and it documents the trap concretely: the `//` spelling really
        does turn a deny into an allow."""
        broken = ('if (.can_write // true) then "" else '
                  '((.reason // "") | if . == "" then "BLOCKED" else . end) end')
        assert _transform(broken, "'unused'", '{"can_write":false}').strip() == ""


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
        # 4 -> 2: the session-id extraction and the response relevance check are now
        # json_transform calls. The two that remain do real python work (regex keyword
        # extraction, and an import of writ_phase_scoped_rules), not JSON reshaping.
        "writ-posttool-rag.sh": 2,
        # 4 -> 2 -> 1: the can-write request body, the fallback response reshape, and the
        # deny reason are converted. This loop runs once per path in the Bash command,
        # so each start it drops is paid per path rather than per command. The 2 -> 1 is
        # a CORRECTION, not a conversion: one of the two counted was a comment (see
        # _snippets). The single survivor is the python fallback arm of the jq-first
        # request builder at writ-bash-write-gate.sh:1230, which is meant to stay.
        "writ-bash-write-gate.sh": 1,
        "pre-validate-file.sh": 3,
        # Not on the write path: measured, ZERO of these execute on a file write.
        # Converting them changes the cost of writing a RULE file. Kept in the ratchet
        # so the count cannot grow while it waits. 10 -> 9 is the same comment
        # correction, at validate-rules.sh:67.
        "validate-rules.sh": 9,
    }

    # A whole-line shell comment. Dropped before counting: see _snippets.
    _COMMENT_LINE = re.compile(r"^[ \t]*#.*$", re.M)

    @classmethod
    def _snippets(cls, text: str) -> list[int]:
        """Offsets of inline `python3 -c` blocks that do JSON work.

        Scans the SNIPPET BODY, not the line. These blocks open with `python3 -c "`
        and put `import json` on the NEXT line, so a line-based check reported zero
        offenders for a hook that has four of them, which is how an earlier version of
        this test passed while the condition it names was false.

        COMMENTS ARE STRIPPED FIRST, and that correction is why two constants below moved
        without a line of production code changing. A hook that explains in prose why a
        `python3 -c "json.dump(...)"` snippet was removed was scored as still having it, so
        the ratchet's "measured inventory" included two sentences: writ-bash-write-gate.sh
        counted 2 where 1 executes, validate-rules.sh counted 10 where 9 do. That is worse
        than an off-by-one, because the number moves when nobody touches the code -- the
        gate-bypass work rewrote one such comment on 2026-08-08 and the count "improved"
        from 2 to 1 on its own. It also means a ratchet could be lowered by deleting an
        explanation, which is the opposite of the incentive this class exists to create.

        Trailing comments after code are NOT stripped: `#` is legal inside a shell string,
        so removing from `#` to end-of-line would corrupt real commands. None of the five
        hooks has one, and a whole-line comment is the form prose actually takes here.
        """
        text = cls._COMMENT_LINE.sub("", text)
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

    def test_the_count_ignores_prose_and_still_sees_code(self) -> None:
        """Anti-vacuity for the comment strip, in both directions.

        Stripping is the kind of correction that can quietly hollow out the whole ratchet:
        a transform that removed too much would drive every count toward zero and each
        `<=` assertion would pass forever. So this pins that a commented snippet scores 0,
        a real one scores 1, and the two together score 1 -- the strip removes exactly the
        prose and leaves the executable line beside it.
        """
        commented = '# was python3 -c "import json; json.dumps({})" before the rewrite\n'
        real = 'B=$(python3 -c "\nimport json\nprint(json.dumps({}))\n")\n'

        assert self._snippets(commented) == [], "a comment is being counted as inventory"
        assert len(self._snippets(real)) == 1, "the strip ate a real snippet"
        assert len(self._snippets(commented + real)) == 1, (
            "the strip must remove the prose and keep the code that follows it"
        )

    @pytest.mark.parametrize("hook,minimum", [
        ("writ-posttool-rag.sh", 2), ("writ-bash-write-gate.sh", 3),
    ])
    def test_the_converted_hooks_actually_call_the_helper(self, hook: str, minimum: int) -> None:
        """The counterpart to the ratchet: a snippet can also vanish by being deleted
        or rewritten as a second inline python. This asserts the replacement is
        json_transform, so the helper has real production callers rather than being a
        tested library nothing uses."""
        text = (REPO / "hooks" / "scripts" / hook).read_text()
        assert text.count("json_transform") >= minimum, (
            f"{hook} calls json_transform {text.count('json_transform')} times, "
            f"expected at least {minimum}"
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


class TestParsedFieldsMatchesParsedFieldPerArm:
    """`parsed_fields` reads N fields in one pass; it must answer exactly what N calls to
    `parsed_field` answer, per arm.

    Why it exists: parsed_field pipes the WHOLE document into a fresh jq for every field.
    The RAG hook called it 5 times on the ~10KB /prompt-bundle response (it carries the
    full always-on rule text), so 50KB of piping and 5 interpreter starts to read 5
    strings, measured at ~38ms. Any divergence here is a silent behaviour change at those
    call sites, which is why this compares against the old helper rather than against
    hand-written expectations.
    """

    DOCS = [
        '{"error":false,"always_on_block":"=== RULES ===\\nline2","rules_text":"r",'
        '"methodology_block":"","nudge":null}',
        '{"error":"boom"}',
        "{}",
        '{"error":null,"nudge":"hi"}',
        '{"always_on_block":"has $(cmd) and `tick` and \\"quotes\\""}',
        "not json at all",
        "",
        '{"n":42,"neg":-1,"uni":"caf\\u00e9 \\u2713","bs":"C:\\\\path"}',
    ]
    FIELDS = ["error", "always_on_block", "rules_text", "methodology_block", "nudge", "n"]

    def _both(self, doc: str, *, no_jq: bool) -> tuple[dict, dict]:
        env = "WRIT_NO_JQ=1 " if no_jq else ""
        pairs = " ".join(f"F_{f}={f}" for f in self.FIELDS)
        singles = "\n".join(
            f"printf 'S_{f}=%s\\n' \"$({env}parsed_field {shlex.quote(doc)} {f})\""
            for f in self.FIELDS
        )
        script = (
            "set -uo pipefail\n"
            f"source {shlex.quote(COMMON_SH)} >/dev/null 2>&1\n"
            f'eval "$({env}parsed_fields {shlex.quote(doc)} {pairs})" 2>/dev/null || true\n'
            + "\n".join(f"printf 'M_{f}=%s\\n' \"${{F_{f}-}}\"" for f in self.FIELDS)
            + "\n" + singles + "\n"
        )
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             timeout=60).stdout
        multi, single = {}, {}
        for line in out.splitlines():
            if line.startswith("M_"):
                k, _, v = line[2:].partition("=")
                multi[k] = v
            elif line.startswith("S_"):
                k, _, v = line[2:].partition("=")
                single[k] = v
        return multi, single

    @pytest.mark.parametrize("no_jq", [False, True], ids=["jq", "python"])
    @pytest.mark.parametrize("doc", DOCS)
    def test_one_pass_equals_n_calls(self, doc: str, no_jq: bool) -> None:
        multi, single = self._both(doc, no_jq=no_jq)
        assert multi == single, (
            f"parsed_fields diverged from parsed_field on {doc[:50]!r} "
            f"({'python' if no_jq else 'jq'} arm)"
        )

    def test_a_value_cannot_break_out_of_the_eval(self) -> None:
        """The output is eval'd, so @sh / shlex.quote is load-bearing. A field carrying
        a command substitution must arrive as literal text, never execute."""
        doc = '{"nudge":"$(touch /tmp/writ-parsed-fields-pwned)"}'
        script = (
            "set -uo pipefail\n"
            f"source {shlex.quote(COMMON_SH)} >/dev/null 2>&1\n"
            f'eval "$(parsed_fields {shlex.quote(doc)} V=nudge)"\n'
            'printf "%s" "$V"\n'
        )
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             timeout=60).stdout
        assert out == "$(touch /tmp/writ-parsed-fields-pwned)"
        assert not Path("/tmp/writ-parsed-fields-pwned").exists()

    def test_the_comparison_is_not_vacuous(self) -> None:
        """If parsed_fields emitted nothing for every doc, every dict above would be
        empty-string-filled and still compare equal to a broken parsed_field. Pin that a
        real value actually round-trips."""
        multi, _ = self._both('{"nudge":"real-value"}', no_jq=False)
        assert multi["nudge"] == "real-value"
