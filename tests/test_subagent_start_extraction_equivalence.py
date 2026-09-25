"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, batch 4b: writ-subagent-start.sh's three
remaining inline `python3 -c` JSON extractions (AGENT_PROMPT, RULES_INJECTED_EXTRA,
SA_OUTPUT) move to jq-first helpers, with the current python kept verbatim as the
WRIT_NO_JQ fallback.

Each of these extractions is gated (AGENT_PROMPT and RULES_INJECTED_EXTRA behind
`[ -n "$HEALTH" ]` and a scored /query response; SA_OUTPUT behind a PHASE_INFO that is
in practice never empty) or fed inputs (malformed /query bodies, a bare JSON array
envelope) the full hook cannot be driven to reproduce end-to-end -- `cmd_format`
(writ/session/budget_tracking.py:361) exits with NO output at all the moment `rules`
is falsy, so RULES_TEXT is empty and the whole RULES_INJECTED_EXTRA block never runs
for most of the malformed shapes this capability enumerates. So these tests exercise
the THREE TRANSFORMS DIRECTLY: HEAD's own inline python (called verbatim, unmodified,
as the reference), the shared `json_transform` helper (bin/lib/common.sh) fed the
literal jq filter / python expression the plan's Analysis section specifies for
AGENT_PROMPT, and -- for RULES_INJECTED_EXTRA and SA_OUTPUT, which json_transform
cannot carry (side values / no env access) -- the jq-first inline pattern
writ-posttool-rag.sh:261-277 already established, invoked directly via `jq`/`jq -n`
subprocesses.

RULES_INJECTED_EXTRA's jq filter has no literal text in the plan (only a "shape for
shape" prose spec), so `_RULES_INJECTED_EXTRA_JQ_FILTER` below is this test's own
derivation of that spec -- the acceptance contract the real jq arm must satisfy, not a
transcription of implementation text. It was hand-verified against every enumerated
case before being pinned here.

Per TEST-REGRESSION-001: the equivalence assertions are mostly GREEN today (both
"arms" tested are independent implementations of the SAME python behaviour and were
verified to already agree), which is the correct state for a characterization test
pinning a contract nothing violates yet; the one deliberately RED capability is the
list-valued-task divergence (HEAD's python prints a repr, the new arms must not), and
the source-inventory test (the three inline extractions gone from their own arms) is
RED at HEAD by construction (today they are not gated by jq/WRIT_NO_JQ at all).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
HOOK_SOURCE_PATH = REPO / "hooks" / "scripts" / "writ-subagent-start.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")

# ---------------------------------------------------------------------------------
# AGENT_PROMPT (writ-subagent-start.sh:263-271)
# ---------------------------------------------------------------------------------

# Verbatim from HEAD (writ-subagent-start.sh:264-271), run directly as the reference.
_HEAD_AGENT_PROMPT_PY = """
import sys, json
try:
    d = json.load(sys.stdin)
    prompt = d.get('task') or d.get('prompt') or d.get('description') or d.get('message') or ''
    print(prompt[:500])
except Exception:
    print('')
"""

# Literal from the plan's Analysis section, 4b, AGENT_PROMPT.
_NEW_AGENT_PROMPT_JQ_FILTER = (
    '[.task, .prompt, .description, .message] | '
    'map(select(. != null and . != false and . != "" and . != 0 and . != [] and . != {})) | '
    '(.[0] // "") | if type == "string" then .[:500] else "" end'
)
_NEW_AGENT_PROMPT_PY_EXPR = (
    "(lambda p: p[:500] if str(p) == p else '')"
    "(d.get('task') or d.get('prompt') or d.get('description') or d.get('message') or '')"
)


def _head_agent_prompt(stdin: str) -> str:
    r = subprocess.run(["python3", "-c", _HEAD_AGENT_PROMPT_PY], input=stdin,
                       capture_output=True, text=True, timeout=10)
    return r.stdout.rstrip("\n")


def _json_transform(stdin: str, jq_filter: str, py_expr: str, *, no_jq: bool) -> str:
    """`printf '%s' <stdin> | json_transform <jq_filter> <py_expr>` through the real,
    unmodified `json_transform` (bin/lib/common.sh) -- the exact helper AGENT_PROMPT's
    new call site is specified to use."""
    cmd = f"source {shlex.quote(str(COMMON_SH))}; json_transform {shlex.quote(jq_filter)} {shlex.quote(py_expr)}"
    env = os.environ.copy()
    if no_jq:
        env["WRIT_NO_JQ"] = "1"
    r = subprocess.run(["bash", "-c", cmd], input=stdin, env=env,
                       capture_output=True, text=True, timeout=10)
    return r.stdout.rstrip("\n")


_AGENT_PROMPT_CASES = [
    pytest.param('{"task": "investigate the retrieval module"}', id="task-present"),
    pytest.param('{"prompt": "review the write gate for bypasses"}', id="prompt-fallthrough"),
    pytest.param('{"description": "plan the migration"}', id="description-fallthrough"),
    pytest.param('{"message": "implement the fix"}', id="message-fallthrough"),
    pytest.param('{"task": "", "prompt": "falls through the empty task"}', id="empty-string-task-falls-through"),
    pytest.param(json.dumps({"task": "A" * 600}), id="over-500-chars-cut-at-500"),
    pytest.param('{"agent_type": "writ-explorer"}', id="missing-fields"),
    pytest.param("not json at all", id="non-json-input"),
    pytest.param("[1, 2, 3]", id="json-array-envelope"),
]


class TestAgentPromptEquivalence:
    """Capability: AGENT_PROMPT from the jq arm and the WRIT_NO_JQ arm equals HEAD's
    python output, across task/prompt/description/message fallthrough, an
    empty-string task, a >500-char cut, missing fields, non-JSON input and a JSON
    array envelope."""

    @pytest.mark.parametrize("stdin", _AGENT_PROMPT_CASES)
    def test_jq_arm_matches_head(self, stdin):
        expected = _head_agent_prompt(stdin)
        actual = _json_transform(stdin, _NEW_AGENT_PROMPT_JQ_FILTER, _NEW_AGENT_PROMPT_PY_EXPR, no_jq=False)
        assert actual == expected, (stdin, expected, actual)

    @pytest.mark.parametrize("stdin", _AGENT_PROMPT_CASES)
    def test_writ_no_jq_arm_matches_head(self, stdin):
        expected = _head_agent_prompt(stdin)
        actual = _json_transform(stdin, _NEW_AGENT_PROMPT_JQ_FILTER, _NEW_AGENT_PROMPT_PY_EXPR, no_jq=True)
        assert actual == expected, (stdin, expected, actual)


class TestAgentPromptListValuedTaskDivergence:
    """Capability: a list-valued `task` yields an EMPTY AGENT_PROMPT on both new arms
    (HEAD printed a python repr of the list). This is the one deliberate malformed-
    input divergence the plan pins explicitly. RED at HEAD's own expectation: the
    reference and the new arms must DISAGREE here, not agree."""

    _STDIN = json.dumps({"task": [1, 2, 3]})

    def test_head_prints_a_python_repr(self):
        assert _head_agent_prompt(self._STDIN) == "[1, 2, 3]"

    def test_jq_arm_yields_empty(self):
        actual = _json_transform(self._STDIN, _NEW_AGENT_PROMPT_JQ_FILTER, _NEW_AGENT_PROMPT_PY_EXPR, no_jq=False)
        assert actual == ""

    def test_writ_no_jq_arm_yields_empty(self):
        actual = _json_transform(self._STDIN, _NEW_AGENT_PROMPT_JQ_FILTER, _NEW_AGENT_PROMPT_PY_EXPR, no_jq=True)
        assert actual == ""


# ---------------------------------------------------------------------------------
# RULES_INJECTED_EXTRA (writ-subagent-start.sh:323-335)
# ---------------------------------------------------------------------------------

# Verbatim from HEAD (writ-subagent-start.sh:324-335), run directly as the reference.
_HEAD_RULES_INJECTED_EXTRA_PY = """
import sys, json, os
try:
    rs = json.load(sys.stdin).get('rules', [])
except Exception:
    rs = []
print(json.dumps({
    'agent_type': os.environ.get('AGENT_TYPE', ''),
    'query_source': os.environ.get('QSRC', ''),
    'rule_count': len(rs),
    'rule_ids': [r.get('rule_id', '') for r in rs],
}))
"""

# This test's own derivation of the plan's "shape for shape" prose spec (## Analysis,
# 4b, RULES_INJECTED_EXTRA): non-JSON/non-object input and a missing `rules` key both
# give an empty rule set; `rules` of [], {} or "" gives count 0; null, a number, a
# non-empty string, a non-empty object, or any non-dict element gives {} (mirroring
# HEAD's python raising and `|| echo '{}'` taking over); a present `rule_id: null`
# stays null, an absent key becomes "". `-R -s` (raw slurp) lets the filter recover
# from a parse failure itself, matching python's own try/except around json.load.
_RULES_INJECTED_EXTRA_JQ_FILTER = """
(try fromjson catch null) as $resp
| if ($resp == null) or (($resp | type) != "object") then
    {agent_type: $at, query_source: $qs, rule_count: 0, rule_ids: []}
  else
    ((if ($resp | has("rules")) then $resp.rules else [] end)) as $r
    | ($r | type) as $t
    | if $t == "null" then {}
      elif $t == "number" then {}
      elif $t == "string" then
        (if ($r | length) == 0 then
          {agent_type: $at, query_source: $qs, rule_count: 0, rule_ids: []}
        else {} end)
      elif $t == "object" then
        (if ($r | length) == 0 then
          {agent_type: $at, query_source: $qs, rule_count: 0, rule_ids: []}
        else {} end)
      elif $t == "array" then
        (if ($r | any(type != "object")) then {}
        else
          {agent_type: $at, query_source: $qs, rule_count: ($r | length),
           rule_ids: [$r[] | (if has("rule_id") then .rule_id else "" end)]}
        end)
      else {}
      end
  end
"""


def _head_rules_injected_extra(response_body: str, agent_type: str, query_source: str) -> dict:
    env = os.environ.copy()
    env["AGENT_TYPE"] = agent_type
    env["QSRC"] = query_source
    r = subprocess.run(["python3", "-c", _HEAD_RULES_INJECTED_EXTRA_PY], input=response_body,
                       env=env, capture_output=True, text=True, timeout=10)
    out = r.stdout.strip()
    return json.loads(out) if out else {}


def _jq_rules_injected_extra(response_body: str, agent_type: str, query_source: str) -> dict:
    r = subprocess.run(
        ["jq", "-R", "-s", "-c", "--arg", "at", agent_type, "--arg", "qs", query_source,
         _RULES_INJECTED_EXTRA_JQ_FILTER],
        input=response_body, capture_output=True, text=True, timeout=10,
    )
    out = r.stdout.strip()
    return json.loads(out) if out else {}


_RULES_INJECTED_EXTRA_CASES = [
    pytest.param(json.dumps({"rules": [{"rule_id": "A"}, {"rule_id": "B"}]}), id="normal-response"),
    pytest.param(json.dumps({}), id="missing-rules-key"),
    pytest.param(json.dumps({"rules": []}), id="rules-empty-list"),
    pytest.param(json.dumps({"rules": {}}), id="rules-empty-object"),
    pytest.param(json.dumps({"rules": ""}), id="rules-empty-string"),
    pytest.param(json.dumps({"rules": None}), id="rules-null"),
    pytest.param(json.dumps({"rules": "ab"}), id="rules-nonempty-string"),
    pytest.param(json.dumps({"rules": [1]}), id="rules-non-dict-element"),
    pytest.param(json.dumps({"rules": [{}]}), id="element-without-rule-id"),
    pytest.param(json.dumps({"rules": [{"rule_id": None}]}), id="element-with-rule-id-null"),
    pytest.param("not json", id="non-json"),
    pytest.param("[1, 2, 3]", id="json-array-envelope"),
]


class TestRulesInjectedExtraEquivalence:
    """Capability: RULES_INJECTED_EXTRA from both new arms parses to the same value
    as HEAD's, for a normal /query response, a missing `rules` key, `rules` of [], {},
    "", null, "ab", [1], an element without rule_id, an element with rule_id null,
    non-JSON and a JSON array."""

    @pytest.mark.parametrize("response_body", _RULES_INJECTED_EXTRA_CASES)
    def test_jq_arm_matches_head(self, response_body):
        expected = _head_rules_injected_extra(response_body, "writ-explorer", "task")
        actual = _jq_rules_injected_extra(response_body, "writ-explorer", "task")
        assert actual == expected, (response_body, expected, actual)

    def test_writ_no_jq_arm_is_head_kept_verbatim(self):
        """The plan keeps the python block verbatim as the else arm -- there is no
        second python expression to derive, so this pins that the reference IS the
        WRIT_NO_JQ arm (a no-op assertion that fails only if a future edit touches
        the snippet without updating this file's copy)."""
        response_body = json.dumps({"rules": [{"rule_id": "A"}]})
        assert _head_rules_injected_extra(response_body, "writ-explorer", "task") == {
            "agent_type": "writ-explorer", "query_source": "task",
            "rule_count": 1, "rule_ids": ["A"],
        }


# ---------------------------------------------------------------------------------
# SA_OUTPUT (writ-subagent-start.sh:444-455)
# ---------------------------------------------------------------------------------

# Verbatim from HEAD (writ-subagent-start.sh:445-455), run directly as the reference.
_HEAD_SA_OUTPUT_PY = """
import json, sys
ctx = sys.argv[1]
if sys.argv[2]:
    ctx = sys.argv[2] + '\\n' + ctx
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'SubagentStart',
        'additionalContext': ctx,
    }
}))
"""

# Literal from the plan's Analysis section, 4b, SA_OUTPUT.
_NEW_SA_OUTPUT_JQ_FILTER = (
    '{hookSpecificOutput:{hookEventName:"SubagentStart",'
    'additionalContext:(if $pi != "" then $pi + "\\n" + $ctx else $ctx end)}}'
)


def _head_sa_output(additional_context: str, phase_info: str) -> dict:
    r = subprocess.run(["python3", "-c", _HEAD_SA_OUTPUT_PY, additional_context, phase_info],
                       capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout.strip())


def _jq_sa_output(additional_context: str, phase_info: str) -> dict:
    r = subprocess.run(
        ["jq", "-n", "-c", "--arg", "ctx", additional_context, "--arg", "pi", phase_info,
         _NEW_SA_OUTPUT_JQ_FILTER],
        capture_output=True, text=True, timeout=10,
    )
    return json.loads(r.stdout.strip())


_SA_OUTPUT_CASES = [
    pytest.param("some rules text", "[Writ sub-agent: mode=work, phase=planning, gates=none]", id="with-phase-and-rules"),
    pytest.param("some rules text", "", id="without-phase-info"),
    pytest.param("", "[Writ sub-agent: mode=work, phase=planning, gates=none]", id="without-rules-text"),
    pytest.param('rules with "quotes" and \\backslash\\', "phase", id="quotes-and-backslashes"),
    pytest.param("line one\nline two", "phase", id="embedded-newline"),
    pytest.param("résumé — non-ascii \U0001F600", "phase", id="non-ascii"),
]


class TestSaOutputEquivalence:
    """Capability: SA_OUTPUT from both new arms parses to the same value as HEAD's,
    with and without PHASE_INFO, with and without rules text, and for text holding
    quotes, backslashes, newlines and non-ASCII characters; stdout stays a single
    JSON line (jq's `-c` and python's single `print` both guarantee this on their
    own, so this is asserted directly rather than re-derived)."""

    @pytest.mark.parametrize("additional_context,phase_info", [
        (p.values[0], p.values[1]) for p in _SA_OUTPUT_CASES
    ], ids=[p.id for p in _SA_OUTPUT_CASES])
    def test_jq_arm_matches_head(self, additional_context, phase_info):
        expected = _head_sa_output(additional_context, phase_info)
        actual = _jq_sa_output(additional_context, phase_info)
        assert actual == expected, (additional_context, phase_info, expected, actual)

    def test_jq_arm_output_is_a_single_line(self):
        r = subprocess.run(
            ["jq", "-n", "-c", "--arg", "ctx", "text", "--arg", "pi", "phase",
             _NEW_SA_OUTPUT_JQ_FILTER],
            capture_output=True, text=True, timeout=10,
        )
        lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, lines


# ---------------------------------------------------------------------------------
# Source inventory: the three inline extractions leave the hook's main path.
# ---------------------------------------------------------------------------------

# AGENT_PROMPT is not in this table: it goes through json_transform, so HEAD's inline
# python for it is gone entirely rather than kept as a WRIT_NO_JQ arm (see
# test_agent_prompt_goes_through_json_transform below).
_AGENT_PROMPT_HEAD_INLINE = "prompt = d.get('task') or d.get('prompt') or d.get('description') or d.get('message') or ''"

_INLINE_MARKERS = {
    "RULES_INJECTED_EXTRA": "'rule_count': len(rs),",
    "SA_OUTPUT": "'hookEventName': 'SubagentStart',",
}


class TestInlinePythonExtractionsLeaveTheMainPath:
    """Capability: writ-subagent-start.sh no longer contains the three inline python
    extractions for AGENT_PROMPT, RULES_INJECTED_EXTRA and SA_OUTPUT outside their
    WRIT_NO_JQ fallback arms, and still has exactly one
    `2>>"$WRIT_HOOK_LOG_SINK"`. RED at HEAD: today none of the three markers sits
    behind a jq arm at all (there is no literal "jq" anywhere in this hook's source),
    so each marker's only occurrence is unconditional, not a fallback.
    """

    def _source(self) -> str:
        return HOOK_SOURCE_PATH.read_text()

    @pytest.mark.parametrize("name,marker", list(_INLINE_MARKERS.items()))
    def test_marker_survives_only_as_a_fallback_arm(self, name, marker):
        source = self._source()
        occurrences = source.count(marker)
        assert occurrences == 1, (
            f"{name}: expected exactly one occurrence of the fallback marker, found "
            f"{occurrences}: {marker!r}"
        )
        idx = source.index(marker)
        preceding = source[max(0, idx - 600):idx]
        assert "jq" in preceding, (
            f"{name}: the fallback marker {marker!r} has no preceding jq arm within "
            "600 characters -- it is still the unconditional (non-fallback) path"
        )

    def test_agent_prompt_goes_through_json_transform(self):
        source = self._source()
        assert _AGENT_PROMPT_HEAD_INLINE not in source, (
            "AGENT_PROMPT: HEAD's inline python extraction is still in the hook"
        )
        assert re.search(r'AGENT_PROMPT=\$\(printf .%s. "\$STDIN_JSON" \| json_transform', source), (
            "AGENT_PROMPT must be extracted through json_transform"
        )

    def test_exactly_one_debug_log_sink_redirect(self):
        source = self._source()
        assert source.count('2>>"$WRIT_HOOK_LOG_SINK"') == 1, (
            "writ-subagent-start.sh must keep exactly one "
            '2>>"$WRIT_HOOK_LOG_SINK" redirect (tests/test_debug_gating.py pins this '
            "hook's debug-gating contract at exactly two stderr sinks total)"
        )
