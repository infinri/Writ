"""The jq stdin parser must be interchangeable with the python one.

Pins the capabilities.md section "The jq stdin parser is interchangeable with the
python one".

Why this exists: every hook spawns `parse-hook-stdin.py --shell` once just to read
its stdin envelope. Measured 2026-08-07, one `Write` fires 15 hooks and so pays 13
of these, at ~18ms each. The jq equivalent measured 3ms on the same envelope. That
is ~195ms per write for a parser swap, which is only safe if the two parsers agree.

THE ONE REAL HAZARD, and why part of this contract is semantic rather than
byte-identical: `parse-hook-stdin.py` re-serializes a normalized `HOOK_ENVELOPE`
with python's `json.dumps`, which writes `", "` separators, while jq's `tojson` is
compact. Those strings differ by whitespace. Every consumer re-parses that envelope
rather than string-matching it, so equal-parsed-objects is the correct contract, but
that is asserted here AND backed by a repo scan proving no consumer compares it as
text. The scalar assignments must still match byte for byte.

Per TEST-TDD-001: skeletons approved before implementation.
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
PY_PARSER = REPO / "bin" / "lib" / "parse-hook-stdin.py"
JQ_FILTER = REPO / "bin" / "lib" / "parse-hook-stdin.jq"

# The scalar assignments both parsers must agree on byte for byte. HOOK_ENVELOPE is
# excluded deliberately and checked separately, for the separator reason above.
SCALAR_KEYS = (
    "HOOK_SESSION_ID", "HOOK_SESSION_ID_RAW", "HOOK_AGENT_ID", "HOOK_AGENT_TYPE",
    "HOOK_EVENT", "HOOK_TOOL_NAME", "HOOK_FILE_PATH", "HOOK_COMMAND", "HOOK_IS_ERROR",
)

ENVELOPES = {
    "normal_write": {
        "session_id": "s-1", "tool_name": "Write", "hook_event_name": "PreToolUse",
        "cwd": "/tmp", "tool_input": {"file_path": "/tmp/x.py", "content": "y = 1\n"},
    },
    "bash_command": {
        "session_id": "s-2", "tool_name": "Bash", "hook_event_name": "PreToolUse",
        "tool_input": {"command": "ls -la | grep x"},
    },
    "edit_with_strings": {
        "session_id": "s-3", "tool_name": "Edit", "hook_event_name": "PreToolUse",
        "tool_input": {"file_path": "/tmp/a.py", "old_string": "a", "new_string": "b"},
    },
    "missing_optional_keys": {"session_id": "s-4"},
    "empty_tool_input": {"session_id": "s-5", "tool_name": "Read", "tool_input": {}},
    "error_result": {
        "session_id": "s-6", "tool_name": "Bash", "hook_event_name": "PostToolUse",
        "tool_result_is_error": True, "tool_input": {"command": "false"},
    },
    "quotes_and_newlines": {
        "session_id": "s-7", "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/q.py", "content": 'a "quoted" and \n newline'},
    },
    "backslashes": {
        "session_id": "s-8", "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/b.py", "content": "C:\\\\path\\n not newline"},
    },
    "unicode": {
        "session_id": "s-9", "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/u.py", "content": "caf\u00e9 \u2713 \U0001f600"},
    },
    "single_quote_in_value": {
        "session_id": "s-10", "tool_name": "Bash",
        "tool_input": {"command": "echo 'it''s fine'"},
    },
    "agent_fields": {
        "session_id": "s-11", "agent_id": "a-1", "agent_type": "writ-reviewer",
        "tool_name": "Write", "tool_input": {"file_path": "/tmp/z.py"},
    },
    # The four shapes below were added after the first implementation, because the
    # eleven above did not distinguish the two parsers on inputs that CC can actually
    # send. Both of the first two DID diverge when measured (2026-08-07).
    "explicit_nulls": {
        "session_id": "s-12", "agent_id": None, "agent_type": None,
        "hook_event_name": None, "tool_name": None,
        "tool_input": {"file_path": "/tmp/n.py"},
    },
    "null_tool_input": {"session_id": "s-13", "tool_name": "Read", "tool_input": None},
    "tool_input_as_json_string": {
        "session_id": "s-14", "tool_name": "Write",
        "tool_input": '{"file_path": "/tmp/s.py", "content": "x"}',
    },
    "notebook_empty_content_falls_to_new_source": {
        "session_id": "s-15", "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": "/tmp/n.ipynb", "content": "",
                       "new_source": "cell body"},
    },
}

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; the python path is the fallback"
)


def _child_env(extra: dict[str, str] | None) -> dict[str, str] | None:
    if not extra:
        return None
    return {**os.environ, **extra}


def _run_python(envelope: str, extra_env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        ["python3", str(PY_PARSER), "--shell"], input=envelope,
        capture_output=True, text=True, timeout=30, env=_child_env(extra_env),
    )
    return proc.stdout


def _run_jq(envelope: str, extra_env: dict[str, str] | None = None) -> str:
    proc = subprocess.run(
        # -R -s is part of the filter's contract; see its header. Keep this in step
        # with load_hook_env's invocation or the test stops testing the real path.
        ["jq", "-R", "-s", "-r", "-f", str(JQ_FILTER)], input=envelope,
        capture_output=True, text=True, timeout=30, env=_child_env(extra_env),
    )
    assert proc.returncode == 0, f"jq failed: {proc.stderr[:300]}"
    return proc.stdout


def _assignments(shell_output: str) -> dict[str, str]:
    """Parse `KEY=value` lines into a dict, keeping the value exactly as emitted."""
    out: dict[str, str] = {}
    for line in shell_output.splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _unquote(value: str) -> str:
    """Evaluate a single shell-quoted token back to its literal string."""
    proc = subprocess.run(
        ["bash", "-c", f"printf '%s' {value}"], capture_output=True, text=True, timeout=30
    )
    return proc.stdout


# --------------------------------------------------------------------------- #
# 1. Scalar parity
# --------------------------------------------------------------------------- #
class TestScalarParity:
    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_scalar_values_match_after_eval(self, name: str) -> None:
        """Compares the VALUE each assignment yields, not the raw quoted text.

        Corrected while implementing: `shlex.quote` leaves a safe token bare
        (`HOOK_TOOL_NAME=Write`) while jq's `@sh` always wraps it
        (`HOOK_TOOL_NAME='Write'`). Both eval to the same variable, so the meaningful
        contract is per-field equality after the eval the hooks actually perform.
        Byte-identity would fail on a pure quoting-style difference and would say
        nothing about whether a hook behaves the same.
        """
        raw = json.dumps(ENVELOPES[name])
        py, jq = _assignments(_run_python(raw)), _assignments(_run_jq(raw))
        for key in SCALAR_KEYS:
            assert _unquote(jq[key]) == _unquote(py[key]), (
                f"{name}: {key} differs after eval\n"
                f"  python: {_unquote(py[key])!r}\n  jq:     {_unquote(jq[key])!r}"
            )

    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_both_outputs_are_safely_evaluable(self, name: str) -> None:
        """The hooks `eval` this output, so quoting must be correct on both paths.
        A value that broke out of its quoting would execute here."""
        raw = json.dumps(ENVELOPES[name])
        for producer in (_run_python, _run_jq):
            out = producer(raw)
            proc = subprocess.run(
                ["bash", "-c", f"eval {shlex.quote(out)}; printf 'OK'"],
                capture_output=True, text=True, timeout=30,
            )
            assert proc.stdout == "OK", f"{name}: eval failed: {proc.stderr[:200]}"

    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_every_scalar_key_is_emitted_by_both(self, name: str) -> None:
        """A parser that silently drops a key would pass the comparison above by
        both sides being absent."""
        raw = json.dumps(ENVELOPES[name])
        py, jq = _assignments(_run_python(raw)), _assignments(_run_jq(raw))
        for key in SCALAR_KEYS:
            assert key in py, f"{name}: python parser did not emit {key}"
            assert key in jq, f"{name}: jq parser did not emit {key}"

    def test_env_fallback_keys_on_absence_not_on_null(self) -> None:
        """The divergence that mattered most, and it only shows with the env set.

        python spells `event` as `.get("hook_event_name", $ENV)`, so an explicit null
        in the envelope IS a value and the env is never consulted. jq's `//` operator
        falls through on null, so the first port answered HOOK_EVENT=PreToolUse where
        python answered the empty string. Hooks branch on HOOK_EVENT, so that is a
        behavior change, and no envelope in the set above could catch it because none
        of them ran with these variables set.
        """
        env = {"HOOK_EVENT": "PreToolUse", "HOOK_TOOL_NAME": "Write",
               "HOOK_TOOL_IS_ERROR": "1"}
        raw = json.dumps(ENVELOPES["explicit_nulls"])
        py, jq = _assignments(_run_python(raw, env)), _assignments(_run_jq(raw, env))
        for key in SCALAR_KEYS:
            assert _unquote(jq[key]) == _unquote(py[key]), f"{key} diverges under env"
        assert _unquote(jq["HOOK_EVENT"]) == "", (
            "an explicit null must not be replaced by the env value"
        )

    def test_env_fallback_applies_when_the_key_is_absent(self) -> None:
        """Anti-vacuity for the test above: absence MUST reach the env, or the two
        parsers would agree simply by both ignoring it."""
        env = {"HOOK_EVENT": "PreToolUse", "HOOK_TOOL_NAME": "Write"}
        raw = json.dumps({"session_id": "s-x", "tool_input": {}})
        py, jq = _assignments(_run_python(raw, env)), _assignments(_run_jq(raw, env))
        assert _unquote(py["HOOK_EVENT"]) == "PreToolUse"
        assert _unquote(jq["HOOK_EVENT"]) == "PreToolUse"

    @pytest.mark.parametrize("raw", ['[1,2]', '"a string"', '42', 'null', ''])
    def test_a_non_object_root_yields_empty_fields_on_both_paths(self, raw: str) -> None:
        """`has()` raises on an array and `.get` raises on a list, so an envelope that
        is valid JSON but not an object used to abort one parser and not the other.
        Both must answer "no fields" instead."""
        py, jq = _assignments(_run_python(raw)), _assignments(_run_jq(raw))
        for key in SCALAR_KEYS:
            assert _unquote(jq[key]) == _unquote(py[key]), f"{raw!r}: {key} diverges"

    def test_shell_quoting_survives_a_round_trip(self) -> None:
        """The emitted values are eval'd by the calling hook, so quoting has to be
        correct, not merely equal: a value with a single quote is the trap.

        Asserts against the FIXTURE value rather than a hand-typed expectation. The
        hand-typed version of this test was wrong (I dropped the doubled quote while
        transcribing it) and would have failed a correct implementation.
        """
        expected = ENVELOPES["single_quote_in_value"]["tool_input"]["command"]
        raw = json.dumps(ENVELOPES["single_quote_in_value"])
        assert _unquote(_assignments(_run_jq(raw))["HOOK_COMMAND"]) == expected
        assert _unquote(_assignments(_run_python(raw))["HOOK_COMMAND"]) == expected


# --------------------------------------------------------------------------- #
# 2. Envelope parity (semantic, for the documented separator reason)
# --------------------------------------------------------------------------- #
class TestEnvelopeParity:
    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_envelopes_parse_to_equal_objects(self, name: str) -> None:
        raw = json.dumps(ENVELOPES[name])
        py, jq = _assignments(_run_python(raw)), _assignments(_run_jq(raw))
        assert json.loads(_unquote(py["HOOK_ENVELOPE"])) == \
               json.loads(_unquote(jq["HOOK_ENVELOPE"])), name

    def test_no_hook_compares_the_envelope_as_text(self) -> None:
        """This is what makes semantic equality sufficient instead of a gamble. If a
        hook string-matched HOOK_ENVELOPE, the separator difference would change
        behavior and that hook would have to keep the python parser."""
        offenders = []
        pattern = re.compile(
            r'(\[\[?\s*"?\$\{?HOOK_ENVELOPE\}?"?\s*==|'
            r'grep[^\n|]*\$\{?HOOK_ENVELOPE|'
            r'case\s+"?\$\{?HOOK_ENVELOPE)'
        )
        for path in (REPO / "hooks" / "scripts").glob("*.sh"):
            if pattern.search(path.read_text()):
                offenders.append(path.name)
        assert offenders == [], (
            f"these hooks compare HOOK_ENVELOPE as text, so the jq parser's compact "
            f"serialization would change their behavior: {offenders}"
        )


# --------------------------------------------------------------------------- #
# 3. The fallback contract
# --------------------------------------------------------------------------- #
class TestFallback:
    def test_no_jq_forces_the_python_parser(self) -> None:
        """WRIT_NO_JQ=1 must route load_hook_env through python, mirroring the seam
        parsed_field already uses."""
        raw = json.dumps(ENVELOPES["normal_write"])
        script = (
            f"source {REPO}/bin/lib/common.sh >/dev/null 2>&1; "
            f"WRIT_NO_JQ=1 load_hook_env; printf '%s' \"$HOOK_SESSION_ID\""
        )
        proc = subprocess.run(
            ["bash", "-c", script], input=raw, capture_output=True, text=True, timeout=30
        )
        assert proc.stdout == "s-1"

    def test_the_jq_arm_is_the_one_actually_used(self) -> None:
        """The whole saving rests on WHICH arm runs, and every error on the jq arm is
        swallowed by `2>/dev/null`, so a broken filter would fall back to python
        forever and look exactly like success. This test discriminates the arms
        directly: divergence 7 (`@sh` always quotes, `shlex.quote` leaves safe tokens
        bare) is a reliable fingerprint of which parser produced the line.
        """
        raw = json.dumps(ENVELOPES["normal_write"])
        script = (
            f"source {REPO}/bin/lib/common.sh >/dev/null 2>&1; _writ_parse_hook_stdin"
        )
        out = subprocess.run(
            ["bash", "-c", script], input=raw, capture_output=True, text=True, timeout=30
        ).stdout
        assert "HOOK_TOOL_NAME='Write'" in out, (
            "the python arm ran (bare token), so the jq filter is not being used:\n" + out
        )

    def test_forcing_the_fallback_reaches_the_python_arm(self) -> None:
        """Anti-vacuity for the fingerprint above: if both arms quoted identically the
        assertion would prove nothing."""
        raw = json.dumps(ENVELOPES["normal_write"])
        script = (
            f"source {REPO}/bin/lib/common.sh >/dev/null 2>&1; "
            f"WRIT_NO_JQ=1 _writ_parse_hook_stdin"
        )
        out = subprocess.run(
            ["bash", "-c", script], input=raw, capture_output=True, text=True, timeout=30
        ).stdout
        assert "HOOK_TOOL_NAME=Write" in out and "HOOK_TOOL_NAME='Write'" not in out

    def test_a_missing_filter_file_still_parses_via_python(self) -> None:
        """The partial-install case the arm choice is ordered around: jq is present,
        the filter is not. stdin must still reach python intact."""
        raw = json.dumps(ENVELOPES["normal_write"])
        script = (
            f"source {REPO}/bin/lib/common.sh >/dev/null 2>&1; "
            f"_PARSE_HOOK_STDIN_JQ=/nonexistent/parse.jq load_hook_env; "
            f"printf '%s' \"$HOOK_SESSION_ID\""
        )
        out = subprocess.run(
            ["bash", "-c", script], input=raw, capture_output=True, text=True, timeout=30
        ).stdout
        assert out == "s-1"

    def test_jq_and_fallback_agree_through_load_hook_env(self) -> None:
        """End to end through the real helper, not just the two parsers side by side."""
        raw = json.dumps(ENVELOPES["quotes_and_newlines"])
        outs = []
        for env in ("", "WRIT_NO_JQ=1 "):
            script = (
                f"source {REPO}/bin/lib/common.sh >/dev/null 2>&1; "
                f"{env}load_hook_env; "
                f"printf '%s|%s|%s' \"$HOOK_SESSION_ID\" \"$HOOK_TOOL_NAME\" \"$HOOK_FILE_PATH\""
            )
            outs.append(subprocess.run(
                ["bash", "-c", script], input=raw, capture_output=True, text=True, timeout=30
            ).stdout)
        assert outs[0] == outs[1] != ""

    def test_malformed_envelope_fails_the_same_way_on_both_paths(self) -> None:
        """A truncated envelope must not leave one path populated and the other empty."""
        raw = '{"session_id": "s-1", "tool_input": {'
        outs = []
        for env in ("", "WRIT_NO_JQ=1 "):
            script = (
                f"source {REPO}/bin/lib/common.sh >/dev/null 2>&1; "
                f"{env}load_hook_env 2>/dev/null; printf '%s' \"$HOOK_SESSION_ID\""
            )
            outs.append(subprocess.run(
                ["bash", "-c", script], input=raw, capture_output=True, text=True, timeout=30
            ).stdout)
        assert outs[0] == outs[1]
