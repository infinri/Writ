"""The pre-write dispatch parse must mean the same thing on both arms.

writ-pre-write-dispatch.sh is the hottest gate path in the system and had the most
python starts of any hook (8, measured 2026-08-07). Its consolidated stdin parse now
runs as a jq filter with the original python snippet kept as the fallback, so this
pins the two against each other.

THE REFERENCE IS EXTRACTED FROM THE HOOK, not copied into this file. A copy rots: an
edit to the python arm would leave the test asserting the old behavior while claiming
to compare arms. Pulling the snippet out of the hook source means the comparison is
always against whatever the fallback actually does today.

Line 3 (the /pre-write-check body) is compared as PARSED JSON, for the reason
documented in test_hook_env_parity: python's json.dumps writes ", " separators and
jq's tojson is compact. That body is an HTTP request body which FastAPI parses, so
semantic equality is the operative contract; lines 1 and 2 are read by `head`/`sed` in
the hook and are compared byte for byte.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-pre-write-dispatch.sh"
JQ_FILTER = REPO / "bin" / "lib" / "pre-write-parse.jq"
SKILL_DIR = "/skill/dir"

ENVELOPES = {
    "write": {"session_id": "s-1", "tool_name": "Write",
              "tool_input": {"file_path": "/tmp/a.py", "content": "line1\nline2"}},
    "edit": {"session_id": "s-2", "tool_name": "Edit",
             "tool_input": {"file_path": "/tmp/b.py", "old_string": "a",
                            "new_string": "b"}},
    "notebook_path_mapping": {"session_id": "s-3", "tool_name": "NotebookEdit",
                              "tool_input": {"notebook_path": "/tmp/n.ipynb",
                                             "new_source": "cell"}},
    "agent_id_wins": {"session_id": "s-4", "agent_id": "a-9",
                      "tool_input": {"file_path": "/tmp/c.py"}},
    "session_id_needs_stripping": {"session_id": "  s-5  ",
                                   "tool_input": {"file_path": "/tmp/d.py"}},
    "tool_input_as_string": {"session_id": "s-6",
                             "tool_input": '{"file_path": "/tmp/e.py"}'},
    "tool_input_null": {"session_id": "s-7", "tool_input": None},
    "explicit_nulls": {"session_id": "s-8", "agent_id": None,
                       "tool_input": {"file_path": "/tmp/f.py", "content": None}},
    "empty_content_falls_to_new_source": {
        "session_id": "s-9",
        "tool_input": {"file_path": "/tmp/g.py", "content": "", "new_source": "src"}},
    "carriage_returns": {"session_id": "s-10",
                         "tool_input": {"file_path": "/tmp/h.py",
                                        "content": "a\r\nb"}},
    "quotes_and_backslashes": {"session_id": "s-11",
                               "tool_input": {"file_path": "/tmp/i.py",
                                              "content": 'say "hi" C:\\path'}},
    "unicode": {"session_id": "s-12",
                "tool_input": {"file_path": "/tmp/j.py", "content": "caf\u00e9 \u2713"}},
    "no_tool_input": {"session_id": "s-13"},
    "session_id_with_embedded_newline": {"session_id": "abc\ndef",
                                         "tool_input": {"file_path": "/tmp/k.py"}},
    # NON-STRING ids. Added after review caught the newline fix introducing an
    # asymmetry: the python arm gained an isinstance guard and the jq arm did not, so
    # agent_id 5 gave "5" on jq and "" on python. Coercion is not parity-safe here
    # (jq tostring gives "true", python str() gives "True"), so both arms emit "".
    "agent_id_is_a_number": {"agent_id": 5, "session_id": "s-14",
                             "tool_input": {"file_path": "/tmp/l.py"}},
    "agent_id_is_a_float": {"agent_id": 5.0, "tool_input": {"file_path": "/tmp/m.py"}},
    "agent_id_is_true": {"agent_id": True, "tool_input": {"file_path": "/tmp/o.py"}},
    "session_id_is_a_list": {"session_id": [1], "tool_input": {"file_path": "/tmp/p.py"}},
    "session_id_is_an_object": {"session_id": {"a": 1},
                                "tool_input": {"file_path": "/tmp/q.py"}},
}

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed; the python arm is the fallback"
)


def _python_reference() -> str:
    """The fallback snippet, lifted out of the hook it lives in."""
    src = HOOK.read_text()
    m = re.search(r'PARSED_INPUT=\$\(python3 -c "\n(.*?)\n" "\$STDIN_DATA"', src, re.S)
    assert m, (
        "could not find the python fallback snippet in writ-pre-write-dispatch.sh; "
        "if the call site was restructured, update this extraction rather than "
        "pasting a copy of the snippet into the test"
    )
    return m.group(1)


def _run_python(envelope: str) -> str:
    return subprocess.run(
        ["python3", "-c", _python_reference(), envelope, SKILL_DIR],
        capture_output=True, text=True, timeout=30,
    ).stdout


def _run_jq(envelope: str) -> str:
    proc = subprocess.run(
        ["jq", "-R", "-s", "-r", "--arg", "skill_dir", SKILL_DIR, "-f", str(JQ_FILTER)],
        input=envelope, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, f"jq failed: {proc.stderr[:300]}"
    return proc.stdout


def _split(out: str) -> tuple[str, str, str]:
    """Mirror the hook's own head -1 / sed -n 2p / tail -n +3 split."""
    lines = out.split("\n")
    return lines[0], (lines[1] if len(lines) > 1 else ""), "\n".join(lines[2:])


class TestArmParity:
    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_session_id_and_write_context_are_byte_identical(self, name: str) -> None:
        raw = json.dumps(ENVELOPES[name])
        p_sid, p_ctx, _ = _split(_run_python(raw))
        j_sid, j_ctx, _ = _split(_run_jq(raw))
        assert j_sid == p_sid, f"{name}: session id differs"
        assert j_ctx == p_ctx, f"{name}: write context differs"

    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_check_body_parses_to_the_same_object(self, name: str) -> None:
        raw = json.dumps(ENVELOPES[name])
        _, _, p_body = _split(_run_python(raw))
        _, _, j_body = _split(_run_jq(raw))
        assert json.loads(j_body) == json.loads(p_body), f"{name}: body differs"

    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_both_arms_emit_a_non_empty_body(self, name: str) -> None:
        """Anti-vacuity, and it guards a live branch: the hook treats an empty parse
        as "nothing to check" and exits 0, which on this path means the write gate
        does not run. Both arms must always produce the three lines."""
        raw = json.dumps(ENVELOPES[name])
        for label, out in (("python", _run_python(raw)), ("jq", _run_jq(raw))):
            _, _, body = _split(out)
            assert body.strip(), f"{name}: {label} arm produced no check body"
            assert json.loads(body)["skill_dir"] == SKILL_DIR


class TestTheThreeLineContract:
    """The call site splits this output BY POSITION, so line count is a contract.

    Found by review: neither arm stripped embedded newlines from the session id, so
    `"session_id": "abc\\ndef"` emitted four lines. SESSION_ID silently truncated to
    "abc" and CHECK_BODY became a stray line glued to the real JSON body, which is then
    POSTed to /pre-write-check. Both arms did it identically, so arm-parity tests could
    not see it: the hole was in the format, not in the translation.
    """

    @pytest.mark.parametrize("name", sorted(ENVELOPES))
    def test_output_is_exactly_three_lines_on_both_arms(self, name: str) -> None:
        raw = json.dumps(ENVELOPES[name])
        for label, out in (("python", _run_python(raw)), ("jq", _run_jq(raw))):
            lines = out.rstrip("\n").split("\n")
            assert len(lines) == 3, (
                f"{name}: {label} arm emitted {len(lines)} lines, not 3: {lines!r}"
            )

    @pytest.mark.parametrize("name", [
        "agent_id_is_a_number", "agent_id_is_a_float", "agent_id_is_true",
        "session_id_is_a_list", "session_id_is_an_object",
    ])
    def test_a_non_string_id_becomes_empty_on_both_arms(self, name: str) -> None:
        """Pins the VALUE, not just arm equality, because the two obvious answers
        (coerce, or empty) are both self-consistent and only one is parity-safe.

        Coercion is not: jq's tostring gives "true" where python's str() gives "True".
        Empty has no formatting to disagree about, and it routes to the hook's existing
        detect_session_id fallback instead of inventing an id. Note the envelope for
        agent_id_is_a_number ALSO carries a valid session_id, and the answer is still
        empty: python's `or` picks the truthy agent_id first and never reaches the
        session_id, so a jq arm that fell back to session_id here would diverge.
        """
        raw = json.dumps(ENVELOPES[name])
        for label, out in (("python", _run_python(raw)), ("jq", _run_jq(raw))):
            sid, _, body = _split(out)
            assert sid == "", f"{label} arm: expected empty id, got {sid!r}"
            assert json.loads(body)["session_id"] == "", f"{label} arm body"

    def test_a_newline_in_the_session_id_does_not_shift_the_lines(self) -> None:
        raw = json.dumps(ENVELOPES["session_id_with_embedded_newline"])
        for label, out in (("python", _run_python(raw)), ("jq", _run_jq(raw))):
            sid, _, body = _split(out)
            assert "\n" not in sid and sid != "abc", (
                f"{label} arm: session id truncated at the newline: {sid!r}"
            )
            # The body must still be the JSON object, not a fragment plus an object.
            assert json.loads(body)["file_path"] == "/tmp/k.py", f"{label} arm body"


class TestDegenerateInput:
    @pytest.mark.parametrize("raw", ["", "{", "[1,2]", "null", '"str"'])
    def test_malformed_stdin_still_yields_three_lines_on_both_arms(self, raw: str) -> None:
        """The python arm wraps json.loads in try/except and carries on with {}. jq
        would normally exit non-zero and print nothing, which is why the filter is
        invoked with -R -s and does its own parsing inside a `try`."""
        p_sid, p_ctx, p_body = _split(_run_python(raw))
        j_sid, j_ctx, j_body = _split(_run_jq(raw))
        assert (j_sid, j_ctx) == (p_sid, p_ctx), f"{raw!r}: scalars differ"
        assert json.loads(j_body) == json.loads(p_body), f"{raw!r}: body differs"


class TestBothArmsAreReachable:
    def test_the_hook_has_a_jq_arm_and_a_python_fallback(self) -> None:
        src = HOOK.read_text()
        assert "pre-write-parse.jq" in src, "the hook does not invoke the jq filter"
        assert "python3 -c" in src, "the python fallback was removed, not kept"
        assert "WRIT_NO_JQ" in src, "the hook has no seam to force the fallback"

    def test_forcing_no_jq_leaves_the_hook_functional(self) -> None:
        """End to end through the real hook on both arms: it must not crash or hang
        with the fallback forced. Exit status only -- what the gate DECIDES depends on
        session state, which this test deliberately does not set up."""
        raw = json.dumps(ENVELOPES["write"])
        for env_prefix in ([], ["env", "WRIT_NO_JQ=1"]):
            proc = subprocess.run(
                [*env_prefix, "bash", str(HOOK)], input=raw,
                capture_output=True, text=True, timeout=120,
            )
            assert proc.returncode in (0, 2), (
                f"hook exited {proc.returncode} with {env_prefix}: {proc.stderr[:300]}"
            )
